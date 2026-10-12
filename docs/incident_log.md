# Incident log

Real failures recorded during development (dates are when they occurred).

## INC-1: Circuit breaker never opened during a provider outage (2026-10-09)

* **Detected by:** demonstration item 8 rehearsal. With the provider fault enabled, runs
  correctly degraded and escalated, but `GET llm-gateway/health` kept reporting `"circuit": "closed"`
  and `llm_circuit_state` stayed 0.
* **Impact:** no wrong answers, because each run's own model client stopped calling after the first failure
  and escalated. But every new run still made 4 provider attempts with backoff before degrading. During
  a real outage that adds latency and wasted calls (and cost on a paid tier). The breaker existed only on paper.
* **Root cause:** the gateway ran under `uvicorn --workers 2`. The breaker and the fault flag are
  per-process memory. The fault toggle reached one process; the health and metrics requests and some
  completion calls reached the other. Each process saw only part of the failures, so neither reached the
  threshold, and the observed state came from whichever process answered.
* **Fix:** the gateway runs a single async process (`--workers 1`). It is I/O-bound, so one event loop
  handles the load, and there is one breaker per gateway instance. Re-tested: a second failing
  run opens the breaker (`llm_circuit_state 2`, `short_circuited` calls counted).
* **Follow-up:** if the gateway is ever scaled out, move the breaker state to Redis so all replicas share it.

## INC-2: Overbooking claims escalated instead of paid (2026-10-09)

* **Detected by:** the evaluation batch (scenarios s12, s14: route `escalate_to_human`, expected decline and pay).
* **Root cause:** the intake extractor read "denied boarding **due to overbooking**" from the passenger's
  own text as an airline-stated cause. The cause classifier did not recognise it, and an unclassified
  cause is (correctly) escalated for judgement under the general test of CAR 1.4 (2.2.5 in the development transcription used at the time).
* **Fix:** overbooking/oversold is classified as an operational (within the airline's control) cause.
  An unclassified cause that came only from the passenger's free text, and not from the airline's stated
  cause field, no longer forces escalation. The provenance of each extracted fact is recorded, which is
  what made this distinction possible.
* **Caveat:** this rule change was made after seeing evaluation failures, so the evaluation set is no
  longer a blind test for these two scenarios. Noted in the report.

## INC-3: Stack would not start: Docker engine not running (2026-10-09)

* **Symptom:** `docker compose build` printed `Image ... Building` and then `failed to connect to the docker
  API at npipe:////./pipe/dockerDesktopLinuxEngine`. A background build appeared to "complete"
  with exit code 0 even though nothing had been built.
* **Root cause:** Docker Desktop was installed per-user and not running. The CLI's exit status hid the failure.
* **Fix / lesson:** README prerequisites now say Docker must be running. Check with
  `docker version --format '{{.Server.Version}}'` (it fails when the engine is down) rather than trusting
  the build's exit code.

## INC-4: Correct answers escalated on a real local model (2026-10-12)

* **Detected by:** the first end-to-end test with a real model (Ollama, qwen2.5:7b, then llama3.1:8b).
* **Symptoms and root causes:**
  1. The section 4.1 case escalated with "model judged statement unsupported by [3.3.2]". qwen2.5:7b
     consistently read "shall provide either an acceptable alternate flight, or compensation in addition
     to a full refund" as requiring both. It did so even with a v3 verifier prompt that explains
     either/or, and with the judge shown only the cited span. Safe (escalated), but wrong.
  2. The Rs 5,000 cancellation claim escalated: the 8B judge disputed "block time 55 min → Rs 5,000"
     against "block time up to and including one hour". Small models are unreliable at threshold arithmetic.
  3. A baggage claim was processed as a "delay": the intake model's category overrode the
     deterministic out-of-scope finding.
* **Fixes:** the model judge now evaluates only non-numeric statements, because amounts are already
  verified deterministically: computed from the retrieved tier and traced to the cited passage. The
  judge sees the cited span rather than the whole passage. A deterministic out-of-scope classification
  can no longer be overridden by the model. Prompt v3 is added (v2 plus a stricter verifier prompt).
  `llama3.1:8b` is the recommended local model; qwen2.5:7b still escalates the 4.1 case.
* **Result (llama3.1:8b, prompt v3):** the 4.1 case, s02 (pay 5,000), s13 (pay 6,000), s18 (refund 7,500),
  s08 (disputed cause → escalate), baggage → escalate, injection → blocked, malformed → quarantined,
  and the injected unsupported citation → caught, re-searched, verified. Runs take 55–100 s each on local
  hardware with one worker, against about 8 s with the stub, so the 30 s SLO applies only with a hosted provider.

## INC-5: Switching to the official CAR broke the entitlement rules (2026-10-12)

* **Detected by:** comparing the official Rev. 4 PDF (dgca.gov.in) with the development transcription
  before re-indexing. A blind swap would have made every rule reference a clause that does not exist.
  The coverage loop would have escalated every claim as "provisions not found". That is safe, but useless.
* **Differences that mattered:** facilities are para 3.8.1, not 3.6.1. Extraordinary circumstances are
  defined in 1.4/1.5 and applied by 3.3.4 and 3.4.4, not 2.2.5/3.7.x. Contact details are 3.3.3,
  block time is 2.9, the denied-boarding timing condition is in the definition 2.6, and foreign carriers
  are 3.6.1. There is **no** clause saying "no compensation for delay": compensation is only referred to
  in 3.2.2 and 3.3.2 (para 3.7.1). And 3.3.2 compensation applies only to passengers informed less than
  24 h before departure. Notice of 24 h to 2 weeks gives an alternate flight or refund only (3.3.1).
  Scenario s06 changed from pay to decline as a result.
* **PDF parsing problems found:** running headers/footers ("CIVIL AVIATION REQUIREMENTS", page numbers)
  were read as text, and wrapped lines starting with a number ("1.5 which could not...", "3.8.1 (a).")
  were read as new clauses, cutting 3.3.3 and 3.4.4 short and losing their cross-references.
* **Fix:** the parser strips the running headers and accepts a new clause only if it is the immediate
  successor of the previous one (first child, next sibling, or next number at an ancestor level). All
  60 clauses of the official CAR then parse with the right cross-references. The rules were rewritten to
  the official numbering. The modifier check now also treats sibling clauses as modifiers, because in the
  real text 3.3.3 and 3.3.4 qualify 3.3.2 without referring to it, which is exactly the difficulty in
  section 3 of the brief.

## INC-6: Real-model verifier rejected correct answers (2026-10-12)

* **Detected by:** the full 24-scenario evaluation on llama3.1:8b. The first run was 18/24, all misses being escalations.
* **Root causes:** (1) basis statements mixed the rule with claim facts ("block time 55 min [2.9]"), so
  the judge correctly found that 2.9 does not mention 55 minutes; (2) the judge saw only the auto-selected
  quote, which for multi-part clauses (3.4.1 a/b/c, 3.5.1 b) was the wrong sub-item; (3) the intake model
  put "cancelled" into the cause field, which was then treated as an unclassifiable airline cause;
  (4) one statement I wrote ("for delays the CAR requires facilities, not compensation", cited to 3.4.1)
  was not actually supported by 3.4.1. The judge was right to reject it.
* **Fixes:** basis statements now state what the clause says, close to its literal text. The judge sees
  the cited span and the full clause. Only the airline-stated cause field counts as an attribution of
  cause. The unsupported statement was removed. Percentages join amounts on the deterministic-only path.
  Prompt v3 tells the judge that claim facts are established elsewhere.
* **Result:** 22/24 with v3 (v2: 14/24). The two remaining misses (s16, s17) are the judge rejecting a
  threshold statement that is in the clause. They escalate safely. I stopped tuning here deliberately: each
  of these fixes was made after seeing failures on this set, and continuing would overfit it.
