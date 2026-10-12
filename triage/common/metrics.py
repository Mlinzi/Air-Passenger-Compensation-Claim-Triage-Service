"""Prometheus metrics shared by every service (A6, D2, D5, L5)."""
import json
import os
import time

from prometheus_client import Counter, Gauge, Histogram

from .obs import START_TIME

LAT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13, 20, 30, 45, 60, 90, 120)

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["service", "route", "status"])
HTTP_LATENCY = Histogram("http_request_seconds", "HTTP latency", ["service", "route"], buckets=LAT_BUCKETS)

AGENT_TASKS = Counter("agent_tasks_total", "Agent task outcomes", ["agent", "outcome"])
AGENT_STEP_LATENCY = Histogram("agent_step_seconds", "Per-step latency", ["agent", "step"], buckets=LAT_BUCKETS)
AGENT_STEPS_PER_RUN = Histogram("agent_steps_per_run", "Steps per run", ["agent"], buckets=(1, 2, 3, 5, 8, 12, 16, 20, 30))
AGENT_TOOL_CALLS_PER_RUN = Histogram("agent_tool_calls_per_run", "Tool calls per run", ["agent"],
                                     buckets=(0, 1, 2, 3, 5, 8, 12, 16, 20, 30))
AGENT_RETRIES = Counter("agent_retries_total", "Retries", ["agent", "kind"])
AGENT_LATENCY = Histogram("agent_seconds", "Latency per agent per run", ["agent"], buckets=LAT_BUCKETS)

RUN_E2E = Histogram("run_end_to_end_seconds", "Submission to final result", ["route"], buckets=LAT_BUCKETS)
RUNS = Counter("runs_total", "Runs finished", ["status", "route"])

LLM_TOKENS = Counter("llm_tokens_total", "Tokens", ["agent", "step", "type"])
LLM_COST = Counter("llm_cost_usd_total", "Cost at call time (USD)", ["agent", "step"])

CACHE = Counter("cache_events_total", "Cache hit/miss", ["cache", "event"])
QUARANTINED = Counter("quarantined_total", "Rejected claims quarantined")
RATE_LIMITED = Counter("rate_limited_total", "Requests rejected by rate limiter")
GUARDRAIL = Counter("guardrail_events_total", "Guardrail triggers", ["kind"])
LIVE_QUALITY = Histogram("live_quality_score", "Automatic quality score of sampled live runs (L6)", ["metric"],
                         buckets=(0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0))
FEEDBACK = Counter("feedback_total", "Caller feedback on results", ["satisfactory"])

UPTIME = Gauge("container_uptime_seconds", "Process uptime", ["service"])

INDEX_BUILT = Gauge("index_built_timestamp_seconds", "When the index was last built")
INDEX_CHUNKS = Gauge("index_chunks", "Chunks in the index")
INDEX_DOCS = Gauge("index_documents", "Documents in the index")
INDEX_THROUGHPUT = Gauge("index_pipeline_chunks_per_second", "Indexing pipeline throughput")
INDEX_AGE = Gauge("index_age_seconds", "Seconds since the index was built")


def register_uptime(service: str) -> None:
    UPTIME.labels(service).set_function(lambda: time.time() - START_TIME)


def register_index_freshness(index_dir: str) -> None:
    path = os.path.join(index_dir, "manifest.json")

    def read(field, default=0.0):
        def f():
            try:
                with open(path) as fh:
                    m = json.load(fh)
                v = m.get(field, default)
                return time.time() - v if field == "_age" else float(v)
            except Exception:
                return default
        return f

    INDEX_BUILT.set_function(read("built_at"))
    INDEX_CHUNKS.set_function(read("chunk_count"))
    INDEX_DOCS.set_function(read("doc_count"))
    INDEX_THROUGHPUT.set_function(read("chunks_per_second"))

    def age():
        try:
            with open(path) as fh:
                return time.time() - json.load(fh)["built_at"]
        except Exception:
            return -1
    INDEX_AGE.set_function(age)
