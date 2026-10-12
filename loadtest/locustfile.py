"""Load test (D6). Measures a COMPLETE run -- submission to final result -- not just POST /claims.

Each simulated analyst tool submits a claim, polls until the result is final, and reports the
whole interval as the request named "full_run". The LLM is the gateway's stub provider
(LLM_PROVIDER=stub, STUB_DELAY_S=1.5), so this measures our system, not provider rate limits.

Every user gets its own X-Client-Id so the per-client rate limiter does not distort the test,
and claims are varied (random fare / block time / PNR) so the run cache does not serve them;
set LOCUST_CACHEABLE=1 to measure the cached path instead.
"""
import os
import random
import time
import uuid

from locust import HttpUser, between, events, task

CACHEABLE = os.environ.get("LOCUST_CACHEABLE") == "1"
TEMPLATES = [
    {"disruption_type": "cancellation", "notified_at_h": -1.5,
     "description": "My flight was cancelled. I was already at the airport and was told 90 minutes before departure. The airline put me on a flight four hours later, which I took."},
    {"disruption_type": "cancellation", "notified_at_h": -5, "alternate_flight_offered": False, "alternate_flight_taken": False,
     "contact_details_provided": True,
     "description": "The airline sent an SMS five hours before departure that the flight was cancelled. No alternate flight was offered."},
    {"disruption_type": "denied_boarding", "reported_on_time": True, "alternate_flight_offered": False,
     "alternate_flight_taken": False, "description": "Denied boarding due to overbooking although I checked in on time."},
    {"disruption_type": "delay", "actual_departure_h": 4,
     "description": "The flight was delayed by four hours and I want compensation."},
]


def make_claim():
    from datetime import datetime, timedelta

    t = dict(random.choice(TEMPLATES))
    sched = datetime(2026, 11, 20, 10, 0)
    c = {"pnr": uuid.uuid4().hex[:6].upper(), "passenger_name": "Load Test", "carrier_code": "6E",
         "flight_number": "6E 2041", "origin": "BLR", "destination": "DEL", "scheduled_departure": sched.isoformat()}
    if "notified_at_h" in t:
        c["notified_at"] = (sched + timedelta(hours=t.pop("notified_at_h"))).isoformat()
    if "actual_departure_h" in t:
        c["actual_departure"] = (sched + timedelta(hours=t.pop("actual_departure_h"))).isoformat()
    c.update(t)
    if not CACHEABLE:
        c["block_time_minutes"] = random.randint(45, 240)
        c["ticket_fare_inr"] = random.randint(2500, 20000)
    return c


class Analyst(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        self.client_id = "locust-" + uuid.uuid4().hex[:8]

    @task
    def full_run(self):
        t0 = time.time()
        r = self.client.post("/claims", json=make_claim(), headers={"X-Client-Id": self.client_id}, name="POST /claims")
        if r.status_code != 202:
            events.request.fire(request_type="RUN", name="full_run", response_time=(time.time() - t0) * 1000,
                                response_length=0, exception=Exception(f"submit {r.status_code}"))
            return
        run_id = r.json()["run_id"]
        while time.time() - t0 < 300:
            time.sleep(0.5)
            s = self.client.get(f"/runs/{run_id}", name="GET /runs/{id}")
            if s.ok and s.json().get("result"):
                route = s.json()["result"].get("route")
                events.request.fire(request_type="RUN", name="full_run", response_time=(time.time() - t0) * 1000,
                                    response_length=0, exception=None, context={"route": route})
                return
        events.request.fire(request_type="RUN", name="full_run", response_time=(time.time() - t0) * 1000,
                            response_length=0, exception=Exception("run did not finish in 300 s"))
