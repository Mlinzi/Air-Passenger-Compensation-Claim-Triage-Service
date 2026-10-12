# Air Passenger Compensation Claim Triage Service: Project Report

Mini Project 04 · UE24AM342AA5 ML System Design & AgentOps · Domain: Aviation

## 1. Problem and scope

An airline customer-relations analyst decides what a passenger is owed after denied boarding,
cancellation, delay or downgrading. The amounts come from DGCA CAR Section 3 Series M Part IV.
The amounts themselves are easy to find. The conditions that remove them are scattered around them:
how much notice was given, whether an alternate flight was taken, whether contact details were given,
and whether the cause was beyond the airline's control. Every declined claim must cite its provision,
because the passenger can escalate through AirSewa.

We built a service that accepts a claim over a REST contract, validates it, and finds the governing
provisions *and the provisions that modify them*. It works out the entitlement position and routes the
claim to **pay-as-computed**, **request-further-evidence**, **decline-with-cited-provision** or
**escalate-to-human**, with clause-level citations verified against the retrieved text. It escalates
instead of answering when the cause attribution is disputed, provisions conflict, or the case is outside
the corpus. It never authorises a payment.

**Corpus.** The CAR is the official **Rev. 4 (25 Jan 2023, effective 15 Feb 2023)** PDF from dgca.gov.in,
parsed directly. The airline charter is a **sample** modelled on Indian carriers' charters. The airlines'
own sites could not be fetched by script, so the team must replace it with the chosen airline's
published charter (README §1). All flight data is synthetic.

**Out of scope:** model training, cloud deployment, real booking systems.

## 2. Architecture

```
 browser / Locust ──► api (FastAPI) ──enqueue──► redis (RQ queue, caches, rate limits) ──► agent-worker ×N
   POST /claims        contract validation                                                 LangGraph agent
   GET  /runs/{id}     quarantine, rate limit ──► postgres (runs, per-step trace,           │      │
   web client          results, trace, feedback    quarantine, feedback, live scores) ◄─────┘      │
                                                                                                    │ hand-off
                       indexer (one-shot) ──► volume: Chroma + docstore + manifest ◄── verifier ◄───┘ (HTTP)
                                                        ▲ read by worker and verifier      │
                                                                                           ▼
                                               llm-gateway (stub | OpenAI-compatible: Ollama, hosted)
                                               timeouts · retry+backoff · circuit breaker · tokens/cost
 observability: OTLP ► jaeger · /metrics ► prometheus (DNS discovery of every replica) ► grafana · cadvisor
```

Each box is a container (10 services plus Locust). Full diagram and failure table:
[architecture.md](architecture.md).

| Boundary | Placed for |
|---|---|
| api vs agent-worker | *Scaling and resource profile.* The API is light and latency-sensitive. Workers hold the embedding model (≈580 MB RSS each) and scale with `--scale agent-worker=N`. *Failure isolation:* queued runs survive a worker crash. |
| llm-gateway | *Failure isolation and independent release.* The only component that talks to a provider, so timeouts, retries, the circuit breaker, cost accounting and the stub live in one place. The provider is changed by configuration only. |
| verifier | A separate agent (A2) with its own prompt and its own read-only view of the corpus. It does not trust the research agent's copy of the passages. |
| indexer | Batch lifecycle (runs, then exits). Its success gates worker and verifier start-up. |
| redis / postgres | Ephemeral state (queue, caches, rate limits) vs durable state (traces, quarantine, feedback). |

**Asynchronous execution (A4).** `POST /claims` validates, persists and enqueues, then returns `202 {run_id}`.
Clients poll `GET /runs/{id}`. We chose polling over callbacks because callers sit outside the
deployment: callbacks would need an inbound endpoint, delivery retries and signing, while polling once a
second costs almost nothing against a 10–40 s run. RQ on Redis is the task runner.

## 3. Agent design

**Topology (A1, A2).**
`intake → research ⇄ adjudicate → draft → verify (separate service) → output guard → result`. There are
two loops: *coverage* (adjudication reports a required provision missing → research again with keyword or
by-id lookup) and *verification* (verification fails → research again with a different search strategy).
Any loop limit, verifier outage, unhandled error or detected injection goes to **escalate**.

* **Intake agent:** input guardrails, then classification and extraction. Deterministic extraction always
  runs; the model only fills gaps. Each fact records its provenance (`field:`, `text:`, `model:`). A
  deterministic out-of-scope finding (e.g. baggage) cannot be overridden by the model.
* **Research agent.** It has four tools:
  * `search_corpus`: dense, BM25 or hybrid search.
  * `get_clause`: lookup by identifier.
  * `find_modifiers`: sibling clauses, plus clauses that refer to the clause or its paragraph.
  * `get_claim_field`: the request's own data.

  The model proposes actions. A condition-seeking policy from the versioned prompt artifact queues
  follow-ups derived from what was read: cross-references ("Para 1.4 and Para 1.5"), the modifiers of any
  clause that grants money, and a new query built from the condition sentence just read ("either … or …",
  "no such compensation …").
* **Adjudication:** a checklist keyed to the official clause numbers ([rules.py](../triage/agent/rules.py)).
  A branch fires only if its clause is in the retrieved evidence; otherwise the clause goes back to research.
  This is what makes step 2 of §5 of the brief mandatory rather than hoped-for. The model drafts the
  rationale, but never computes amounts.
* **Verification agent (A3)** checks that:
  1. every citation resolves to a passage retrieved in this run;
  2. that passage matches the canonical corpus;
  3. the quote is a literal span of it;
  4. every clause cited in the rationale was retrieved;
  5. every amount is computed or present in a cited passage;
  6. the modifiers of each entitlement clause were read (for 3.3.2 that means 3.3.1, 3.3.3, 3.3.4, 3.3.5,
     3.6.1 and 3.7.1);
  7. the route is consistent with the entitlements;
  8. a model judgement agrees that each non-numeric statement is supported by its clause.

  On failure it returns the missing provisions, and research retries with another strategy, up to 2
  times, then escalates. An unverified draft is never returned.
* **Bounds:** 60 steps, 45 tool calls and a 90 s wall clock (300 s with a local model), 3 coverage
  rounds, 2 verification retries, a timeout on every tool and model call, and a LangGraph recursion limit.

**Section 4.1 case** (cancelled at the airport 90 min before departure; took an alternate flight 4 h later).
The first search finds the 3.3.2 amounts. The research agent then reads 3.3.2's references and siblings
and finds the condition: the airline shall *either* provide an acceptable alternate flight *or* pay
compensation in addition to a refund. It also follows "Para 3.8.1 (a)".
Result: **decline-with-cited-provision under 3.3.2, with meals and refreshments owed under 3.8.1(a)**,
verified. This was correct on the stub and on llama3.1:8b.

## 4. Design decisions and what was rejected

| Decision | Rejected | Why |
|---|---|---|
| Clause-level chunks with heading path and parsed cross-references | Fixed-size chunks | Conditions stay attached to their clause and references can be followed. Measured in §5: accuracy 100% → 21%. |
| Checklist rules whose branches require their clause in the evidence; the model drafts and judges | Letting the model decide the entitlement | Arithmetic and condition precedence are where models fabricate (INC-4, INC-6). Every route is reproducible and testable offline. |
| Numbers verified deterministically; the model judges only non-numeric statements | Model judges everything | 7–8B judges are unreliable on thresholds ("55 min ≤ 1 hour", "50% for 1500–3500 km"). Amounts are computed from the cited tier and traced by code. |
| Verifier as its own service | Second prompt in the worker | Required by A2. Independent corpus view; failure isolation. |
| Single-process gateway owning the breaker | Breaker per process | INC-1: per-process breakers never opened. |
| Polling + RQ | Callbacks, Celery | Simpler; callers are not reachable. |
| Pattern-based injection and toxicity screen | Detoxify / LLM guard | Runs in every container with no GPU or cost. Weaker recall (§8), mitigated by read-only tools, the checklist, the verifier and the output filter. |

## 5. Evaluation

**Set:** 24 fixed scenarios ([eval/scenarios.jsonl](../eval/scenarios.jsonl)) covering all four categories,
every route, the section 4.1 case, conflicts, disputed causes, a foreign carrier, an injection and an
out-of-corpus claim. Each scenario has an expected route, decisive clauses and supporting passages. It runs
as a batch: `python -m eval.run_eval`.

**Metrics** (TruLens-equivalent, computed deterministically from the run trace; see
[metrics.py](../triage/evaluation/metrics.py)):
* *Groundedness:* the share of citations whose quote is a literal span of a retrieved passage.
* *Context relevance:* the recall of gold supporting clauses in the retrieved set.
* *Faithfulness:* the route is correct AND every decisive clause is cited.
* *Hallucination rate:* the share of citations and amounts not traceable to retrieved text or computed
  entitlements.

### Prompt variants (L2): v2 vs v3, real model (llama3.1:8b via Ollama, official CAR)

v3 differs from v2 only in the verification agent's prompt. It tells the judge that statements may
paraphrase and apply the rule to the claim's facts, and how to read "either A, or B".

| Metric | v2 | v3 | Δ |
|---|---|---|---|
| Route accuracy | 0.583 | **0.917** | **+0.333** |
| Faithfulness | 0.500 | **0.917** | +0.417 |
| Groundedness / Hallucination | 1.000 / 0.000 | 1.000 / 0.000 | 0 |
| Context relevance | 0.917 | 0.917 | 0 |
| Mean latency per run | 32.7 s | **26.5 s** | −6.2 s |
| p95 latency per run | 54.1 s | **36.8 s** | −17.3 s |

**v3 is better, by +33 points of route accuracy (14 → 22 of 24).** Every v2 miss was the v2 judge rejecting
a correct statement, so v2 escalated. It never produced a wrong answer, and hallucination is 0 for both.
Escalating less also made v3 faster, with fewer verification retries.
With the stub model, all of v1, v2 and v3 score 24/24, because the stub's judge always agrees. That is why the
prompt comparison has to be made on a real model. On the stub, v1 (no modifier queries) needs 12.7
steps against 28.5 for v3 and is 37% slower (10.2 s vs 7.4 s), because it relies on repeated coverage rounds.

### Deliberate change (L3): clause-aware → fixed-size chunking (500 chars, 80 overlap)

Same prompt (v3), same 24 scenarios, stub model:

| Metric | Before (clause chunks) | After (fixed chunks) | Δ |
|---|---|---|---|
| Groundedness | 1.000 | 1.000 | 0 |
| Context relevance | 0.917 | 0.729 | −0.188 |
| Faithfulness | 1.000 | 0.083 | −0.917 |
| Hallucination rate | 0.000 | 0.000 | 0 |
| (Route accuracy) | 1.000 | 0.208 | −0.792 |

Fixed chunks cut conditions away from their clauses and break clause addressing. Most runs could not
assemble the provisions their decision needs, so they **escalated rather than guessed**: hallucination
stayed at 0 while usefulness collapsed. Disabling the coverage loop (prompt v1) gives the same result
(accuracy 0.208, context relevance 0.208). Without the second retrieval pass, the agent cannot decide.

**Honesty notes.** Several fixes were made after seeing failures on this set (INC-2, INC-5, INC-6), so it
is not a blind test. A held-out set should be written by someone who has not seen the rules. The two
remaining real-model misses (s16, s17) are the 8B judge rejecting a threshold statement that is in the
clause. I stopped tuning there deliberately, to avoid overfitting the set.

### Live quality (L6)

A 25% sample of live runs is scored automatically. Scores are stored (`GET /reports/quality`) and exported as
`live_quality_score`, which is charted over time on the Grafana dashboard ("Live quality over time").
Live context relevance uses a different definition: cited clauses ÷ retrieved clauses. It is low (≈0.05)
by design, because the agent reads ~30 provisions to cite ~3. Feedback (`POST /runs/{id}/feedback`) is
persisted, aggregated (`GET /feedback/summary`) and charted ("Caller feedback").

## 6. Performance (D6, A7)

**SLO, declared before any load test** ([slo.md](slo.md)): p95 of a complete run (submission to final
result) ≤ 30 s at 10 concurrent analyst clients, with the LLM replaced by the stub (1.5 s per call).

Measured with Locust, 2 min per test, on a 12-core laptop with Docker Desktop:

| Workers | Users | Runs/s | p50 | p95 | Max queue depth | SLO |
|---|---|---|---|---|---|---|
| 1 | 10 | 0.13 | 59 s | 73 s | 9 | ✗ |
| 3 | 5 | 0.30 | 13 s | 25 s | 7 | ✓ |
| **3** | **10** | **0.39** | **23 s** | **27 s** | 7 | **✓ met** |
| 3 | 20 | 0.37 | 46 s | 54 s | 17 | ✗ |
| 3 | 10, repeated claims (cache) | 2.96 | 0.54 s | 7.8 s | — | ✓ |

* **The target is met at the declared load with 3 workers, and fails at 20 users.** With 1 worker it fails at 10.
* **Effect of `--scale agent-worker=3`:** throughput ×3 (0.13 → 0.39 runs/s), p95 73 → 27 s.
* **Bottleneck: worker concurrency, not the provider.** Each worker runs one claim at a time, with about 9 s of
  service time, most of it 4–5 *sequential* model calls. At 20 users the Redis queue grew to 17 while
  gateway p95 stayed flat at 1.95 s (the stub's delay), and throughput did not rise (0.37 vs 0.39). Runs were
  waiting for a worker. Capacity ≈ workers ÷ 9 s, so 20 users need about 6 workers, or fewer sequential model
  calls per run.
* On the real local model (llama3.1:8b, one GPU shared by all workers), a single run takes 17–90 s
  (mean 26.5 s). Ollama serialises requests, so there the model is the bottleneck and scaling workers does not help.

**Caching (D7).** Query embeddings, retrievals (keyed by index version) and whole verified runs (keyed by
claim content, prompt version and index version; PNR and name excluded) are cached in Redis. On repeated
claims, p50 fell from 23 s to 0.54 s and throughput rose ×7.6. A cache hit makes zero model calls, so cost per
run falls in proportion to the hit rate. Degraded or unverified results are never cached. Per-client rate
limiting (30 runs/min) is the cost control against a looping client.

## 7. Cost (L5)

Tokens and cost are recorded at call time for every step (`steps` table) and every run (`runs.total_cost_usd`),
and reported at `GET /reports/cost`. Measured on the real model: **2,009 prompt + 328 completion tokens per
run**. Our local model costs $0, so at an example hosted price ($0.59 / $0.79 per 1M tokens) that is
**≈ $0.14 per 100 runs.** **Largest single cost driver: the adjudicator's drafting call**, which has the largest
prompt (≈530 tokens/call, because it carries the cited passages). The research agent's planning calls come
next: they are the most frequent (one per research round). Verification judgements add about 3 short calls per run.

## 8. Reliability and safety (A5, L4)

* Timeouts on every tool, model and service call. The gateway retries up to 3 times with exponential backoff and
  jitter. A circuit breaker (5 failures) is shared by all callers. Demonstrated on the real provider: two
  failing runs, then `circuit: open`, with later calls short-circuited.
* **Fallback path:** the deterministic intake, research and checklist complete the run. It is marked `degraded`
  and routed to **escalate-to-human with the fallback recommendation attached**, never presented as a
  confident answer.
* Injection: free text is screened before any model sees it, then passed as a delimited untrusted block, and
  tools are read-only. Output filter: no "approved/authorised/paid" language, only the four routes, amounts
  traceable. Toxicity screen on input and output.

## 9. What did not work / limitations

* **The charter is a sample.** Clause references C1.1, C3.2, C5.1 and C6.2 must be remapped to the real charter.
* **The real-model accuracy depends on the model.** qwen2.5:7b could not read "either A, or B" in 3.3.2
  even with prompt help (INC-4). llama3.1:8b does. Two delay scenarios still escalate.
* **SLO only at ≤10 users with 3 workers.** More load needs more workers or fewer sequential model calls (e.g.
  merging intake extraction into the first research plan).
* **cAdvisor on Docker Desktop for Windows** does not label containers. Per-container CPU and memory for our
  services therefore come from each service's own process metrics (Grafana "CPU / Memory per service
  container"). Redis and Postgres are covered only on Linux hosts.
* **Guardrails are pattern-based.** A paraphrased injection can pass the screen. The other layers bound what it
  could change, but it would reach the model.
* **The evaluation set is not blind** (see §5).

## 10. Operations

Runbook for the two most likely failures (provider outage, stuck queue): [runbook.md](runbook.md).
Incident log with six real incidents from development: [incident_log.md](incident_log.md).
