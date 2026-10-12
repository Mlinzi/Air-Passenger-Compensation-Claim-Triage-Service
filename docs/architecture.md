# Architecture

## Deployment view (dashed boxes = deployment boundaries = separate containers)

```
                    ┌──────────────────────────── docker compose ─────────────────────────────┐
  browser / Locust  │                                                                          │
  ───────────────►  ┊ api (FastAPI ×2 procs) ┊──enqueue──►┊ redis ┊◄──BLPOP──┊ agent-worker ×N ┊   │
   POST /claims     ┊ contract validation    ┊            ┊ queue, ┊          ┊ LangGraph agent  ┊   │
   GET  /runs/{id}  ┊ quarantine, rate limit ┊            ┊ caches,┊          ┊ intake/research/ ┊   │
   web client       ┊ results, trace, feedbk ┊            ┊ rl     ┊          ┊ adjudicate/draft ┊   │
                    └──────────┬─────────────┘                               └──┬──────┬──────┬─┘   │
                               │ runs, steps, quarantine, feedback, scores       │      │      │     │
                               ▼                                                 │      │      │     │
                          ┊ postgres ┊◄──────────────────────────────────────────┘      │      │     │
                                                       hand-off (HTTP) ┌────────────────┘      │     │
                                                                       ▼                       ▼     │
                                                          ┊ verifier (agent) ┊───►┊ llm-gateway ┊──►  provider
                                                          ┊ own prompt, own  ┊    ┊ stub | openai┊     (or stub)
                                                          ┊ corpus view      ┊    ┊ retry, CB,   ┊
                                                                   ▲              ┊ cost/tokens  ┊
                         ┊ indexer (one-shot) ┊──writes──► volume: index (Chroma + docstore + manifest)
                                                                   read by worker + verifier
   observability: every service ──OTLP──► jaeger · prometheus ◄── /metrics (api, workers via DNS SD,
                  verifier, gateway, cadvisor) · grafana ◄── prometheus, jaeger
```

## Why the boundaries are where they are

| Boundary | Reason |
|---|---|
| **api** vs **agent-worker** | *Scaling* and *resource profile*. The API is cheap and latency-sensitive. Workers hold the embedding model (~400 MB) and are CPU-heavy, so they scale on their own (`--scale agent-worker=3`). A worker crash never takes the API down (*failure isolation*), and queued runs survive in Redis. |
| **llm-gateway** | *Failure isolation* and *independent release*. It is the only component that talks to the provider: one place for timeouts, retries, the circuit breaker, cost accounting and the stub. The provider is changed by configuration without touching agents. One process per instance, so the breaker state is consistent (see INC-1). |
| **verifier** | A2: an agent with its own responsibility, prompt and corpus view, so it does not trust the research agent's copy of the passages. It is released and scaled independently. If it fails mid-run, the run escalates and nothing unverified is returned. |
| **indexer** | Batch job with a different lifecycle: runs to completion, then exits. Its outcome gates worker startup (`service_completed_successfully`). |
| **redis / postgres** | State. Redis holds the queue, caches and rate-limit counters (ephemeral). Postgres holds durable run traces, quarantine, feedback and scores. |

## Agent topology (A1, A2)

```
intake ──► research ◄────────────┐◄──────────────────────┐
              │ (one tool call   │ missing provisions     │ verification failed:
              │  per step,       │ (coverage loop,        │ new strategy (hybrid/keyword),
              │  loops)          │  keyword / by-id)      │ fetch missing modifiers
              ▼                  │                        │
          adjudicate ────────────┘                        │
              │                                           │
            draft ──► verify (Verification agent service) ┴──► finalize (output guard) ──► result
              │
  any limit / unrecoverable failure / injection ───────────────► escalate ──► result (no answer)
```

* **Intake agent:** guardrail screen (injection text never reaches a model), classification and
  entity extraction. Deterministic extraction always runs; the model only fills gaps, and each fact records its provenance.
* **Research agent:** tools `search_corpus` (dense/keyword/hybrid), `get_clause` (by identifier),
  `find_modifiers` (clauses that refer to a clause or its paragraph) and `get_claim_field` (the request's own data).
  The model proposes actions; a condition-seeking policy from the prompt artifact queues follow-ups:
  cross-references read in the text, modifiers of any clause granting money, and a query built from
  the condition sentence just read ("either … or …", "not payable", "subject to").
* **Adjudication:** the entitlement checklist ([rules.py](../triage/agent/rules.py)). A branch can only
  fire if its clause is among the retrieved passages. Otherwise the clause is reported as missing and
  research runs again. This is step 2 of section 5 of the brief.
* **Verification agent:** see [verifier/main.py](../triage/verifier/main.py) for its 8 checks.
* **Loop bounds:** `AGENT_MAX_STEPS`, `AGENT_MAX_TOOL_CALLS`, `AGENT_WALL_CLOCK_S`, up to 3 coverage
  rounds and up to `AGENT_MAX_VERIFY_RETRIES` verification retries. Every tool call has a timeout.
  LangGraph also has a recursion limit as a backstop.

### What happens if an agent fails mid-run

| Failure | Behaviour |
|---|---|
| Model provider down / circuit open | Run continues on the deterministic path, is marked `degraded`, and the route is forced to `escalate_to_human`, with the fallback recommendation attached for the officer. |
| Tool timeout / exception | Recorded as a failed step. Research continues with the remaining queue; missing evidence becomes a missing provision, then retry, then escalation. |
| Verifier unavailable | Run escalates ("answer could not be verified"). The unverified draft is withheld. |
| Loop limit hit | Escalation with the limit named. |
| Worker process dies | Job lands in RQ's failed registry; the run row stays `running` (runbook §2). |
| Unhandled exception | Caught at `run_agent`; escalation with `degraded: true`. |

## Asynchronous execution (A4)

`POST /claims` returns `202 {run_id}` after validation and enqueueing. Clients poll `GET /runs/{id}`.
**Polling instead of callbacks:** the callers (web client, Locust, analysts' tools) sit outside the
deployment and cannot be assumed reachable. Callbacks would need an inbound endpoint, delivery
retries and request signing, and polling at 0.5–1 s costs almost nothing against a run that takes tens of seconds.
**RQ on Redis** is the task runner between submission and execution. It is simple, inspectable,
and the same Redis also serves caches and rate limiting.

## Data contract (D1)

`GET /contract` publishes JSON Schema for the request, response and feedback, with
`CONTRACT_VERSION`. Validation covers schema and types (pydantic, `extra=forbid`), ranges (block time
15–1200 min, fare 0–1,000,000, distance), categories (enums) and cross-field rules (origin ≠
destination; alternate taken ⇒ offered; alternate departs after scheduled; notification no later than
24 h after departure; downgrading ⇒ flown class lower than booked; delay ⇒ actual departure present).
A rejected payload is written to `quarantine` with field-level errors and returns 422. No run row or
job is created.
