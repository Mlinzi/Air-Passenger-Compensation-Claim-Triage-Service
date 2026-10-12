# Service level objective (declared before load testing)

Declared on 2026-10-09, before any Locust run. The only latency figures available at the time were
single, unloaded runs from the evaluation batch (mean 7.9 s, max 24 s including a cold start), all
with the stub provider.

## Objective

**p95 of a complete run — from `POST /claims` to the moment `GET /runs/{id}` returns the final
result — is at most 30 seconds, at a sustained load of 10 concurrent analyst clients, with
availability (non-5xx responses and completed runs) of at least 99%.**

Measured with the LLM replaced by the gateway's stub provider (`STUB_DELAY_S=1.5`, about 4 model
calls per run), so the test measures this system rather than a free tier's rate limits.

## Why 30 seconds (section 1 of the brief)

* The consumer of a run is a customer-relations analyst working a queue of claims, not a passenger
  waiting on a web page. The passenger's own timeline is days: the airline answers the claim and
  the passenger may escalate through AirSewa. Sub-second latency buys nothing.
* An analyst does open each claim and wait for the recommendation before deciding. Past about 30 s,
  analysts switch to another task and come back. The context switch costs more than the wait, so
  30 s is the point where latency starts costing analyst time.
* A run makes several model calls (intake, research planning per round, drafting, verification).
  At about 1.5 s per call on a realistic provider, plus retrieval and verification, an unloaded run
  takes 8–12 s. 30 s at p95 leaves room for queueing under load and one verification retry.
* 10 concurrent clients is about three times the expected peak for a customer-relations team of
  roughly 10 analysts, each submitting a claim every couple of minutes.

## What counts as failure

A run that times out (300 s), an escalation caused by a component failure (verifier unavailable,
loop limit), and any 5xx all count against availability. Correct escalations, where the system
declines to answer, are successful runs.
