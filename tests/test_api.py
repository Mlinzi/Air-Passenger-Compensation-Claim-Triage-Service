import json
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from triage.api import main as api


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api.db, "init_schema", lambda: None)
    monkeypatch.setattr(api.cache, "rate_limit", lambda client_id: (True, 29))
    with TestClient(api.app) as test_client:
        yield test_client


@pytest.fixture
def valid_claim():
    return {
        "pnr": "AB1234",
        "passenger_name": "A Passenger",
        "carrier_code": "6E",
        "flight_number": "6E 123",
        "origin": "DEL",
        "destination": "BOM",
        "scheduled_departure": "2026-11-20T10:00:00",
        "description": "My flight was cancelled and no alternate flight was offered.",
    }


def patch_submission(monkeypatch, *, quarantine=None, create_run=None, enqueue=None):
    monkeypatch.setattr(
        api.db,
        "quarantine",
        quarantine or (lambda *args: "q_test"),
    )
    monkeypatch.setattr(api.db, "create_run", create_run or (lambda *args: None))
    monkeypatch.setattr(api.db, "update_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(api.queue, "enqueue", enqueue or (lambda *args, **kwargs: None))


def test_valid_claim_is_persisted_and_queued(client, valid_claim, monkeypatch):
    created = []
    enqueued = []
    patch_submission(
        monkeypatch,
        create_run=lambda *args: created.append(args),
        enqueue=lambda *args, **kwargs: enqueued.append((args, kwargs)),
    )

    response = client.post("/claims", json=valid_claim)

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "queued"
    assert data["run_id"].startswith("run_")
    assert data["status_url"] == f"/runs/{data['run_id']}"
    assert len(created) == len(enqueued) == 1


@pytest.mark.parametrize(
    "raw_body",
    [b"", b'{"pnr":'],
)
def test_empty_or_malformed_json_is_quarantined_without_starting_run(
    client, raw_body, monkeypatch
):
    quarantined = []
    created = []
    enqueued = []
    patch_submission(
        monkeypatch,
        quarantine=lambda *args: quarantined.append(args) or "q_test",
        create_run=lambda *args: created.append(args),
        enqueue=lambda *args, **kwargs: enqueued.append(args),
    )

    response = client.post(
        "/claims",
        content=raw_body,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"
    assert response.json()["agent_run_started"] is False
    assert len(quarantined) == 1
    assert not created and not enqueued


@pytest.mark.parametrize("body", [[], "claim", 42, None])
def test_non_object_json_is_quarantined(client, body, monkeypatch):
    quarantined = []
    patch_submission(monkeypatch, quarantine=lambda *args: quarantined.append(args) or "q_test")

    response = client.post("/claims", json=body)

    assert response.status_code == 422
    assert response.json()["errors"][0]["field"] == "body"
    assert response.json()["agent_run_started"] is False
    assert len(quarantined) == 1


def test_missing_required_fields_are_reported_and_quarantined(client, monkeypatch):
    quarantined = []
    patch_submission(monkeypatch, quarantine=lambda *args: quarantined.append(args) or "q_test")

    response = client.post("/claims", json={"description": "A long enough claim description."})

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"
    assert "pnr" in {error["field"] for error in response.json()["errors"]}
    assert response.json()["agent_run_started"] is False
    assert len(quarantined) == 1


def test_invalid_field_value_is_reported_without_echoing_input(client, monkeypatch):
    quarantined = []
    patch_submission(monkeypatch, quarantine=lambda *args: quarantined.append(args) or "q_test")

    response = client.post(
        "/claims",
        json={
            "pnr": "AB1234",
            "passenger_name": "A Passenger",
            "carrier_code": "6E",
            "flight_number": "6E 123",
            "origin": "NOT-AN-IATA-CODE",
            "destination": "BOM",
            "scheduled_departure": "2026-11-20T10:00:00",
            "description": "My flight was cancelled and no alternate flight was offered.",
        },
    )

    assert response.status_code == 422
    assert any(error["field"] == "origin" for error in response.json()["errors"])
    assert "NOT-AN-IATA-CODE" not in json.dumps(response.json())
    assert response.json()["agent_run_started"] is False
    assert len(quarantined) == 1


def test_invalid_field_type_is_reported_and_quarantined(client, monkeypatch):
    quarantined = []
    patch_submission(monkeypatch, quarantine=lambda *args: quarantined.append(args) or "q_test")

    response = client.post(
        "/claims",
        json={
            "pnr": "AB1234",
            "passenger_name": "A Passenger",
            "carrier_code": "6E",
            "flight_number": "6E 123",
            "origin": "DEL",
            "destination": "BOM",
            "scheduled_departure": "2026-11-20T10:00:00",
            "ticket_fare_inr": "not-a-number",
            "description": "My flight was cancelled and no alternate flight was offered.",
        },
    )

    assert response.status_code == 422
    assert any(error["field"] == "ticket_fare_inr" for error in response.json()["errors"])
    assert response.json()["agent_run_started"] is False
    assert len(quarantined) == 1


def test_rate_limit_response_has_retry_after_and_skips_quarantine(client, monkeypatch):
    monkeypatch.setattr(api.cache, "rate_limit", lambda client_id: (False, 0))
    quarantine_calls = []
    patch_submission(monkeypatch, quarantine=lambda *args: quarantine_calls.append(args))

    response = client.post("/claims", json={})

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert response.json()["error"] == "rate_limited"
    assert not quarantine_calls


def test_rate_limiter_failure_returns_service_unavailable(client, monkeypatch):
    monkeypatch.setattr(api.cache, "rate_limit", lambda client_id: (_ for _ in ()).throw(ConnectionError()))

    response = client.post("/claims", json={})

    assert response.status_code == 503
    assert response.json() == {"error": "service_unavailable"}


def test_missing_run_id_returns_not_found(client, monkeypatch):
    monkeypatch.setattr(api.db, "get_run", lambda run_id: None)

    response = client.get("/runs/run_missing")

    assert response.status_code == 404
    assert response.json() == {"error": "not_found"}


def test_untrusted_client_id_header_does_not_change_rate_limit_identity(
    client, valid_claim, monkeypatch
):
    observed = []
    monkeypatch.setattr(api.s, "demo_mode", False)
    monkeypatch.setattr(api.cache, "rate_limit", lambda client_id: observed.append(client_id) or (True, 29))
    patch_submission(monkeypatch)

    first = client.post("/claims", json=valid_claim, headers={"X-Client-ID": "attacker-1"})
    second = client.post("/claims", json=valid_claim, headers={"X-Client-ID": "attacker-2"})

    assert first.status_code == second.status_code == 202
    assert observed == ["testclient", "testclient"]


def test_demo_client_id_is_preserved_for_load_tests(client, valid_claim, monkeypatch):
    observed = []
    monkeypatch.setattr(api.s, "demo_mode", True)
    monkeypatch.setattr(api.cache, "rate_limit", lambda client_id: observed.append(client_id) or (True, 29))
    patch_submission(monkeypatch)

    response = client.post(
        "/claims",
        json=valid_claim,
        headers={"X-Client-ID": "locust-user"},
    )

    assert response.status_code == 202
    assert observed == ["demo:locust-user"]


def test_database_failure_does_not_enqueue_claim(client, valid_claim, monkeypatch):
    enqueued = []
    updates = []
    patch_submission(
        monkeypatch,
        create_run=lambda *args: (_ for _ in ()).throw(ConnectionError()),
        enqueue=lambda *args, **kwargs: enqueued.append(args),
    )
    monkeypatch.setattr(
        api.db,
        "update_run",
        lambda run_id, **kwargs: updates.append((run_id, kwargs)),
    )

    response = client.post("/claims", json=valid_claim)

    assert response.status_code == 503
    assert response.json() == {"error": "service_unavailable"}
    assert not enqueued
    assert updates[0][1]["status"] == "failed"


def test_enqueue_failure_marks_persisted_run_failed(client, valid_claim, monkeypatch):
    updates = []
    patch_submission(
        monkeypatch,
        enqueue=lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError()),
    )
    monkeypatch.setattr(
        api.db,
        "update_run",
        lambda run_id, **kwargs: updates.append((run_id, kwargs)),
    )

    response = client.post("/claims", json=valid_claim)

    assert response.status_code == 503
    assert response.json()["error"] == "service_unavailable"
    assert response.json()["run_id"].startswith("run_")
    assert updates[0][1]["status"] == "failed"


def test_quarantine_persistence_failure_returns_service_unavailable(client, monkeypatch):
    patch_submission(
        monkeypatch,
        quarantine=lambda *args: (_ for _ in ()).throw(ConnectionError()),
    )

    response = client.post("/claims", json={})

    assert response.status_code == 503
    assert response.json() == {"error": "service_unavailable"}


def test_unhandled_endpoint_error_has_safe_response_and_correlation_id(
    client, monkeypatch
):
    monkeypatch.setattr(
        api.db,
        "get_run",
        lambda run_id: (_ for _ in ()).throw(RuntimeError("internal detail")),
    )

    response = client.get(
        "/runs/run_broken",
        headers={"X-Correlation-ID": "test-correlation-id"},
    )

    assert response.status_code == 500
    assert response.json() == {"error": "internal_error"}
    assert response.headers["X-Correlation-ID"] == "test-correlation-id"


def test_health_reports_queue_failure_as_degraded(client, monkeypatch):
    class GatewayResponse:
        def json(self):
            return {"status": "ok"}

    class RedisProbe:
        def ping(self):
            return True

    class DatabaseProbe:
        def execute(self, query):
            return None

    class BrokenQueue:
        def __len__(self):
            raise ConnectionError()

    @contextmanager
    def database_connection():
        yield DatabaseProbe()

    monkeypatch.setattr(api.cache, "r", lambda: RedisProbe())
    monkeypatch.setattr(api.db, "conn", database_connection)
    monkeypatch.setattr(api, "queue", BrokenQueue())
    monkeypatch.setattr(api.httpx, "get", lambda *args, **kwargs: GatewayResponse())

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["checks"]["queue_depth"] == "fail: ConnectionError"
