"""Tests for promptguard_client, entirely against httpx.MockTransport --
no real backend process, no live API calls of any kind."""

import json

import httpx
import pytest

from promptguard_client import (
    ApprovalPending,
    ApprovalStatus,
    PromptGuard,
    PromptGuardAPIError,
    ToolBlocked,
    ToolDenied,
)

SESSION_ID = "session_test"


def _detection_json(decision: str, *, step_id: int = 1, risk_score: int = 50, approval_status: str | None = None) -> dict:
    return {
        "trace_id": SESSION_ID,
        "step_id": step_id,
        "drift_score": 0.1,
        "risk_score": risk_score,
        "provenance_flag": "internal",
        "classification": "clean",
        "explanation": f"test fixture: {decision}",
        "decision": decision,
        "approval_status": approval_status if approval_status is not None else ("pending" if decision == "approval_required" else "not_required"),
    }


def _make_transport(
    *,
    check_decision: str = "allow_logged",
    approval_sequence: tuple[str, ...] = (),
    captured_requests: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """A minimal fake /guard/* backend. `approval_sequence` is consumed one
    status per GET /guard/approval call; once exhausted, repeats its last
    entry (so a test can assert a timeout by giving a sequence that never
    reaches "approved"/"denied")."""
    approval_iter = iter(approval_sequence)
    last_approval_status = approval_sequence[0] if approval_sequence else "pending"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal last_approval_status
        if captured_requests is not None:
            captured_requests.append(request)

        path = request.url.path
        if path == "/guard/session" and request.method == "POST":
            return httpx.Response(200, json={"session_id": SESSION_ID})

        if path == "/guard/check" and request.method == "POST":
            return httpx.Response(200, json=_detection_json(check_decision))

        if path == "/guard/observe" and request.method == "POST":
            return httpx.Response(200, json=_detection_json("allow_logged"))

        if path == f"/guard/approval/{SESSION_ID}/1" and request.method == "GET":
            try:
                last_approval_status = next(approval_iter)
            except StopIteration:
                pass
            decided = last_approval_status != "pending"
            return httpx.Response(
                200,
                json={
                    "status": last_approval_status,
                    "decided_by": "operator_jane" if decided else None,
                    "decided_at": "2026-01-01T00:00:00+00:00" if decided else None,
                },
            )

        return httpx.Response(404, json={"detail": f"unhandled path in test transport: {path}"})

    return httpx.MockTransport(handler)


def _guard(transport: httpx.MockTransport, **kwargs) -> PromptGuard:
    return PromptGuard("https://pg.test", "test-api-key", transport=transport, **kwargs)


def test_start_posts_session_and_sends_api_key_header():
    captured: list[httpx.Request] = []
    guard = _guard(_make_transport(captured_requests=captured))

    session = guard.start(agent_id="assistant", goal="summarize my inbox")

    assert session.session_id == SESSION_ID
    request = captured[0]
    assert request.url.path == "/guard/session"
    assert json.loads(request.content) == {"agent_id": "assistant", "original_goal": "summarize my inbox"}
    assert request.headers["x-promptguard-key"] == "test-api-key"


def test_low_level_check_does_not_raise_or_wait_regardless_of_decision():
    guard = _guard(_make_transport(check_decision="block"))
    session = guard.start(agent_id="assistant", goal="goal")

    result = session.check("agent_A", "delete_file", {"path": "/x"}, "delete it", "tool_output", "external")

    assert result.decision == "block"
    assert result.approval_status == "not_required"


def test_observe_calls_endpoint_and_returns_detection():
    captured: list[httpx.Request] = []
    guard = _guard(_make_transport(captured_requests=captured))
    session = guard.start(agent_id="assistant", goal="goal")

    detection = session.observe("agent_A", "a suspicious email", "tool_output", "external")

    assert detection.decision == "allow_logged"
    observe_request = next(r for r in captured if r.url.path == "/guard/observe")
    assert json.loads(observe_request.content) == {
        "session_id": SESSION_ID,
        "actor": "agent_A",
        "input_text": "a suspicious email",
        "input_source": "tool_output",
        "input_provenance": "external",
    }


def test_guarded_tool_allow_logged_runs_the_tool():
    guard = _guard(_make_transport(check_decision="allow_logged"))
    session = guard.start(agent_id="assistant", goal="goal")
    calls = []

    @session.guarded_tool(actor="agent_A")
    def read_email(folder: str) -> str:
        calls.append(folder)
        return "ok"

    result = read_email(folder="inbox", input_text="read my inbox", input_source="user", input_provenance="internal")

    assert result == "ok"
    assert calls == ["inbox"]


def test_guarded_tool_block_raises_tool_blocked_and_does_not_run():
    guard = _guard(_make_transport(check_decision="block"))
    session = guard.start(agent_id="assistant", goal="goal")
    calls = []

    @session.guarded_tool(actor="agent_A", destructive=True)
    def delete_file(path: str) -> None:
        calls.append(path)

    with pytest.raises(ToolBlocked) as excinfo:
        delete_file(path="/documents", input_text="delete everything", input_source="tool_output", input_provenance="external")

    assert excinfo.value.detection.decision == "block"
    assert calls == []  # never ran


def test_guarded_tool_approval_required_wait_true_polls_until_approved_then_runs():
    guard = _guard(
        _make_transport(check_decision="approval_required", approval_sequence=("pending", "pending", "approved"))
    )
    session = guard.start(agent_id="assistant", goal="goal")
    calls = []

    @session.guarded_tool(actor="agent_A", poll_interval=0.01, timeout=1.0)
    def send_message(to: str, content: str) -> None:
        calls.append((to, content))

    result = send_message(
        to="a@b.com",
        content="hi",
        input_text="forward this",
        input_source="tool_output",
        input_provenance="external",
    )
    assert result is None
    assert calls == [("a@b.com", "hi")]


def test_guarded_tool_approval_required_denied_raises_tool_denied():
    guard = _guard(_make_transport(check_decision="approval_required", approval_sequence=("denied",)))
    session = guard.start(agent_id="assistant", goal="goal")
    calls = []

    @session.guarded_tool(actor="agent_A", poll_interval=0.01, timeout=1.0)
    def send_message(to: str) -> None:
        calls.append(to)

    with pytest.raises(ToolDenied) as excinfo:
        send_message(to="a@b.com", input_text="x", input_source="user", input_provenance="internal")

    assert excinfo.value.approval.status == "denied"
    assert calls == []


def test_guarded_tool_approval_required_times_out_raises_tool_denied():
    guard = _guard(_make_transport(check_decision="approval_required", approval_sequence=("pending",)))
    session = guard.start(agent_id="assistant", goal="goal")
    calls = []

    @session.guarded_tool(actor="agent_A", poll_interval=0.01, timeout=0.03)
    def send_message(to: str) -> None:
        calls.append(to)

    with pytest.raises(ToolDenied) as excinfo:
        send_message(to="a@b.com", input_text="x", input_source="user", input_provenance="internal")

    assert excinfo.value.approval.status == "pending"  # timed out, not denied
    assert calls == []


def test_guarded_tool_wait_false_raises_approval_pending_immediately():
    captured: list[httpx.Request] = []
    guard = _guard(
        _make_transport(check_decision="approval_required", captured_requests=captured, approval_sequence=("pending",))
    )
    session = guard.start(agent_id="assistant", goal="goal")

    @session.guarded_tool(actor="agent_A", wait=False)
    def send_message(to: str) -> None:
        pass

    with pytest.raises(ApprovalPending) as excinfo:
        send_message(to="a@b.com", input_text="x", input_source="user", input_provenance="internal")

    assert excinfo.value.step_id == 1
    assert excinfo.value.session_id == SESSION_ID
    # No polling happened -- only the one /guard/check call, nothing to /guard/approval/.
    assert not any(r.url.path.startswith("/guard/approval/") for r in captured)


def test_guarded_tool_resume_after_wait_false_via_session_approval():
    guard = _guard(_make_transport(check_decision="approval_required", approval_sequence=("approved",)))
    session = guard.start(agent_id="assistant", goal="goal")

    @session.guarded_tool(actor="agent_A", wait=False)
    def send_message(to: str) -> None:
        pass

    with pytest.raises(ApprovalPending) as excinfo:
        send_message(to="a@b.com", input_text="x", input_source="user", input_provenance="internal")

    # A web app would store excinfo.value.step_id and poll later -- simulate "later" here.
    status = session.approval(excinfo.value.step_id)
    assert status.status == "approved"


def test_enforce_false_still_checks_but_always_runs_the_tool():
    guard = _guard(_make_transport(check_decision="block"), enforce=False)
    session = guard.start(agent_id="assistant", goal="goal")
    calls = []

    @session.guarded_tool(actor="agent_A", destructive=True)
    def send_message(to: str) -> str:
        calls.append(to)
        return "sent"

    # The honest "guard OFF" comparison: PromptGuard's decision was real
    # (block), but enforce=False means it's logged, not acted on.
    result = send_message(to="attacker@evil.com", input_text="forward this", input_source="tool_output", input_provenance="external")

    assert result == "sent"
    assert calls == ["attacker@evil.com"]  # ran anyway, despite "block"


def test_guarded_tool_requires_an_actor():
    guard = _guard(_make_transport())
    session = guard.start(agent_id="assistant", goal="goal")  # no actor= here

    @session.guarded_tool()  # no actor= here either
    def read_email(folder: str) -> None:
        pass

    with pytest.raises(TypeError, match="no actor"):
        read_email(folder="inbox", input_text="x", input_source="user", input_provenance="internal")


def test_guarded_tool_per_call_actor_overrides_decorator_default():
    captured: list[httpx.Request] = []
    guard = _guard(_make_transport(captured_requests=captured))
    session = guard.start(agent_id="assistant", goal="goal")

    @session.guarded_tool(actor="default_actor")
    def read_email(folder: str) -> None:
        pass

    read_email(folder="inbox", input_text="x", input_source="user", input_provenance="internal", actor="agent_B")

    check_request = next(r for r in captured if r.url.path == "/guard/check")
    assert json.loads(check_request.content)["actor"] == "agent_B"


def test_api_error_on_unknown_session_raises_promptguard_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "no session found"})

    guard = PromptGuard("https://pg.test", "k", transport=httpx.MockTransport(handler))

    with pytest.raises(PromptGuardAPIError) as excinfo:
        guard.start(agent_id="assistant", goal="goal")

    assert excinfo.value.status_code == 404
    assert "no session found" in excinfo.value.detail


def test_wait_for_approval_returns_directly_without_decorator():
    guard = _guard(_make_transport(approval_sequence=("pending", "approved")))
    session = guard.start(agent_id="assistant", goal="goal")

    status = session.wait_for_approval(1, timeout=1.0, poll_interval=0.01)

    assert isinstance(status, ApprovalStatus)
    assert status.status == "approved"
    assert status.decided_by == "operator_jane"
