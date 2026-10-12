"""Agent worker: consumes runs from the Redis queue (RQ) and executes the agent graph.

Scales horizontally: `docker compose up --scale agent-worker=3`. Uses RQ's SimpleWorker (no fork
per job) so the embedding model is loaded once per container and Prometheus metrics persist.
"""
import logging
import random
import time

from prometheus_client import start_http_server
from redis import Redis
from rq import Queue, SimpleWorker

from triage.common import cache, db
from triage.common.metrics import (AGENT_STEPS_PER_RUN, AGENT_TOOL_CALLS_PER_RUN, CACHE, LIVE_QUALITY, RUN_E2E, RUNS,
                                   register_index_freshness, register_uptime)
from triage.common.obs import correlation_id, log, run_id_var, setup_logging, setup_tracing, tracer
from triage.common.settings import get_settings
from triage.evaluation.metrics import score

logger = logging.getLogger("worker")
LIVE_SCORE_SAMPLE = 0.25
RUN_CACHE_EXCLUDE = {"pnr", "passenger_name", "contact_email"}


def run_cache_key(claim: dict, prompt_version: str, index_version: str) -> str:
    return cache.key("run", prompt_version, index_version, {k: v for k, v in claim.items() if k not in RUN_CACHE_EXCLUDE})


def process_run(run_id: str, claim: dict, prompt_version: str, corr: str, inject: str | None = None):
    from triage.agent.graph import run_agent
    from triage.agent.retrieval import get_index
    from triage.agent.runtime import RunContext

    correlation_id.set(corr)
    run_id_var.set(run_id)
    started = time.time()
    db.update_run(run_id, status="running", started_at=started)
    index = get_index()
    ck = run_cache_key(claim, prompt_version, index.version)
    with tracer().start_as_current_span("worker.process_run") as span:
        span.set_attribute("run_id", run_id)
        span.set_attribute("correlation_id", corr)
        cached = None if inject else cache.get_json(ck)
        if cached:
            CACHE.labels("run", "hit").inc()
            result = {**cached, "cache_hit": True}
            ctx = None
        else:
            CACHE.labels("run", "miss").inc()
            ctx = RunContext(run_id, prompt_version)
            result = run_agent(ctx, claim, inject=inject)
            for agent, n in ctx.per_agent_steps.items():
                AGENT_STEPS_PER_RUN.labels(agent).observe(n)
            for agent in ("research", "intake", "verifier", "adjudicator"):
                AGENT_TOOL_CALLS_PER_RUN.labels(agent).observe(ctx.per_agent_tools.get(agent, 0))
            # only verified, non-degraded results are reusable
            if result.get("verified") and not result.get("degraded"):
                cache.set_json(ck, result)
    result["run_id"] = run_id
    status = "completed" if result.get("verified") else "escalated"
    finished = time.time()
    run = db.get_run(run_id)
    e2e = finished - (run["submitted_at"] if run else started)
    db.update_run(run_id, status=status, result=result, finished_at=finished, index_version=index.version,
                  prompt_version=prompt_version, cache_hit=bool(result.get("cache_hit")),
                  total_cost_usd=ctx.cost if ctx else 0.0, prompt_tokens=ctx.pt if ctx else 0,
                  completion_tokens=ctx.ct if ctx else 0)
    RUN_E2E.labels(result.get("route") or "none").observe(e2e)
    RUNS.labels(status, result.get("route") or "none").inc()
    log(logger, "run finished", route=result.get("route"), status=status, e2e_s=round(e2e, 3),
        cost_usd=ctx.cost if ctx else 0, cache_hit=bool(result.get("cache_hit")))
    if ctx and random.random() < LIVE_SCORE_SAMPLE:
        try:
            sc = score(result, ctx.passages)
            db.add_live_score(run_id, sc)
            for m in ("groundedness", "context_relevance", "faithfulness", "hallucination"):
                LIVE_QUALITY.labels(m).observe(sc[m])
        except Exception as e:
            log(logger, "live scoring failed", logging.WARNING, error=str(e))
    return {"run_id": run_id, "status": status}


def main():
    s = get_settings()
    setup_logging("agent-worker", s.log_level)
    setup_tracing("agent-worker", s.otel_endpoint)
    register_uptime("agent-worker")
    register_index_freshness(s.index_dir)
    start_http_server(9100)
    db.init_schema()
    # warm the embedding model and index before taking work
    from triage.agent.retrieval import get_index
    for i in range(60):
        try:
            get_index()
            break
        except FileNotFoundError:
            log(logger, "waiting for index", attempt=i)
            time.sleep(2)
    conn = Redis.from_url(s.redis_url)
    SimpleWorker([Queue("runs", connection=conn)], connection=conn).work(with_scheduler=False)


if __name__ == "__main__":
    main()
