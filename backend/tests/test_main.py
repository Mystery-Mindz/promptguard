import pytest
from fastapi.testclient import TestClient

from app import config, storage
from app.main import app
from app.schemas import DetectionOutput

client = TestClient(app)


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "test.db"))
    storage.init_db()


def _seed(trace_id: str, step_id: int, decision: str) -> None:
    storage.save_detection_output(
        DetectionOutput(
            trace_id=trace_id,
            step_id=step_id,
            drift_score=0.5,
            risk_score=60,
            provenance_flag="external",
            classification="suspicious",
            explanation="test fixture",
            decision=decision,
        )
    )


def test_approval_decision_succeeds_for_flagged_step():
    _seed("trace_test", 1, "approval_required")

    response = client.post(
        "/approval-decision",
        json={"trace_id": "trace_test", "step_id": 1, "operator_decision": "approved", "operator_id": "reviewer_1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["trace_id"] == "trace_test"
    assert body["step_id"] == 1
    assert body["final_status"] == "approved_and_allowed"


def test_approval_decision_denied_maps_to_denied_and_blocked():
    _seed("trace_test", 2, "approval_required")

    response = client.post(
        "/approval-decision",
        json={"trace_id": "trace_test", "step_id": 2, "operator_decision": "denied"},
    )

    assert response.status_code == 200
    assert response.json()["final_status"] == "denied_and_blocked"


def test_approval_decision_rejects_step_never_analyzed():
    response = client.post(
        "/approval-decision",
        json={"trace_id": "nonexistent_trace", "step_id": 1, "operator_decision": "approved"},
    )

    assert response.status_code == 404


def test_approval_decision_rejects_step_not_flagged_approval_required():
    _seed("trace_test", 3, "allow_logged")

    response = client.post(
        "/approval-decision",
        json={"trace_id": "trace_test", "step_id": 3, "operator_decision": "approved"},
    )

    assert response.status_code == 409
