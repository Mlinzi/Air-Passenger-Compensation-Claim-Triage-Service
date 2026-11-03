# Air Passenger Compensation Claim Triage Service

Mini Project 04, UE24AM342AA5 (ML System Design & AgentOps).

A multi-agent service that accepts a passenger's claim for denied boarding, cancellation, delay or
downgrading. It validates the claim, works out the entitlement under DGCA CAR Section 3 Series M
Part IV and the airline's passenger charter, and routes the claim to **pay-as-computed**,
**request-further-evidence**, **decline-with-cited-provision** or **escalate-to-human**. Every
route comes with clause-level citations that have been verified against the retrieved text.

> The system never authorises a payment. It produces a recommendation, and a customer relations officer decides.

---

## 1. Run it (clean clone → working system)

Prerequisites: Docker Desktop (or Docker Engine) **running**, with Compose v2.24+, about 6 GB free disk and 4 GB RAM.
On Windows Git Bash, prefix `docker compose run ... /mnt/...` commands with `MSYS_NO_PATHCONV=1`.
Internet access is needed on the first start, to pull images and download the embedding model once.

```bash
git clone <repo> && cd <repo>
cp .env.example .env          # defaults work as-is: stub model provider, local embeddings
docker compose up --build     # first start: ~5-10 min (image build + model download + indexing)
```

When `api` reports healthy:

| What | Where |
|---|---|
| Web client (submit a claim, see recommendation + citations + escalation path) | http://localhost:8000 |
| API docs (OpenAPI) | http://localhost:8000/docs |
| Published data contract | http://localhost:8000/contract |
| Grafana dashboard "Claim Triage Service" (anonymous view; admin password from `.env`) | http://localhost:3000 |
| Jaeger traces | http://localhost:16686 |
| Prometheus | http://localhost:9090 |

Startup order is enforced by health checks: `redis`, `postgres`, `llm-gateway` → `indexer` (one-shot,
builds the index, then exits 0) → `verifier` → `agent-worker` → `api`.

### Using a real model

The default `.env.example` uses the **stub** provider (fixed responses after a delay), so the system
runs anywhere without a key. To use a real model, set these in `.env` and run `docker compose up -d`:

```
LLM_PROVIDER=openai_compat
LLM_BASE_URL=<OpenAI-compatible base URL, e.g. http://host.docker.internal:11434/v1 for a local Ollama>
LLM_API_KEY=<key; any non-empty value for Ollama>
LLM_MODEL=<model id>
```

Any OpenAI-compatible `/chat/completions` endpoint works. Tested: a local Ollama with an 8B Llama 3.1
model, with `LLM_TIMEOUT_S=90`, `AGENT_WALL_CLOCK_S=300`, `GATEWAY_CALL_TIMEOUT_S=120` and
`VERIFIER_TIMEOUT_S=200` (local inference is slow; see docs/incident_log.md INC-4 for model choice).
For a hosted provider, **set a spending limit on the account first.** No model identifiers, endpoints or
keys are stored in the repository.

### Corpus

| Document | File | Status |
|---|---|---|
| DGCA CAR Section 3 Series M Part IV, **Rev. 4 dated 25 Jan 2023, effective 15 Feb 2023** | `corpus/raw/car_m_iv_rev4_2023.pdf` | Official PDF from dgca.gov.in (has a text layer) |
| Airline passenger charter | `corpus/seed/airline_charter.txt` | **Sample charter** modelled on Indian carriers' charters. Replace with your chosen airline's published charter (see below) |

The indexer parses the official PDF directly. It strips the running headers and footers, splits at clause
numbers (rejecting wrapped lines that merely start with a number), and records each clause's
cross-references ("Para 1.4 and Para 1.5", "para 3.2.2/3.3.2/3.5.1"). The entitlement rules in
`triage/agent/rules.py` use the official Rev. 4 numbering.

To use a real airline charter: save it as `corpus/raw/charter_<airline>.pdf` (a PDF with a text layer, with
numbered clauses `C1.1`-style or `1.1`-style), delete `corpus/seed/airline_charter.txt`, and run
`docker compose run --rm indexer`. Only changed chunks are re-embedded. The rules reference three charter
clauses: precedence (`C1.1`), causes beyond control (`C3.2`) and downgrading (`C5.1`), plus the evidence
clause `C6.2`. Map these to the real charter's numbering in `rules.py`.
An earlier development transcription of the CAR is kept in `corpus/archive/` for reference only; it is not indexed.

---

## 2. Try the demonstration items

The web client at http://localhost:8000 has a drop-down with ready-made examples. From a shell:

```bash
# (2)/(10) the section 4.1 case: cancelled at the airport, took the alternate flight
curl -s -X POST localhost:8000/claims -H 'Content-Type: application/json' -d @eval/examples/section_4_1.json
curl -s localhost:8000/runs/<run_id>            # poll until "result" appears
# (3) full reconstruction of one run: every tool call, query, passage, prompt version, latency, cost, verification
curl -s localhost:8000/runs/<run_id>/trace      # and Jaeger: search service "agent-worker", tag run_id
# (4) malformed request -> 422 with field-level errors, quarantined, no run created
curl -s -X POST localhost:8000/claims -H 'Content-Type: application/json' -d '{"pnr":"bad","origin":"DEL","destination":"DEL"}'
curl -s localhost:8000/quarantine
# (6) self-verification catching an unsupported answer (DEMO_MODE=true only)
curl -s -X POST 'localhost:8000/claims?inject=unsupported_citation' -H 'Content-Type: application/json' -d @eval/examples/section_4_1.json
#     -> trace shows verify: failed (citation 3.9.7 does not resolve ...), further search, verify: passed
# (8) provider outage -> retries, circuit opens, deterministic fallback, escalated (not a confident answer)
curl -s -X POST 'localhost:8000/admin/provider-fault?enable=true'
#     submit two claims; then: curl localhost:8001/health  -> "circuit": "open"
curl -s -X POST 'localhost:8000/admin/provider-fault?enable=false'
```

Items (5) and (7), escalation outside the corpus and prompt injection, are the "Baggage" and
"Prompt injection" examples in the web client.

---

## 3. Evaluation (L2, L3)

24 fixed scenarios are in [eval/scenarios.jsonl](eval/scenarios.jsonl), generated by
[eval/make_scenarios.py](eval/make_scenarios.py). Each has an expected route, the decisive clauses
and the supporting passages. The set includes the section 4.1 case.

```bash
# stub model (fast; default .env)
docker compose run --rm agent-worker python -m eval.run_eval --prompt-version v3 --label v3_stub
# deliberate change (L3): fixed-size chunking
docker compose run --rm agent-worker sh -c "python -m triage.indexer.pipeline --chunking fixed --index-dir /data/index_fixed && python -m eval.run_eval --prompt-version v3 --index-dir /data/index_fixed --label v3_fixed_chunks_stub"
# prompt comparison (L2) on a real model: set LLM_PROVIDER/LLM_MODEL in .env, docker compose up -d, then
docker compose run --rm agent-worker python -m eval.run_eval --prompt-version v3 --label v3_llama31_8b
docker compose run --rm agent-worker python -m eval.run_eval --prompt-version v2 --label v2_llama31_8b
docker compose run --rm agent-worker python -m eval.run_eval --compare eval/results/v2_llama31_8b.json eval/results/v3_llama31_8b.json
# --offline runs without the gateway/verifier services (deterministic path only)
```

Metric definitions are in [triage/evaluation/metrics.py](triage/evaluation/metrics.py). Results and
discussion are in [docs/report.md](docs/report.md).

## 4. Load test (D6, A7)

The SLO was declared before measuring, in [docs/slo.md](docs/slo.md).

```bash
docker compose up -d --scale agent-worker=3
docker compose --profile loadtest run --rm locust -f /mnt/locust/locustfile.py --host http://api:8000 \
   --headless -u 10 -r 2 -t 2m --csv /mnt/locust/results/w3_u10
```

The `full_run` row in `loadtest/results/*_stats.csv` is submission-to-final-result time.

## 5. Operations

* Health: `GET /health` on api (dependency checks), verifier and llm-gateway.
* Metrics: `/metrics` on every service. Prometheus discovers every worker replica through Compose DNS.
* Logs: JSON lines carrying `correlation_id`, `run_id` and `trace_id`, for example `docker compose logs agent-worker | grep <run_id>`.
* Cost: `GET /reports/cost` gives cost per 100 runs and the largest cost driver by step.
* Quality over time: `GET /reports/quality` gives live-scored runs (25% sample); charted on the Grafana panel "Live quality over time". Feedback: `POST /runs/{id}/feedback`, summarised at `GET /feedback/summary`.
* Re-index: `docker compose run --rm indexer` is incremental. `... indexer python -m triage.indexer.pipeline --reembed car_m_iv` re-embeds one document only.
* [Runbook](docs/runbook.md) · [Incident log](docs/incident_log.md) · [Architecture](docs/architecture.md) · [Report](docs/report.md)

## 6. API validation and guardrails

`POST /claims` quarantines malformed JSON and schema-invalid objects, returning `422` with field
errors and no agent run. If rate limiting, persistence, or queueing is unavailable, the API returns
a safe `503` response. Rate limiting uses the server-observed client address; `X-Client-ID` is
honored only when `DEMO_MODE=true` for local load tests. Prompt-injection screening keeps passenger
text untrusted, and output amounts must match computed entitlements; statutory cap references are
accepted only for the existing DGCA caps and when stated as caps/limits.

## 7. Automated tests

Install the development dependencies and run the local, service-mocked tests:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

The API tests mock persistence, rate limiting, and queue operations; they do not require live Redis,
Postgres, an LLM gateway, or a verifier.

## 8. Repository layout

```
triage/api         FastAPI: contract validation, quarantine, rate limit, queueing, results, trace, feedback, web client
triage/agent       LangGraph agent graph, tools, retrieval, rules checklist, RQ worker
triage/verifier    Verification agent service
triage/gateway     LLM gateway: provider abstraction, stub, retries, circuit breaker, token/cost accounting
triage/indexer     Corpus pipeline (parse -> clause chunk -> embed -> Chroma), freshness manifest
triage/guardrails  Injection, toxicity and output filtering
triage/common      Settings, data contract, DB, cache, metrics, logging/tracing
prompts/           Versioned prompt artifacts (v1, v2)
eval/              Scenarios, runner, results
loadtest/          Locust file and results
monitoring/        Prometheus config, Grafana provisioning and dashboard
docs/              Architecture, SLO, report, runbook, incident log
```
