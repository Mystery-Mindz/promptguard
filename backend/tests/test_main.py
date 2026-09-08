from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import config, storage
from app.main import app
from app.schemas import DetectionOutput, Trace, TraceStep

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


# --- Endpoint-level tests for /analyze-trace and /run-agent ---
#
# These go through the real FastAPI routes via TestClient (real request/
# response validation, real routing), not the underlying functions directly.
# compute_drift_and_risk / run_agent are mocked at the app.main import site
# to keep these fast, deterministic, and offline — they make real Gemini
# calls otherwise (paced at 13s/call and rate-limited on the free tier),
# which is what test_drift_engine.py's unit tests already cover separately.


def test_analyze_trace_endpoint_returns_correct_schema_for_valid_trace():
    valid_trace = {
        "trace_id": "trace_endpoint_test",
        "agent_id": "agent_A",
        "original_goal": "Read and summarize today's emails",
        "steps": [
            {
                "step_id": 1,
                "actor": "agent_A",
                "input_text": "Please read my inbox and summarize anything urgent.",
                "input_source": "user",
                "input_provenance": "internal",
                "action": "read_email",
                "action_params": {"folder": "inbox"},
                "timestamp": "2026-09-08T10:00:00Z",
            }
        ],
    }

    with patch("app.main.compute_drift_and_risk", return_value=(0.2, 25)):
        response = client.post("/analyze-trace", json=valid_trace)

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) == 1

    output = body[0]
    assert set(output.keys()) == {
        "trace_id",
        "step_id",
        "drift_score",
        "risk_score",
        "provenance_flag",
        "classification",
        "explanation",
        "decision",
    }
    assert output["trace_id"] == "trace_endpoint_test"
    assert output["step_id"] == 1
    assert output["drift_score"] == 0.2
    assert output["risk_score"] == 25
    assert output["provenance_flag"] == "internal"
    assert output["classification"] == "clean"
    assert output["decision"] == "allow_logged"


def test_analyze_trace_endpoint_returns_422_for_malformed_trace():
    malformed_trace = {"trace_id": "bad_trace"}  # missing agent_id, original_goal, steps

    response = client.post("/analyze-trace", json=malformed_trace)

    assert response.status_code == 422
    assert "detail" in response.json()


def test_run_agent_endpoint_runs_end_to_end_without_error():
    fake_trace = Trace(
        trace_id="trace_run_test",
        agent_id="agent_A",
        original_goal="Read my inbox and summarize anything urgent",
        steps=[
            TraceStep(
                step_id=1,
                actor="agent_A",
                input_text="Read my inbox and summarize anything urgent",
                input_source="user",
                input_provenance="internal",
                action="read_email",
                action_params={"folder": "inbox"},
                timestamp=datetime.now(timezone.utc),
            )
        ],
    )

    with patch("app.main.run_agent", return_value=fake_trace) as mock_run_agent:
        response = client.post(
            "/run-agent",
            json={"original_goal": "Read my inbox and summarize anything urgent"},
        )

    assert response.status_code == 200
    mock_run_agent.assert_called_once()
    body = response.json()
    assert body["trace_id"] == "trace_run_test"
    assert body["agent_id"] == "agent_A"
    assert len(body["steps"]) == 1
    assert body["steps"][0]["action"] == "read_email"
