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


def test_approval_decision_rejects_second_call_on_already_decided_step():
    _seed("trace_test", 99, "approval_required")

    first_response = client.post(
        "/approval-decision",
        json={"trace_id": "trace_test", "step_id": 99, "operator_decision": "approved", "operator_id": "reviewer_1"},
    )
    assert first_response.status_code == 200

    second_response = client.post(
        "/approval-decision",
        json={"trace_id": "trace_test", "step_id": 99, "operator_decision": "denied", "operator_id": "reviewer_2"},
    )

    assert second_response.status_code == 409
    detail = second_response.json()["detail"]
    assert "reviewer_1" in detail
    assert "approved" in detail

    # Confirm the original decision was NOT overwritten by the rejected second call.
    unchanged = storage.get_approval_decision("trace_test", 99)
    assert unchanged["operator_decision"] == "approved"
    assert unchanged["operator_id"] == "reviewer_1"


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


def test_pending_approvals_returns_correct_records_and_excludes_decided_ones():
    _seed("trace_pending", 1, "approval_required")  # genuinely pending — should be returned
    _seed("trace_pending", 2, "allow_logged")  # wrong decision — should be excluded
    _seed("trace_decided", 1, "approval_required")  # already decided — should be excluded

    storage.save_approval_decision(
        trace_id="trace_decided",
        step_id=1,
        operator_decision="approved",
        operator_id="reviewer_1",
        final_status="approved_and_allowed",
        timestamp="2026-01-01T00:00:00+00:00",
    )

    response = client.get("/pending-approvals")

    assert response.status_code == 200
    body = response.json()
    pairs = {(item["trace_id"], item["step_id"]) for item in body}

    assert ("trace_pending", 1) in pairs
    assert ("trace_pending", 2) not in pairs
    assert ("trace_decided", 1) not in pairs

    pending_entry = next(item for item in body if item["trace_id"] == "trace_pending")
    assert pending_entry["risk_score"] == 60
    assert pending_entry["provenance_flag"] == "external"
    assert pending_entry["explanation"] == "test fixture"
    assert "timestamp" in pending_entry
