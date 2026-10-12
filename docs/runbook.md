# Runbook

These are the two most likely failure modes, judged by how many external dependencies a run has
and by what actually failed during development.

## 1. Model provider unavailable or rate-limited (circuit breaker open)

**Symptom**
* New results arrive with `route: escalate_to_human`, `degraded: true`, and
  `escalation_reason: "model provider unavailable; deterministic fallback recommendation attached..."`.
* Grafana: *Circuit breaker state* = 2 (open); *Retry rate* spikes; escalation ratio rises.
* `curl localhost:8001/health` → `"circuit": "open"`.

**Confirm**
```bash
docker compose logs llm-gateway | grep -E "provider call failed|circuit OPEN" | tail
curl -s localhost:8001/metrics | grep -E "llm_calls_total|llm_circuit_state"
```
* `provider status 429` means a free-tier rate limit. `401`/`403` means a bad key, which is not retryable, so check `.env`.
  A `transport: ConnectTimeout` means a network or provider outage.
* If a demo fault was left on: `curl -X POST 'localhost:8000/admin/provider-fault?enable=false'`.

**Recover**
1. 429: lower the load. Per-client rate limiting (`RATE_LIMIT_PER_MIN`) and the run cache are the levers,
   or move to a higher tier. The breaker half-opens after `BREAKER_RESET_S` and closes on the first success.
2. Bad credentials: fix `.env`, then `docker compose up -d llm-gateway`.
3. Extended outage: switch providers by configuration only (`LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`),
   then `docker compose up -d llm-gateway`.
4. Runs completed while degraded were escalated to humans with a fallback recommendation attached. No
   wrong answers were issued, so nothing needs recalling. Re-submit them if a model-verified result is wanted.

## 2. Runs stuck in `queued` (workers down, index missing, or queue backlog)

**Symptom**
* `GET /runs/{id}` stays `queued`. The web client times out while polling.
* Grafana: *Queue depth* climbing, *Throughput* falling to 0, `up{job="agent-worker"}` = 0, or
  end-to-end p95 well above the 30 s SLO.

**Confirm**
```bash
docker compose ps agent-worker indexer verifier
docker compose logs --tail 50 agent-worker      # "waiting for index", tracebacks, OOM
curl -s localhost:8000/health | python -m json.tool   # queue_depth, index_manifest
docker stats --no-stream                         # memory: each worker holds the embedding model (~400 MB)
```
* `indexer` exited non-zero: the index was never built, so workers wait and the verifier reports `degraded`.
* Workers restarting in a loop with exit code 137: out of memory, so lower the replica count or raise Docker's memory.
* Workers healthy but queue growing: offered load is above capacity (see the load-test report for the per-worker ceiling).

**Recover**
1. Index missing or corrupt: `docker compose run --rm indexer`, then `docker compose restart agent-worker verifier`.
2. Workers down: `docker compose up -d agent-worker`. RQ moves jobs that were running when a worker
   died to its failed registry. Re-submit those claims (their `runs` row stays `running`; find them with
   `SELECT run_id FROM runs WHERE status='running' AND started_at < extract(epoch from now()) - 300`).
3. Backlog: `docker compose up -d --scale agent-worker=3`. Scaling past the point where the gateway or
   CPU saturates does not help; see the bottleneck analysis in the report.
