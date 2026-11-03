"""Public API (F1, F2, A4, D1, D7, F8, L6).

Asynchronous runs: POST /claims validates the claim at the boundary, persists it, enqueues it on
Redis and returns 202 + run_id immediately; the caller polls GET /runs/{run_id}. Polling was
chosen over callbacks because the callers (the web client, Locust, an analyst's tool) are not
reachable from inside the deployment, and polling needs no inbound endpoint, retry or signing.
"""
import json
import logging
import os
import time
import uuid

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest
from pydantic import ValidationError
from redis import Redis
from rq import Queue

from triage.common import cache, db
from triage.common.contract import CONTRACT_VERSION, ClaimRequest, Feedback, RunResult
from triage.common.metrics import (FEEDBACK, HTTP_LATENCY, HTTP_REQUESTS, QUARANTINED, RATE_LIMITED,
                                   register_index_freshness, register_uptime)
from triage.common.obs import correlation_id, log, run_id_var, setup_logging, setup_tracing
from triage.common.settings import get_settings

s = get_settings()
setup_logging("api", s.log_level)
setup_tracing("api", s.otel_endpoint)
register_uptime("api")
register_index_freshness(s.index_dir)

logger = logging.getLogger("api")
app = FastAPI(title="Air Passenger Claim Triage Service", version=CONTRACT_VERSION,
              description="Accepts passenger compensation claims and returns cited entitlement recommendations. "
                          "The service never authorises payment.")
FastAPIInstrumentor.instrument_app(app, excluded_urls="metrics,health")
STATIC = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
queue = Queue("runs", connection=Redis.from_url(s.redis_url))
QUEUE_DEPTH = Gauge("run_queue_depth", "Runs waiting in the queue")


@app.on_event("startup")
def startup():
    db.init_schema()


@app.middleware("http")
async def observe(request: Request, call_next):
    corr = request.headers.get("X-Correlation-ID") or uuid.uuid4().hex
    token = correlation_id.set(corr)
    run_token = run_id_var.set("-")
    t0 = time.time()
    try:
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("unhandled API request failure")
            response = JSONResponse({"error": "internal_error"}, status_code=500)

        route = request.scope.get("route")
        path = getattr(route, "path", "other")
        HTTP_REQUESTS.labels("api", path, str(response.status_code)).inc()
        HTTP_LATENCY.labels("api", path).observe(time.time() - t0)
        response.headers["X-Correlation-ID"] = corr
        return response
    finally:
        correlation_id.reset(token)
        run_id_var.reset(run_token)


def client_of(request: Request, x_client_id: str | None) -> str:
    if s.demo_mode and isinstance(x_client_id, str):
        client_id = x_client_id.strip()
        if client_id and len(client_id) <= 128:
            return "demo:" + client_id
    return request.client.host if request.client else "anonymous"


def _quarantine_invalid_request(client: str, payload: dict, errors: list[dict]):
    try:
        qid = db.quarantine(
            correlation_id.get(),
            client,
            payload,
            errors,
            CONTRACT_VERSION,
        )
    except Exception:
        logger.exception("failed to persist invalid claim quarantine")
        return JSONResponse({"error": "service_unavailable"}, status_code=503)

    QUARANTINED.inc()
    log(
        logger,
        "claim quarantined; no agent run started",
        quarantine_id=qid,
        field_count=len(errors),
    )
    return JSONResponse(
        {
            "error": "invalid_request",
            "quarantine_id": qid,
            "errors": errors,
            "agent_run_started": False,
        },
        status_code=422,
    )


def _mark_run_failed(run_id: str) -> None:
    try:
        db.update_run(run_id, status="failed", finished_at=time.time())
    except Exception:
        logger.exception("failed to mark run as failed")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/contract")
def contract():
    """Published data contract for POST /claims."""
    return {"version": CONTRACT_VERSION, "request": ClaimRequest.model_json_schema(),
            "response": RunResult.model_json_schema(), "feedback": Feedback.model_json_schema()}


@app.post("/claims", status_code=202)
async def submit(request: Request, x_client_id: str | None = Header(default=None), inject: str | None = None):
    client = client_of(request, x_client_id)
    try:
        allowed, remaining = cache.rate_limit(client)
    except Exception:
        logger.exception("claim rate-limit service unavailable")
        return JSONResponse({"error": "service_unavailable"}, status_code=503)
    if not allowed:
        RATE_LIMITED.inc()
        return JSONResponse({"error": "rate_limited", "detail": f"limit {s.rate_limit_per_min}/min per client"},
                            status_code=429, headers={"Retry-After": "60"})

    raw_body = await request.body()
    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raw = raw_body.decode(errors="replace")[:5000]
        return _quarantine_invalid_request(
            client,
            {"raw": raw},
            [{"field": "body", "msg": "body must contain valid JSON", "type": "json_invalid"}],
        )

    if not isinstance(body, dict):
        raw = raw_body.decode(errors="replace")[:5000]
        return _quarantine_invalid_request(
            client,
            {"raw": raw},
            [{"field": "body", "msg": "body must be a JSON object", "type": "object_required"}],
        )

    try:
        claim = ClaimRequest.model_validate(body)
    except ValidationError as e:
        errors = [
            {
                "field": ".".join(str(x) for x in err["loc"]) or "claim",
                "msg": err["msg"],
                "type": err["type"],
            }
            for err in e.errors(include_input=False)
        ]
        return _quarantine_invalid_request(client, body, errors)

    if inject and not s.demo_mode:
        inject = None
    run_id = "run_" + uuid.uuid4().hex[:16]
    run_id_var.set(run_id)
    payload = claim.model_dump(mode="json")
    try:
        db.create_run(run_id, correlation_id.get(), client, payload, s.prompt_version)
    except Exception:
        logger.exception("failed to persist claim run")
        _mark_run_failed(run_id)
        return JSONResponse({"error": "service_unavailable"}, status_code=503)

    try:
        queue.enqueue(
            "triage.agent.worker.process_run",
            run_id,
            payload,
            s.prompt_version,
            correlation_id.get(),
            inject,
            job_id=run_id,
            job_timeout=int(s.agent_wall_clock_s * 2 + 60),
            result_ttl=3600,
        )
    except Exception:
        logger.exception("failed to enqueue persisted claim run")
        _mark_run_failed(run_id)
        return JSONResponse(
            {"error": "service_unavailable", "run_id": run_id},
            status_code=503,
        )

    log(logger, "run queued", client=client)
    return {"run_id": run_id, "status": "queued", "status_url": f"/runs/{run_id}",
            "rate_limit_remaining": remaining}


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    r = db.get_run(run_id)
    if not r:
        return JSONResponse({"error": "not_found"}, status_code=404)
    out = {"run_id": run_id, "status": r["status"], "submitted_at": r["submitted_at"], "finished_at": r["finished_at"]}
    if r["result"]:
        out["result"] = r["result"]
    return out


@app.get("/runs/{run_id}/trace")
def get_trace(run_id: str):
    """Everything needed to reconstruct a run: every tool call, query, passage, prompt version,
    per-step latency and cost, and the verification outcome."""
    r = db.get_run(run_id)
    if not r:
        return JSONResponse({"error": "not_found"}, status_code=404)
    steps = db.get_steps(run_id)
    return {"run": r, "steps": steps,
            "summary": {"steps": len(steps), "tool_calls": sum(1 for x in steps if x["kind"] == "tool"),
                        "llm_calls": sum(1 for x in steps if x["kind"] == "llm"),
                        "cost_usd": sum(x["cost_usd"] or 0 for x in steps),
                        "verification": [x["output"] for x in steps if x["name"] == "verify"]},
            "jaeger_search": f"search Jaeger for tag run_id={run_id} or correlation_id={r['correlation_id']}"}


@app.post("/runs/{run_id}/feedback")
def feedback(run_id: str, fb: Feedback):
    if not db.get_run(run_id):
        return JSONResponse({"error": "not_found"}, status_code=404)
    db.add_feedback(run_id, fb.satisfactory, fb.reason)
    FEEDBACK.labels(str(fb.satisfactory).lower()).inc()
    return {"ok": True}


@app.get("/feedback/summary")
def feedback_summary():
    return db.feedback_summary()


@app.get("/quarantine")
def quarantine(limit: int = 50):
    return db.list_quarantine(limit)


@app.get("/reports/cost")
def cost():
    return db.cost_report()


@app.get("/reports/quality")
def quality():
    rows = db.live_scores()
    if not rows:
        return {"n": 0, "series": []}
    keys = ("groundedness", "context_relevance", "faithfulness", "hallucination")
    return {"n": len(rows), "mean": {k: sum(r[k] for r in rows) / len(rows) for k in keys}, "series": rows}


@app.post("/admin/provider-fault")
def provider_fault(enable: bool = True):
    """Demo: simulate a model-provider outage to open the circuit breaker."""
    r = httpx.post(f"{s.gateway_url}/admin/fault", params={"enable": enable}, timeout=5)
    return r.json()


@app.get("/health")
def health():
    checks = {}
    try:
        cache.r().ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"fail: {type(e).__name__}"
    try:
        with db.conn() as c:
            c.execute("SELECT 1")
        checks["postgres"] = "ok"
    except Exception as e:
        checks["postgres"] = f"fail: {type(e).__name__}"
    for name, url in (("llm_gateway", s.gateway_url), ("verifier", s.verifier_url)):
        try:
            checks[name] = httpx.get(url + "/health", timeout=2).json()
        except Exception as e:
            checks[name] = f"fail: {type(e).__name__}"
    checks["index_manifest"] = os.path.exists(os.path.join(s.index_dir, "manifest.json"))
    if checks["redis"] == "ok":
        try:
            checks["queue_depth"] = len(queue)
        except Exception as e:
            logger.exception("failed to inspect run queue")
            checks["queue_depth"] = f"fail: {type(e).__name__}"
    else:
        checks["queue_depth"] = None
    healthy = (
        checks["redis"] == "ok"
        and checks["postgres"] == "ok"
        and not (
            isinstance(checks["queue_depth"], str)
            and checks["queue_depth"].startswith("fail:")
        )
    )
    return JSONResponse({"status": "ok" if healthy else "degraded", "checks": checks}, status_code=200 if healthy else 503)


@app.get("/metrics", include_in_schema=False)
def metrics():
    try:
        QUEUE_DEPTH.set(len(queue))
    except Exception:
        logger.exception("failed to collect run queue depth")
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
