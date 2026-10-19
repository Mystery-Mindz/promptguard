"""The /guard/* live guard API (see docs/plan/00_OVERVIEW.md, section A).

This is a working skeleton for Day 1 of the 7-day plan: `GUARD_MODE=mock`
gives deterministic, LLM-free scoring so P2 (the Protected Assistant) can
integrate against a real contract today, with sessions/approvals stored
for real so the full approval loop works end to end. The real
drift+judge+provenance pipeline lands on Day 3 (Prompt 3.1); until then,
any `GUARD_MODE` other than `"mock"` (including unset) returns 501.
"""

import os
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException

from app import storage
from app.gate import classification_for
from app.provenance import get_provenance_flag
from app.schemas import (
    Decision,
    DetectionOutput,
    GuardApprovalResponse,
    GuardApprovalState,
    GuardApprovalStatus,
    GuardCheckRequest,
    GuardCheckResponse,
    GuardObserveRequest,
    GuardSessionRequest,
    GuardSessionResponse,
    GuardSessionSummary,
    Trace,
    TraceStep,
    TraceWithDetections,
)

router = APIRouter(prefix="/guard", tags=["guard"])

# Deterministic mock-mode scoring rules (no LLM calls) — see Prompt 1.3 in
# docs/plan/P1_core_backend.md for the exact spec.
MOCK_SUSPICIOUS_PHRASES = ("ignore previous", "urgent", "administrator", "forward", "delete all")
MOCK_DESTRUCTIVE_ACTIONS = {"delete_file", "send_message"}


def _require_mock_mode() -> None:
    """Gates every `/guard/*` route on `GUARD_MODE=mock` for now. Read
    fresh from the environment on every call (not cached at import time
    like the rest of `config.py`) so it behaves like a live feature flag
    and so tests can toggle it per-test with `monkeypatch.setenv`.

    Takes no parameters. Returns nothing if `GUARD_MODE == "mock"`.
    Raises `HTTPException(501)` otherwise — the real implementation
    (`GUARD_MODE` unset or `"live"`) lands on Day 3 (Prompt 3.1).
    """
    if os.getenv("GUARD_MODE") != "mock":
        raise HTTPException(
            status_code=501,
            detail="GUARD_MODE=mock is not set. The real /guard/* implementation is not available yet (Day 3).",
        )


def _get_session_trace(session_id: str) -> Trace:
    """Looks up a guard session's trace by id, 404ing if it doesn't exist.

    Parameters:
    - `session_id`: the session to look up (== `trace_id` in storage).

    Returns the session's `Trace`. Raises `HTTPException(404)` if no
    session with this id was ever created via `POST /guard/session`.
    """
    trace = storage.get_trace(session_id)
    if trace is None:
        raise HTTPException(status_code=404, detail=f"No session found for session_id={session_id!r}.")
    return trace


def _mock_score(trace_so_far: Trace, step: TraceStep) -> tuple[int, Decision]:
    """Deterministic, LLM-free scoring for `GUARD_MODE=mock`. Priority
    order (most severe first): a destructive action after ANY earlier
    external observation in this session is the strongest signal; failing
    that, the step's own text matching a known suspicious phrase; failing
    that, treated as clean. This is intentionally cruder than the real
    pipeline (drift_engine + provenance + gate) landing on Day 3 — it
    exists so P2 can integrate against the real `/guard/*` contract today
    without waiting on LLM calls.

    Parameters:
    - `trace_so_far`: the session's trace as it was BEFORE this step (so
      "any earlier external observation" doesn't include `step` itself).
    - `step`: the new step being scored.

    Returns a `(risk_score, decision)` tuple: `(85, "block")`,
    `(60, "approval_required")`, or `(15, "allow_logged")`.
    """
    earlier_external_observation = any(s.input_provenance == "external" for s in trace_so_far.steps)
    if step.action in MOCK_DESTRUCTIVE_ACTIONS and earlier_external_observation:
        return 85, "block"

    lowered_text = step.input_text.lower()
    if any(phrase in lowered_text for phrase in MOCK_SUSPICIOUS_PHRASES):
        return 60, "approval_required"

    return 15, "allow_logged"


def _mock_explanation(step: TraceStep, risk_score: int, decision: Decision) -> str:
    """Builds the `explanation` string for a mock-mode detection, mirroring
    the real pipeline's style in `main._explanation_for` but naming the
    deterministic rule that fired instead of real drift/judge scores.
    """
    if decision == "block":
        reason = "a destructive action followed an earlier external observation in this session"
    elif decision == "approval_required":
        reason = "input_text matched a mock-mode suspicious phrase"
    else:
        reason = "nothing suspicious matched"
    return f"Step {step.step_id} ({step.action}): [mock mode] {reason} (risk_score={risk_score})"


def _score_and_save_step(session_id: str, trace_before: Trace, step: TraceStep) -> DetectionOutput:
    """Shared by `/guard/observe` and `/guard/check`: persists the new
    step, scores it (mock mode only, for now — see `_require_mock_mode`),
    and persists the resulting detection.

    Parameters:
    - `session_id`: the session this step belongs to.
    - `trace_before`: the session's trace as it was before this step.
    - `step`: the new step (already assigned its `step_id`).

    Returns the saved `DetectionOutput`.
    """
    storage.append_step(session_id, trace_before.agent_id, trace_before.original_goal, step)

    trace_with_step = trace_before.model_copy(update={"steps": [*trace_before.steps, step]})
    risk_score, decision = _mock_score(trace_before, step)
    provenance_flag = get_provenance_flag(trace_with_step, step.step_id)

    output = DetectionOutput(
        trace_id=session_id,
        step_id=step.step_id,
        drift_score=0.0,  # mock mode computes no real drift signal
        risk_score=risk_score,
        provenance_flag=provenance_flag,
        classification=classification_for(risk_score),
        explanation=_mock_explanation(step, risk_score, decision),
        decision=decision,
    )
    storage.save_detection_output(output)
    return output


@router.post("/session", response_model=GuardSessionResponse)
def start_session(request: GuardSessionRequest) -> GuardSessionResponse:
    """API endpoint: `POST /guard/session`. Starts a new guard session for
    an agent's run, so its `/guard/observe` and `/guard/check` calls have
    somewhere to accumulate steps. A session is stored as an ordinary
    trace (`trace_id` == `session_id`), starting with zero steps.

    Parameters (request body, see `GuardSessionRequest`):
    - `agent_id`: which agent this session is for.
    - `original_goal`: the agent's stated goal for this run.

    Returns a `GuardSessionResponse` with a fresh, unique `session_id`
    (`"session_" + 8 hex chars`).
    """
    _require_mock_mode()
    session_id = f"session_{secrets.token_hex(4)}"
    storage.save_trace(Trace(trace_id=session_id, agent_id=request.agent_id, original_goal=request.original_goal, steps=[]))
    return GuardSessionResponse(session_id=session_id)


@router.post("/observe", response_model=DetectionOutput)
def observe(request: GuardObserveRequest) -> DetectionOutput:
    """API endpoint: `POST /guard/observe`. Records content arriving mid-
    session (an email, a web page, a handoff) — NOT a tool call the agent
    is about to make (see `/guard/check` for that). Scored and persisted
    the same way as `/guard/check`, just with a fixed `action="observe"`.

    Parameters (request body, see `GuardObserveRequest`):
    - `session_id`: which session this content arrived in.
    - `actor`, `input_text`, `input_source`, `input_provenance`: describe
      the content, same meaning as the shared trace step schema.

    Returns the resulting `DetectionOutput`. Raises `HTTPException(404)`
    if `session_id` doesn't exist.
    """
    _require_mock_mode()
    trace = _get_session_trace(request.session_id)

    step = TraceStep(
        step_id=len(trace.steps) + 1,
        actor=request.actor,
        input_text=request.input_text,
        input_source=request.input_source,
        input_provenance=request.input_provenance,
        action="observe",
        action_params={},
        timestamp=datetime.now(UTC),
    )
    return _score_and_save_step(request.session_id, trace, step)


@router.post("/check", response_model=GuardCheckResponse)
def check(request: GuardCheckRequest) -> GuardCheckResponse:
    """API endpoint: `POST /guard/check`. Scores a tool call the agent is
    ABOUT to make — call this BEFORE actually running the tool, and only
    run it if the response says to.

    Parameters (request body, see `GuardCheckRequest`):
    - `session_id`: which session this check belongs to.
    - `actor`, `input_text`, `input_source`, `input_provenance`: same
      meaning as the shared trace step schema.
    - `action`, `action_params`: the tool call being considered.

    Returns a `GuardCheckResponse` (a `DetectionOutput` plus
    `approval_status`: `"pending"` if `decision == "approval_required"`
    (poll `GET /guard/approval/{session_id}/{step_id}`), otherwise
    `"not_required"` — the caller can act on `decision` immediately
    (`allow_logged` → run it, `block` → don't). Raises
    `HTTPException(404)` if `session_id` doesn't exist.
    """
    _require_mock_mode()
    trace = _get_session_trace(request.session_id)

    step = TraceStep(
        step_id=len(trace.steps) + 1,
        actor=request.actor,
        input_text=request.input_text,
        input_source=request.input_source,
        input_provenance=request.input_provenance,
        action=request.action,
        action_params=request.action_params,
        timestamp=datetime.now(UTC),
    )
    output = _score_and_save_step(request.session_id, trace, step)

    approval_status: GuardApprovalStatus = "pending" if output.decision == "approval_required" else "not_required"
    return GuardCheckResponse(**output.model_dump(), approval_status=approval_status)


@router.get("/approval/{session_id}/{step_id}", response_model=GuardApprovalResponse)
def get_approval(session_id: str, step_id: int) -> GuardApprovalResponse:
    """API endpoint: `GET /guard/approval/{session_id}/{step_id}`. Lets a
    caller poll for a human operator's decision on a step that
    `/guard/check` returned `approval_status: "pending"` for.

    Parameters:
    - `session_id`, `step_id` (URL path parameters): which step to check.

    Returns a `GuardApprovalResponse`: `status="pending"` if no operator
    decision is recorded yet, otherwise `"approved"` or `"denied"` with
    `decided_by`/`decided_at` filled in from `POST /approval-decision`.
    Raises `HTTPException(404)` if this step was never scored at all.
    """
    _require_mock_mode()
    stored_decision = storage.get_detection_decision(session_id, step_id)
    if stored_decision is None:
        raise HTTPException(
            status_code=404,
            detail=f"No detection found for session_id={session_id!r}, step_id={step_id}.",
        )

    existing = storage.get_approval_decision(session_id, step_id)
    if existing is None:
        return GuardApprovalResponse(status="pending")

    status: GuardApprovalState = "approved" if existing["operator_decision"] == "approved" else "denied"
    return GuardApprovalResponse(status=status, decided_by=existing["operator_id"], decided_at=existing["timestamp"])


@router.get("/session/{session_id}", response_model=TraceWithDetections)
def get_session(session_id: str) -> TraceWithDetections:
    """API endpoint: `GET /guard/session/{session_id}`. Same shape as
    `GET /trace/{trace_id}` — `session_id` and `trace_id` are the same
    underlying id, just named for the audience (a live session vs. a
    previously-analyzed trace).

    Parameters:
    - `session_id` (URL path parameter): the session to look up.

    Returns a `TraceWithDetections`. Raises `HTTPException(404)` if no
    session with this id was ever created via `POST /guard/session`.
    """
    _require_mock_mode()
    result = storage.get_trace_with_detections(session_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No session found for session_id={session_id!r}.")
    return result


@router.get("/sessions", response_model=list[GuardSessionSummary])
def list_sessions(limit: int = 20) -> list[GuardSessionSummary]:
    """API endpoint: `GET /guard/sessions?limit=20`. Lists recent guard
    sessions, newest first, for a dashboard to show live/recent activity
    without looking up one session at a time.

    Parameters:
    - `limit` (query parameter, default 20): maximum number of sessions
      to return.

    Returns a list of `GuardSessionSummary`.
    """
    _require_mock_mode()
    return [
        GuardSessionSummary(
            session_id=row["trace_id"],
            agent_id=row["agent_id"],
            original_goal=row["original_goal"],
            created_at=row["created_at"],
            step_count=row["step_count"],
            max_risk=row["max_risk"],
            worst_decision=row["worst_decision"],
        )
        for row in storage.list_sessions(limit)
    ]
