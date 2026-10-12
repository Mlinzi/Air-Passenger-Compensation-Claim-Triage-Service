"""Postgres persistence: runs, per-step trace, quarantine, feedback, live quality scores."""
import json
import time
import uuid
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from .settings import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    correlation_id TEXT,
    client_id TEXT,
    status TEXT NOT NULL,
    claim JSONB NOT NULL,
    result JSONB,
    prompt_version TEXT,
    index_version TEXT,
    cache_hit BOOLEAN DEFAULT FALSE,
    submitted_at DOUBLE PRECISION NOT NULL,
    started_at DOUBLE PRECISION,
    finished_at DOUBLE PRECISION,
    total_cost_usd DOUBLE PRECISION DEFAULT 0,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS steps (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    agent TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    input JSONB,
    output JSONB,
    latency_ms DOUBLE PRECISION,
    prompt_version TEXT,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cost_usd DOUBLE PRECISION DEFAULT 0,
    ok BOOLEAN DEFAULT TRUE,
    ts DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS steps_run ON steps(run_id, seq);
CREATE TABLE IF NOT EXISTS quarantine (
    id TEXT PRIMARY KEY,
    correlation_id TEXT,
    client_id TEXT,
    payload JSONB,
    errors JSONB NOT NULL,
    contract_version TEXT,
    ts DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    satisfactory BOOLEAN NOT NULL,
    reason TEXT NOT NULL,
    ts DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS live_scores (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    groundedness DOUBLE PRECISION,
    context_relevance DOUBLE PRECISION,
    faithfulness DOUBLE PRECISION,
    hallucination DOUBLE PRECISION,
    ts DOUBLE PRECISION NOT NULL
);
"""


@contextmanager
def conn():
    with psycopg.connect(get_settings().database_url, row_factory=dict_row, autocommit=True) as c:
        yield c


def init_schema(retries: int = 30) -> None:
    for i in range(retries):
        try:
            with conn() as c:
                c.execute("SELECT pg_advisory_lock(42)")
                c.execute(SCHEMA)
                c.execute("SELECT pg_advisory_unlock(42)")
            return
        except psycopg.OperationalError:
            time.sleep(1 + i * 0.2)
    raise RuntimeError("database unavailable")


def J(v):
    return json.dumps(v, default=str)


def create_run(run_id, correlation_id, client_id, claim, prompt_version):
    with conn() as c:
        c.execute(
            "INSERT INTO runs(run_id, correlation_id, client_id, status, claim, prompt_version, submitted_at) "
            "VALUES (%s,%s,%s,'queued',%s,%s,%s)",
            (run_id, correlation_id, client_id, J(claim), prompt_version, time.time()),
        )


def update_run(run_id, **fields):
    if not fields:
        return
    cols, vals = [], []
    for k, v in fields.items():
        cols.append(f"{k}=%s")
        vals.append(J(v) if k in ("result", "claim") else v)
    with conn() as c:
        c.execute(f"UPDATE runs SET {', '.join(cols)} WHERE run_id=%s", (*vals, run_id))


def get_run(run_id):
    with conn() as c:
        return c.execute("SELECT * FROM runs WHERE run_id=%s", (run_id,)).fetchone()


def add_step(run_id, seq, agent, kind, name, inp, out, latency_ms, prompt_version=None,
             prompt_tokens=0, completion_tokens=0, cost_usd=0.0, ok=True):
    with conn() as c:
        c.execute(
            "INSERT INTO steps(run_id, seq, agent, kind, name, input, output, latency_ms, prompt_version, "
            "prompt_tokens, completion_tokens, cost_usd, ok, ts) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (run_id, seq, agent, kind, name, J(inp), J(out), latency_ms, prompt_version,
             prompt_tokens, completion_tokens, cost_usd, ok, time.time()),
        )


def get_steps(run_id):
    with conn() as c:
        return c.execute("SELECT * FROM steps WHERE run_id=%s ORDER BY seq, id", (run_id,)).fetchall()


def quarantine(correlation_id, client_id, payload, errors, contract_version):
    qid = "q_" + uuid.uuid4().hex[:12]
    with conn() as c:
        c.execute(
            "INSERT INTO quarantine(id, correlation_id, client_id, payload, errors, contract_version, ts) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (qid, correlation_id, client_id, J(payload), J(errors), contract_version, time.time()),
        )
    return qid


def list_quarantine(limit=50):
    with conn() as c:
        return c.execute("SELECT * FROM quarantine ORDER BY ts DESC LIMIT %s", (limit,)).fetchall()


def add_feedback(run_id, satisfactory, reason):
    with conn() as c:
        c.execute("INSERT INTO feedback(run_id, satisfactory, reason, ts) VALUES (%s,%s,%s,%s)",
                  (run_id, satisfactory, reason, time.time()))


def feedback_summary():
    with conn() as c:
        agg = c.execute(
            "SELECT count(*) AS total, coalesce(sum(case when satisfactory then 1 else 0 end),0) AS satisfactory "
            "FROM feedback").fetchone()
        reasons = c.execute(
            "SELECT reason, count(*) AS n FROM feedback WHERE NOT satisfactory GROUP BY reason ORDER BY n DESC LIMIT 10"
        ).fetchall()
    return {**agg, "top_unsatisfactory_reasons": reasons}


def add_live_score(run_id, s):
    with conn() as c:
        c.execute(
            "INSERT INTO live_scores(run_id, groundedness, context_relevance, faithfulness, hallucination, ts) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (run_id, s["groundedness"], s["context_relevance"], s["faithfulness"], s["hallucination"], time.time()),
        )


def live_scores(limit=200):
    with conn() as c:
        return c.execute("SELECT * FROM live_scores ORDER BY ts DESC LIMIT %s", (limit,)).fetchall()


def cost_report():
    with conn() as c:
        per_run = c.execute(
            "SELECT count(*) AS runs, avg(total_cost_usd) AS avg_cost, avg(prompt_tokens) AS avg_pt, "
            "avg(completion_tokens) AS avg_ct FROM runs WHERE status IN ('completed','escalated')").fetchone()
        drivers = c.execute(
            "SELECT agent, name, sum(cost_usd) AS cost, sum(prompt_tokens) AS pt, sum(completion_tokens) AS ct, "
            "count(*) AS calls FROM steps WHERE kind='llm' OR cost_usd > 0 GROUP BY agent, name ORDER BY cost DESC, pt DESC"
        ).fetchall()
    avg = per_run["avg_cost"] or 0.0
    return {"runs": per_run["runs"], "cost_per_100_runs_usd": round(avg * 100, 6),
            "avg_prompt_tokens": per_run["avg_pt"], "avg_completion_tokens": per_run["avg_ct"],
            "cost_by_step": drivers, "largest_driver": drivers[0] if drivers else None}
