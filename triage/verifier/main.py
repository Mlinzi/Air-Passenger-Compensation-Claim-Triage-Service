"""Verification agent (A2, A3). Runs as its own service with its own prompt and its own read-only
view of the corpus, so it does not trust the research agent's copy of the passages.

Checks, in order (any failure => not verified):
 1. every citation resolves to a passage the research agent actually retrieved;
 2. that passage is identical to the canonical corpus text (no altered quotes);
 3. the quoted text is a literal span of that passage;
 4. every clause referenced in the rationale was retrieved;
 5. every amount in the rationale is a computed entitlement or appears in a cited passage;
 6. provisions that modify a cited entitlement clause (clauses referring to it or its paragraph)
    were retrieved -- otherwise a condition may have been missed (returned as missing_refs);
 7. route/entitlement consistency;
 8. (when the model is available) a model judgement that each basis statement is supported by
    its cited passage.
"""
import logging
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from opentelemetry.propagate import extract
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from triage.common.metrics import AGENT_STEP_LATENCY, register_index_freshness, register_uptime
from triage.common.obs import correlation_id, log, run_id_var, setup_logging, setup_tracing, tracer
from triage.common.settings import get_settings
from triage.verifier.checks import verify_payload

s = get_settings()
setup_logging("verifier", s.log_level)
setup_tracing("verifier", s.otel_endpoint)
register_uptime("verifier")
register_index_freshness(s.index_dir)
logger = logging.getLogger("verifier")
app = FastAPI(title="Verification agent")


class VerifyReq(BaseModel):
    run_id: str
    prompt_version: str = "v2"
    draft: dict
    passages: list[dict]
    use_model: bool = True


@app.post("/verify")
def verify(req: VerifyReq, request: Request):
    correlation_id.set(request.headers.get("X-Correlation-ID", "-"))
    run_id_var.set(req.run_id)
    t0 = time.time()
    with tracer().start_as_current_span("verifier.verify", context=extract(dict(request.headers))):
        out = verify_payload(req.model_dump())
    AGENT_STEP_LATENCY.labels("verifier", "verify").observe(time.time() - t0)
    log(logger, "verification", verified=out["verified"], failures=out["failures"][:5])
    return out


@app.get("/health")
def health():
    ok = os.path.exists(os.path.join(s.index_dir, "docstore.json"))
    return {"status": "ok" if ok else "degraded", "index": ok}


@app.get("/metrics")
def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
