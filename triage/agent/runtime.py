"""Per-run context: step recording (D5 reconstructibility), loop budgets (A1), model client (A5, L5)."""
import concurrent.futures as cf
import json
import logging
import os
import re
import time
from functools import lru_cache

import httpx
import yaml

from triage.common import db
from triage.common.metrics import AGENT_RETRIES, AGENT_STEP_LATENCY, LLM_COST, LLM_TOKENS
from triage.common.obs import inject_headers, log, tracer
from triage.common.settings import get_settings

logger = logging.getLogger("agent")
PROMPT_DIR = os.environ.get("PROMPT_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "prompts"))
_pool = cf.ThreadPoolExecutor(max_workers=4)


class LLMUnavailable(Exception):
    pass


class BudgetExceeded(Exception):
    pass


@lru_cache
def load_prompts(version: str) -> dict:
    with open(os.path.join(PROMPT_DIR, f"{version}.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class RunContext:
    def __init__(self, run_id: str, prompt_version: str, persist: bool = True, offline: bool = False):
        s = get_settings()
        self.run_id, self.prompt_version, self.persist = run_id, prompt_version, persist
        self.prompts = load_prompts(prompt_version)
        self.seq = 0
        self.steps = 0
        self.tool_calls = 0
        self.started = time.time()
        self.max_steps, self.max_tools, self.wall = s.agent_max_steps, s.agent_max_tool_calls, s.agent_wall_clock_s
        self.cost = 0.0
        self.pt = self.ct = 0
        self.offline = offline  # no model at all (local eval/tests); not a degradation
        self.llm_available = not offline
        self.degraded = False
        self.trace: list[dict] = []  # in-memory copy, used by eval and live scoring
        self.passages: list[dict] = []
        self.per_agent_steps: dict[str, int] = {}
        self.per_agent_tools: dict[str, int] = {}

    # ---- budgets -------------------------------------------------------------------------
    def elapsed(self) -> float:
        return time.time() - self.started

    def check_budget(self, tool: bool = False):
        if self.elapsed() > self.wall:
            raise BudgetExceeded(f"wall-clock limit {self.wall}s")
        if self.steps >= self.max_steps:
            raise BudgetExceeded(f"step limit {self.max_steps}")
        if tool and self.tool_calls >= self.max_tools:
            raise BudgetExceeded(f"tool-call limit {self.max_tools}")

    def remaining_s(self) -> float:
        return max(0.5, self.wall - self.elapsed())

    # ---- recording -----------------------------------------------------------------------
    def record(self, agent, kind, name, inp, out, latency_ms, ok=True, pt=0, ct=0, cost=0.0):
        self.seq += 1
        row = {"seq": self.seq, "agent": agent, "kind": kind, "name": name, "input": inp, "output": out,
               "latency_ms": round(latency_ms, 1), "prompt_version": self.prompt_version, "prompt_tokens": pt,
               "completion_tokens": ct, "cost_usd": cost, "ok": ok}
        self.trace.append(row)
        if kind in ("tool", "llm", "node"):
            self.per_agent_steps[agent] = self.per_agent_steps.get(agent, 0) + 1
        if kind == "tool":
            self.per_agent_tools[agent] = self.per_agent_tools.get(agent, 0) + 1
        AGENT_STEP_LATENCY.labels(agent, name).observe(latency_ms / 1000)
        if self.persist:
            try:
                db.add_step(self.run_id, self.seq, agent, kind, name, inp, out, latency_ms, self.prompt_version,
                            pt, ct, cost, ok)
            except Exception as e:  # tracing must not kill a run
                log(logger, "step persist failed", logging.WARNING, error=str(e))
        log(logger, f"{agent}.{name}", agent=agent, kind=kind, step=name, seq=self.seq, latency_ms=round(latency_ms, 1), ok=ok)

    def tool(self, agent: str, name: str, fn, **kwargs):
        """Runs a tool with a timeout and records it as one step."""
        self.check_budget(tool=True)
        self.tool_calls += 1
        self.steps += 1
        timeout = min(get_settings().tool_timeout_s, self.remaining_s())
        t0 = time.time()
        with tracer().start_as_current_span(f"tool.{name}") as span:
            span.set_attribute("agent", agent)
            span.set_attribute("tool.args", json.dumps(kwargs, default=str)[:500])
            try:
                out = _pool.submit(fn, **kwargs).result(timeout=timeout)
                ok = True
            except cf.TimeoutError:
                out, ok = {"error": f"tool timeout after {timeout:.1f}s"}, False
            except Exception as e:
                out, ok = {"error": f"{type(e).__name__}: {e}"}, False
        self.record(agent, "tool", name, kwargs, out, (time.time() - t0) * 1000, ok)
        return out, ok

    # ---- model calls ---------------------------------------------------------------------
    def llm(self, agent: str, task: str, system: str, user: str, max_tokens: int = 400) -> dict | None:
        """Returns parsed JSON or None. Never raises for provider problems: marks the run degraded instead."""
        if not self.llm_available or not get_settings().gateway_url:
            return None
        self.check_budget()
        self.steps += 1
        s = get_settings()
        t0 = time.time()
        payload = {"task": task, "agent": agent, "step": task, "prompt_version": self.prompt_version,
                   "system": system, "user": user, "max_tokens": max_tokens, "json_mode": True}
        out, ok, pt, ct, cost = None, False, 0, 0, 0.0
        err = None
        for attempt in range(2):  # one local retry on transport errors; provider retries live in the gateway
            try:
                r = httpx.post(s.gateway_url + "/v1/complete", json=payload, headers=inject_headers(),
                               timeout=min(s.gateway_call_timeout_s, self.remaining_s()))
                if r.status_code == 503:
                    err = "circuit open"
                    break
                if r.status_code >= 400:
                    err = f"gateway {r.status_code}: {r.text[:200]}"
                    break
                data = r.json()
                pt, ct, cost = data["prompt_tokens"], data["completion_tokens"], data["cost_usd"]
                if data.get("attempts", 1) > 1:
                    AGENT_RETRIES.labels(agent, "llm").inc(data["attempts"] - 1)
                out, ok = parse_json(data["text"]), True
                break
            except (httpx.TimeoutException, httpx.TransportError) as e:
                err = f"{type(e).__name__}"
                AGENT_RETRIES.labels(agent, "gateway").inc()
                time.sleep(0.3 * (attempt + 1))
        self.pt += pt
        self.ct += ct
        self.cost += cost
        LLM_TOKENS.labels(agent, task, "prompt").inc(pt)
        LLM_TOKENS.labels(agent, task, "completion").inc(ct)
        LLM_COST.labels(agent, task).inc(cost)
        self.record(agent, "llm", task, {"system_prompt_version": self.prompt_version, "user": user[:4000]},
                    out if ok else {"error": err}, (time.time() - t0) * 1000, ok, pt, ct, cost)
        if not ok:
            self.llm_available = False
            self.degraded = True
            log(logger, "model unavailable; switching to deterministic fallback path", logging.WARNING, error=err)
        return out


def parse_json(text: str):
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None
