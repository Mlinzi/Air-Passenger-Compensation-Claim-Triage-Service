"""LLM gateway (A5, L5, D6): the only component that talks to a model provider.

* Provider chosen by configuration: `openai_compat` (any OpenAI-compatible endpoint: Groq,
  Gemini's OpenAI endpoint, OpenRouter, Ollama, ...) or `stub` (fixed responses after a
  realistic delay -- used for load testing so we measure our system, not provider limits).
* Per-call timeout, bounded retry with exponential backoff + jitter on retryable errors,
  and a circuit breaker shared by all callers of this gateway instance.
* Tokens and cost are computed at call time and returned to the caller for per-step attribution.
"""
import asyncio
import json
import logging
import random
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from opentelemetry.propagate import extract
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel

from triage.common.metrics import register_uptime
from triage.common.obs import correlation_id, log, setup_logging, setup_tracing, tracer
from triage.common.settings import get_settings

s = get_settings()
setup_logging("llm-gateway", s.log_level)
setup_tracing("llm-gateway", s.otel_endpoint)
logger = logging.getLogger("gateway")
register_uptime("llm-gateway")

CALLS = Counter("llm_calls_total", "Provider calls", ["outcome"])
RETRIES = Counter("llm_retries_total", "Provider retries")
LAT = Histogram("llm_call_seconds", "Provider latency", buckets=(0.25, 0.5, 1, 2, 3, 5, 8, 13, 20, 30))
BREAKER = Gauge("llm_circuit_state", "0 closed, 1 half-open, 2 open")

app = FastAPI(title="LLM gateway")


class Breaker:
    def __init__(self, threshold: int, reset_s: float):
        self.threshold, self.reset_s = threshold, reset_s
        self.failures, self.state, self.opened_at = 0, "closed", 0.0
        self.lock = asyncio.Lock()

    def _gauge(self):
        BREAKER.set({"closed": 0, "half_open": 1, "open": 2}[self.state])

    async def allow(self) -> bool:
        async with self.lock:
            if self.state == "open" and time.time() - self.opened_at >= self.reset_s:
                self.state = "half_open"
                self._gauge()
                log(logger, "circuit half-open")
            return self.state != "open"

    async def success(self):
        async with self.lock:
            if self.state != "closed":
                log(logger, "circuit closed")
            self.failures, self.state = 0, "closed"
            self._gauge()

    async def failure(self):
        async with self.lock:
            self.failures += 1
            if self.state == "half_open" or self.failures >= self.threshold:
                if self.state != "open":
                    log(logger, "circuit OPEN", logging.WARNING, failures=self.failures)
                self.state, self.opened_at = "open", time.time()
            self._gauge()


breaker = Breaker(s.breaker_fail_threshold, s.breaker_reset_s)
BREAKER.set(0)
_runtime = {"stub_fail": s.stub_fail}


class Completion(BaseModel):
    task: str
    agent: str
    step: str
    prompt_version: str = ""
    system: str
    user: str
    max_tokens: int = 400
    json_mode: bool = True


class ProviderError(Exception):
    def __init__(self, msg, retryable=True):
        super().__init__(msg)
        self.retryable = retryable


def est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


STUB_RESPONSES = {
    "intake": {"category": None},
    "research": {"action": "finish"},
    "draft": {"rationale": ""},
    "verify": {"supported": True, "reason": "stub"},
}


async def call_stub(req: Completion) -> tuple[str, int, int]:
    await asyncio.sleep(s.stub_delay_s * random.uniform(0.8, 1.2))
    text = json.dumps(STUB_RESPONSES.get(req.task, {}))
    return text, est_tokens(req.system + req.user), est_tokens(text) + 40


async def call_openai_compat(req: Completion) -> tuple[str, int, int]:
    if not (s.llm_base_url and s.llm_model):
        raise ProviderError("LLM_BASE_URL / LLM_MODEL not configured", retryable=False)
    body = {"model": s.llm_model, "max_tokens": req.max_tokens, "temperature": 0,
            "messages": [{"role": "system", "content": req.system}, {"role": "user", "content": req.user}]}
    if req.json_mode:
        body["response_format"] = {"type": "json_object"}
    try:
        async with httpx.AsyncClient(timeout=s.llm_timeout_s) as client:
            r = await client.post(s.llm_base_url.rstrip("/") + "/chat/completions", json=body,
                                  headers={"Authorization": f"Bearer {s.llm_api_key}"})
    except (httpx.TimeoutException, httpx.TransportError) as e:
        raise ProviderError(f"transport: {type(e).__name__}")
    if r.status_code == 429 or r.status_code >= 500:
        raise ProviderError(f"provider status {r.status_code}")
    if r.status_code >= 400:
        raise ProviderError(f"provider status {r.status_code}: {r.text[:200]}", retryable=False)
    data = r.json()
    text = data["choices"][0]["message"]["content"] or ""
    usage = data.get("usage") or {}
    return text, usage.get("prompt_tokens") or est_tokens(req.system + req.user), \
        usage.get("completion_tokens") or est_tokens(text)


@app.post("/v1/complete")
async def complete(req: Completion, request: Request):
    correlation_id.set(request.headers.get("X-Correlation-ID", "-"))
    ctx = extract(dict(request.headers))
    with tracer().start_as_current_span(f"llm.{req.task}", context=ctx) as span:
        span.set_attribute("llm.agent", req.agent)
        span.set_attribute("llm.step", req.step)
        span.set_attribute("prompt.version", req.prompt_version)
        if not await breaker.allow():
            CALLS.labels("short_circuited").inc()
            return JSONResponse({"error": "circuit_open"}, status_code=503)
        real = call_stub if s.llm_provider == "stub" else call_openai_compat

        async def provider(r):
            if _runtime["stub_fail"]:  # simulated outage applies to any provider
                await asyncio.sleep(0.2)
                raise ProviderError("simulated provider outage")
            return await real(r)
        last = None
        for attempt in range(s.llm_max_retries + 1):
            t0 = time.time()
            try:
                text, pt, ct = await asyncio.wait_for(provider(req), timeout=s.llm_timeout_s)
                LAT.observe(time.time() - t0)
                await breaker.success()
                CALLS.labels("ok").inc()
                cost = pt / 1000 * s.llm_price_prompt_per_1k + ct / 1000 * s.llm_price_completion_per_1k
                span.set_attribute("llm.prompt_tokens", pt)
                span.set_attribute("llm.completion_tokens", ct)
                return {"text": text, "prompt_tokens": pt, "completion_tokens": ct, "cost_usd": round(cost, 8),
                        "provider": s.llm_provider, "attempts": attempt + 1, "latency_s": round(time.time() - t0, 3)}
            except (ProviderError, asyncio.TimeoutError) as e:
                last = e
                retryable = getattr(e, "retryable", True)
                await breaker.failure()
                log(logger, "provider call failed", logging.WARNING, attempt=attempt + 1, error=str(e) or "timeout",
                    retryable=retryable)
                if not retryable or attempt == s.llm_max_retries or not await breaker.allow():
                    break
                RETRIES.inc()
                await asyncio.sleep(min(8.0, s.llm_backoff_base_s * 2 ** attempt) * random.uniform(0.5, 1.5))
        CALLS.labels("failed").inc()
        status = 503 if breaker.state == "open" else 502
        return JSONResponse({"error": "provider_failed", "detail": str(last) or "timeout",
                             "circuit": breaker.state}, status_code=status)


@app.post("/admin/fault")
async def fault(enable: bool = True):
    """Demo hook: simulate a provider outage (stub provider only)."""
    _runtime["stub_fail"] = enable
    return {"stub_fail": enable, "provider": s.llm_provider}


@app.get("/health")
async def health():
    return {"status": "ok", "provider": s.llm_provider, "circuit": breaker.state}


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
