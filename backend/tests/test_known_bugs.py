"""Regression tests for bugs found in the 19 Oct 2026 audit (see docs/BUG_REPORT.md).

Every test here reproduces a confirmed bug, so each is marked
`@known_bug` (strict xfail): CI stays green while the bug exists. When you
FIX a bug, its test starts passing, strict xfail reports that as a failure
("XPASS(strict)"), and you remove `@known_bug` from that test in the same
commit. Never delete or weaken a test to make it pass.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import config, storage
from app.main import app
from app.provenance import get_provenance_flag
from app.schemas import Trace, TraceStep

client = TestClient(app)

known_bug = pytest.mark.xfail(strict=True, reason="Known bug from the 19 Oct audit; see docs/BUG_REPORT.md")


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "test.db"))
    storage.init_db()


def _step(step_id: int, actor: str, provenance: str, action: str, source: str = "user") -> TraceStep:
    return TraceStep(
        step_id=step_id,
        actor=actor,
        input_text=f"text for step {step_id}",
        input_source=source,  # type: ignore[arg-type]
        input_provenance=provenance,  # type: ignore[arg-type]
        action=action,
        action_params={},
        timestamp=datetime(2026, 10, 19, 10, 0, tzinfo=UTC),
    )


# --- BUG-01: re-analysing an existing trace_id keeps stale data -------------


def test_bug01_reanalysis_replaces_old_steps():
    old = Trace(
        trace_id="t",
        agent_id="a",
        original_goal="g",
        steps=[_step(1, "A", "internal", "read_email"), _step(2, "A", "external", "x"), _step(3, "A", "external", "y")],
    )
    new = Trace(
        trace_id="t",
        agent_id="a",
        original_goal="g2",
        steps=[_step(1, "A", "internal", "read_email"), _step(2, "A", "external", "delete_file")],
    )
    storage.save_trace(old)
    storage.save_trace(new)

    stored = storage.get_trace("t")
    assert stored is not None
    assert [s.action for s in stored.steps] == ["read_email", "delete_file"]


def test_bug01_new_flagged_step_does_not_inherit_an_old_approval():
    trace = {
        "trace_id": "reused_id",
        "agent_id": "agent_A",
        "original_goal": "Summarise my inbox",
        "steps": [
            {
                "step_id": 1,
                "actor": "agent_A",
                "input_text": "Summarise my inbox",
                "input_source": "user",
                "input_provenance": "internal",
                "action": "send_message",
                "action_params": {"to": "me"},
                "timestamp": "2026-10-19T10:00:00Z",
            }
        ],
    }
    with patch("app.main.compute_drift_and_risk", return_value=(0.5, 60)):
        assert client.post("/analyze-trace", json=trace).status_code == 200
    approve = {"trace_id": "reused_id", "step_id": 1, "operator_decision": "approved", "operator_id": "op"}
    assert client.post("/approval-decision", json=approve).status_code == 200

    # Same trace_id, a completely different (dangerous) step 1 — e.g. a second
    # /run-agent call using the default trace_id.
    trace["steps"][0]["action"] = "delete_file"
    trace["steps"][0]["action_params"] = {"path": "/documents"}
    with patch("app.main.compute_drift_and_risk", return_value=(0.5, 60)):
        assert client.post("/analyze-trace", json=trace).status_code == 200

    pending = client.get("/pending-approvals").json()
    assert any(p["trace_id"] == "reused_id" and p["step_id"] == 1 for p in pending), (
        "the new delete_file step is silently treated as already approved"
    )


def test_bug01_run_agent_does_not_reuse_one_fixed_trace_id():
    fake = Trace(trace_id="ignored", agent_id="agent_A", original_goal="g", steps=[])
    seen: list[str] = []

    def fake_run_agent(goal: str, trace_id: str, agent_id: str = "agent_A") -> Trace:
        seen.append(trace_id)
        return fake.model_copy(update={"trace_id": trace_id})

    with patch("app.main.run_agent", side_effect=fake_run_agent):
        client.post("/run-agent", json={"original_goal": "a"})
        client.post("/run-agent", json={"original_goal": "b"})

    assert len(set(seen)) == 2, f"two runs without a trace_id got the same id: {seen}"


# --- BUG-02: taint stops after the first step of the receiving agent --------


def test_bug02_taint_persists_for_every_later_step_of_the_receiving_agent():
    trace = Trace(
        trace_id="handoff",
        agent_id="A",
        original_goal="g",
        steps=[
            _step(1, "A", "internal", "read_email"),
            _step(2, "A", "external", "tool_output_received", "tool_output"),
            _step(3, "B", "internal", "summarise", "agent_handoff"),
            _step(4, "B", "internal", "send_message", "agent_handoff"),
            _step(5, "B", "internal", "delete_file", "agent_handoff"),
        ],
    )
    assert get_provenance_flag(trace, 3) == "tainted"
    assert get_provenance_flag(trace, 4) == "tainted", "agent B's send_message is acting on laundered content"
    assert get_provenance_flag(trace, 5) == "tainted", "agent B's delete_file is acting on laundered content"


def test_bug02_handoff_with_no_external_content_upstream_is_not_a_false_positive():
    """A handoff by itself is not suspicious — only a handoff downstream of
    actual external content is. Guards against over-tainting once taint
    flows forward (BUG-02's fix must not turn every handoff into a false
    positive)."""
    trace = Trace(
        trace_id="clean_handoff",
        agent_id="A",
        original_goal="g",
        steps=[
            _step(1, "A", "internal", "read_email"),
            _step(2, "B", "internal", "summarise", "agent_handoff"),
            _step(3, "B", "internal", "send_message", "agent_handoff"),
        ],
    )
    assert get_provenance_flag(trace, 2) == "internal"
    assert get_provenance_flag(trace, 3) == "internal"


# --- BUG-03: bad tool arguments from the model crash /run-agent -------------


@known_bug
def test_bug03_run_agent_survives_tool_call_with_missing_arguments():
    bad_call = MagicMock(args={}, id="call_1")  # send_message requires `to` and `content`
    bad_call.name = "send_message"

    first = MagicMock()
    first.function_calls = [bad_call]
    first.candidates = [MagicMock()]
    second = MagicMock()
    second.function_calls = None

    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = [first, second]

    with patch("app.agent._get_client", return_value=mock_client), patch("app.agent._pace_calls"):
        response = client.post("/run-agent", json={"original_goal": "message the team", "trace_id": "bad_args"})

    assert response.status_code == 200, response.text


# --- BUG-04: a judge/embedding failure mid-trace returns a bare 500 ---------


@known_bug
def test_bug04_model_failure_returns_clean_error_not_500():
    trace = {
        "trace_id": "quota_test",
        "agent_id": "agent_A",
        "original_goal": "g",
        "steps": [
            {
                "step_id": 1,
                "actor": "agent_A",
                "input_text": "hi",
                "input_source": "user",
                "input_provenance": "internal",
                "action": "read_email",
                "action_params": {},
                "timestamp": "2026-10-19T10:00:00Z",
            }
        ],
    }
    with patch("app.main.compute_drift_and_risk", side_effect=RuntimeError("429 RESOURCE_EXHAUSTED")):
        response = TestClient(app, raise_server_exceptions=False).post("/analyze-trace", json=trace)
    assert response.status_code in (502, 503), f"got {response.status_code}; expected a clear upstream-error status"
    assert "detail" in response.json()
