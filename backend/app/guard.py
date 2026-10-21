"""The /guard/* live guard API (see docs/plan/00_OVERVIEW.md, section A).

`GUARD_MODE=mock` gives deterministic, LLM-free scoring (Prompt 1.3) so P2
(the Protected Assistant) can integrate against the contract without
waiting on real model calls. `GUARD_MODE` unset or `"mock"` gives the real
drift+judge+provenance+gate pipeline (Prompt 3.1) — the same pipeline
`/analyze-trace` uses, just run incrementally: a session is a live trace
(`trace_id` == `session_id`) that grows one step at a time via
`storage.append_step`, and each `/guard/observe`/`/guard/check` call scores
ONLY the new step, against the session's accumulated history so far.
Any other `GUARD_MODE` value is a misconfiguration (`HTTPException(500)`).

Every route on `router` requires `X-PromptGuard-Key` (Prompt 4.1, fixes
BUG-06), enforced once via `require_api_key` as a router-level dependency
rather than repeated in each route. `/guard/check` is additionally rate-
limited per validated key (`config.RATE_LIMIT_GUARD_CHECK_*`).
"""

import logging
import os
import secrets
import time
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException

from app import config, storage
from app.drift_engine import compute_drift_and_risk
from app.gate import classification_for, decide, explanation_for_step
from app.llm import ModelUnavailable
from app.provenance import get_provenance_flag
from app.ratelimit import TokenBucketLimiter
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


def require_api_key(x_promptguard_key: str | None = Header(default=None, alias="X-PromptGuard-Key")) -> str:
    """FastAPI dependency, applied to every `/guard/*` route via
    `router`'s `dependencies=`: requires `X-PromptGuard-Key` to match one
    of `config.PROMPTGUARD_API_KEYS`. Raises `HTTPException(401)`
    otherwise. Returns the validated key itself, so a route that also
    needs it (`check`, for rate limiting) can depend on this function
    again without re-validating — FastAPI caches a dependency's result
    per request by default.
    """
    if not x_promptguard_key or x_promptguard_key not in config.PROMPTGUARD_API_KEYS:
        raise HTTPException(status_code=401, detail="Missing or invalid X-PromptGuard-Key header.")
    return x_promptguard_key


router = APIRouter(prefix="/guard", tags=["guard"], dependencies=[Depends(require_api_key)])
logger = logging.getLogger(__name__)

GuardMode = Literal["mock", "live"]

# Rate limiting (Prompt 4.1, fixes BUG-06) — keyed by the caller's
# validated X-PromptGuard-Key. See config.RATE_LIMIT_GUARD_CHECK_* for the
# budget.
_check_limiter = TokenBucketLimiter(config.RATE_LIMIT_GUARD_CHECK_CAPACITY, config.RATE_LIMIT_GUARD_CHECK_REFILL_PER_SECOND)


def _rate_limit_check(api_key: str = Depends(require_api_key)) -> None:
    if not _check_limiter.allow(api_key):
        raise HTTPException(status_code=429, detail="Rate limit exceeded for /guard/check; slow down.")

# Deterministic mock-mode scoring rules (no LLM calls) — see Prompt 1.3 in
# docs/plan/P1_core_backend.md for the exact spec. The destructive-action
# set itself lives in config.DESTRUCTIVE_ACTIONS (shared with gate.decide's
# action-severity rule) rather than a separate local copy here.
MOCK_SUSPICIOUS_PHRASES = ("ignore previous", "urgent", "administrator", "forward", "delete all")


def _resolve_mode() -> GuardMode:
    """Reads `GUARD_MODE` fresh from the environment on every call (not
    cached at import time like the rest of `config.py`) so it behaves like
    a live feature flag and so tests can toggle it per-test with
    `monkeypatch.setenv`.

    Takes no parameters. Returns `"mock"` if `GUARD_MODE == "mock"`;
    `"live"` if it's unset or `"live"` (the real drift+judge+provenance+
    gate pipeline, Prompt 3.1 — this is now the default, replacing the old
    Day-1 skeleton's 501). Raises `HTTPException(500)` for any other value,
    since that's a misconfiguration, not a valid mode.
    """
    raw = os.getenv("GUARD_MODE")
    if raw is None or raw == "live":
        return "live"
    if raw == "mock":
        return "mock"
    raise HTTPException(
        status_code=500,
        detail=f"Invalid GUARD_MODE={raw!r}; expected 'mock' or 'live' (unset also means 'live').",
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


def _mock_score(trace_so_far: Trace, step: TraceStep) -> int:
    """Deterministic, LLM-free risk scoring for `GUARD_MODE=mock`. Priority
    order (most severe first): a destructive action after ANY earlier
    external observation in this session is the strongest signal; failing
    that, the step's own text matching a known suspicious phrase; failing
    that, treated as clean. This is intentionally cruder than the real
    pipeline (drift_engine + provenance + gate, see `_live_score`) — it
    exists so a caller can integrate against the real `/guard/*` contract
    without waiting on LLM calls. The actual enforcement decision for this
    score is computed afterward by `gate.decide` (see `_score_and_save_step`),
    same as the real pipeline — this function only produces the score.

    Parameters:
    - `trace_so_far`: the session's trace as it was BEFORE this step (so
      "any earlier external observation" doesn't include `step` itself).
    - `step`: the new step being scored.

    Returns a `risk_score`: `85`, `60`, or `15`.
    """
    earlier_external_observation = any(s.input_provenance == "external" for s in trace_so_far.steps)
    if step.action in config.DESTRUCTIVE_ACTIONS and earlier_external_observation:
        return 85

    lowered_text = step.input_text.lower()
    if any(phrase in lowered_text for phrase in MOCK_SUSPICIOUS_PHRASES):
        return 60

    return 15


def _mock_explanation(step: TraceStep, risk_score: int, decision: Decision) -> str:
    """Builds the `explanation` string for a mock-mode detection, mirroring
    the real pipeline's style (`gate.explanation_for_step`) but naming the
    deterministic rule that fired instead of real drift/judge scores.

    `risk_score` alone identifies which of `_mock_score`'s rules produced
    it (85/60/15 are mutually exclusive by construction); `decision` can
    still differ from the naive risk_score-only mapping when `gate.decide`
    floors it on taint or escalates it on the action-severity rule, so a
    `60`-score step can still come back `"block"` if it's also a
    destructive action on external/tainted content.
    """
    if risk_score == 85:
        reason = "a destructive action followed an earlier external observation in this session"
    elif risk_score == 60:
        reason = "input_text matched a mock-mode suspicious phrase"
    else:
        reason = "nothing suspicious matched"
    if decision == "block" and risk_score != 85:
        reason += "; escalated to block — destructive action on external/tainted content"
    elif decision == "approval_required" and risk_score == 15:
        reason += "; floored to approval_required by tainted provenance"
    return f"Step {step.step_id} ({step.action}): [mock mode] {reason} (risk_score={risk_score})"


def _score_and_save_step(session_id: str, trace_before: Trace, step: TraceStep) -> DetectionOutput:
    """Shared by `/guard/observe` and `/guard/check`: scores the new step
    (mock or live, per `_resolve_mode`), then persists both the step and
    its resulting detection — in that order, and only once scoring has
    actually succeeded, so a model failure in live mode (`ModelUnavailable`)
    never leaves an unscored, unpollable step stuck in the session.

    In live mode, scoring reuses `drift_engine.compute_drift_and_risk`
    against `trace_with_step` (the session so far, plus this one new
    step) — exactly the same drift+judge pipeline `/analyze-trace` uses.
    Because `llm.embed_many`/`llm.judge` cache by text content (see
    `app/llm.py`), re-embedding the session's goal and every earlier
    step's already-embedded text on each call costs nothing beyond a
    local cache lookup: the only real model call this makes is the ONE
    new step's embedding, plus the ONE judge call on its `input_text` —
    there's no need for a separate per-session embedding cache here.
    `provenance.get_provenance_flag` runs over the whole session so far
    (the Day-1 forward-taint logic), same as mock mode.

    Parameters:
    - `session_id`: the session this step belongs to.
    - `trace_before`: the session's trace as it was before this step.
    - `step`: the new step (already assigned its `step_id`).

    Returns the saved `DetectionOutput`. Raises `HTTPException(503)` if
    live-mode scoring's underlying model call fails even after `llm.py`'s
    retries (and fallback, if configured).
    """
    mode = _resolve_mode()
    trace_with_step = trace_before.model_copy(update={"steps": [*trace_before.steps, step]})
    started_at = time.perf_counter()

    try:
        if mode == "mock":
            risk_score = _mock_score(trace_before, step)
            drift_score = 0.0
        else:
            try:
                drift_score, risk_score = compute_drift_and_risk(trace_with_step, step.step_id)
            except ModelUnavailable as e:
                raise HTTPException(
                    status_code=503,
                    detail=f"Detection model is currently unavailable, scoring session_id={session_id!r} "
                    f"step {step.step_id} failed after retries: {e}",
                ) from e
    finally:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.info(
            "guard_check_latency_ms=%.1f mode=%s session_id=%s step_id=%s action=%s",
            elapsed_ms,
            mode,
            session_id,
            step.step_id,
            step.action,
        )

    provenance_flag = get_provenance_flag(trace_with_step, step.step_id)
    decision = decide(risk_score, provenance_flag, step.action)
    explanation = (
        _mock_explanation(step, risk_score, decision)
        if mode == "mock"
        else explanation_for_step(step, drift_score, risk_score, provenance_flag)
    )

    # Persisted only now that scoring has actually succeeded (see
    # docstring above) — append_step before save_detection_output, same
    # order /analyze-trace writes in, so a trace_steps row always has a
    # matching detection_outputs row (get_pending_approvals' join on the
    # two relies on that).
    storage.append_step(session_id, trace_before.agent_id, trace_before.original_goal, step)
    output = DetectionOutput(
        trace_id=session_id,
        step_id=step.step_id,
        drift_score=drift_score,
        risk_score=risk_score,
        provenance_flag=provenance_flag,
        classification=classification_for(decision),
        explanation=explanation,
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
    _resolve_mode()
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
    _resolve_mode()
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
def check(request: GuardCheckRequest, _rate_limit: None = Depends(_rate_limit_check)) -> GuardCheckResponse:
    """API endpoint: `POST /guard/check`. Scores a tool call the agent is
    ABOUT to make — call this BEFORE actually running the tool, and only
    run it if the response says to.

    Parameters (request body, see `GuardCheckRequest`):
    - `session_id`: which session this check belongs to.
    - `actor`, `input_text`, `input_source`, `input_provenance`: same
      meaning as the shared trace step schema.
    - `action`, `action_params`: the tool call being considered.

    Rate-limited per validated `X-PromptGuard-Key` (Prompt 4.1,
    `config.RATE_LIMIT_GUARD_CHECK_*`) — `HTTPException(429)` once a
    caller's burst budget is exhausted.

    Returns a `GuardCheckResponse` (a `DetectionOutput` plus
    `approval_status`: `"pending"` if `decision == "approval_required"`
    (poll `GET /guard/approval/{session_id}/{step_id}`), otherwise
    `"not_required"` — the caller can act on `decision` immediately
    (`allow_logged` → run it, `block` → don't). Raises
    `HTTPException(404)` if `session_id` doesn't exist, or
    `HTTPException(503)` if live-mode scoring's model call is unavailable
    (see `_score_and_save_step`).
    """
    _resolve_mode()
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
    `/guard/check` returned `approval_status: "pending"` for. Any
    `approval_required` step a live session produces is automatically
    visible to `GET /pending-approvals` too (it's the same
    `detection_outputs` table `/analyze-trace` writes to), so a decision
    made via `POST /approval-decision` shows up here as soon as it's
    recorded — no separate wiring needed between the two.

    Parameters:
    - `session_id`, `step_id` (URL path parameters): which step to check.

    Returns a `GuardApprovalResponse`: `status="pending"` if no operator
    decision is recorded yet, otherwise `"approved"` or `"denied"` with
    `decided_by`/`decided_at` filled in from `POST /approval-decision`.
    Raises `HTTPException(404)` if this step was never scored at all.
    """
    _resolve_mode()
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
    _resolve_mode()
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
    _resolve_mode()
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
