import os

from fastapi.testclient import TestClient

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from triage.api import main
from triage.common.contract import CONTRACT_VERSION


def test_contract_endpoint_publishes_request_and_response_schemas():
    response = TestClient(main.app).get("/contract")

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == CONTRACT_VERSION
    assert "pnr" in body["request"]["properties"]
    assert "route" in body["response"]["properties"]


def test_invalid_claim_is_quarantined_without_starting_agent(monkeypatch):
    monkeypatch.setattr(main.cache, "rate_limit", lambda client: (True, 19))
    quarantined = []
    monkeypatch.setattr(main.db, "quarantine", lambda *args: quarantined.append(args) or "q_test")

    response = TestClient(main.app).post("/claims", json={"pnr": "bad"})

    assert response.status_code == 422
    assert response.json()["quarantine_id"] == "q_test"
    assert response.json()["agent_run_started"] is False
    assert quarantined

