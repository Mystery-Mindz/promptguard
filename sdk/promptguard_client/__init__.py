"""Python SDK for PromptGuard's live guard API (see docs/plan/00_OVERVIEW.md
section A, and backend/CLAUDE.md's `/guard/*` section for the backend's own
description of the same contract). Depends only on `httpx`.

Protect any tool-calling agent in about 3 lines::

    guard = PromptGuard(base_url, api_key)
    session = guard.start(agent_id="assistant", goal=user_message)

    @session.guarded_tool(destructive=True)
    def send_message(to, content):
        ...  # actually send it

    send_message(to="a@b.com", content="hi", input_text=the_text_that_triggered_this,
                 input_source="tool_output", input_provenance="external")

See this package's README for a complete example, including the low-level
`session.check(...)` call and `session.observe(...)`.
"""

import functools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, fields
from typing import Any, TypeVar

import httpx

__all__ = [
    "PromptGuard",
    "GuardSession",
    "Detection",
    "CheckResult",
    "ApprovalStatus",
    "PromptGuardError",
    "PromptGuardAPIError",
    "ToolBlocked",
    "ToolDenied",
    "ApprovalPending",
]

logger = logging.getLogger("promptguard_client")

T = TypeVar("T")


# --- Response shapes --------------------------------------------------------
#
# Plain dataclasses, not the backend's pydantic models -- this package has
# no dependency on (and must never import) the backend's `app` package, so
# the two can version independently. Built via _from_json, which only
# pulls out the fields this dataclass declares and ignores anything else,
# so a server that adds a new field (the /guard contract's own rule: fields
# may be ADDED, never renamed or removed) never breaks an older SDK.


@dataclass(frozen=True)
class Detection:
    """The result of `/guard/observe`, or the detection half of
    `/guard/check`'s response. Mirrors the backend's `DetectionOutput`
    (`trace_id` is the session_id)."""

    trace_id: str
    step_id: int
    drift_score: float
    risk_score: int
    provenance_flag: str
    classification: str
    explanation: str
    decision: str


@dataclass(frozen=True)
class CheckResult(Detection):
    """The result of `/guard/check`: a `Detection` plus whether a human
    approval is now pending."""

    approval_status: str  # "not_required" | "pending"


@dataclass(frozen=True)
class ApprovalStatus:
    """The result of `GET /guard/approval/{session_id}/{step_id}`."""

    status: str  # "pending" | "approved" | "denied"
    decided_by: str | None = None
    decided_at: str | None = None


def _from_json(cls: type[T], data: dict[str, Any]) -> T:
    field_names = {f.name for f in fields(cls)}  # type: ignore[arg-type]
    return cls(**{key: value for key, value in data.items() if key in field_names})


# --- Exceptions --------------------------------------------------------------


class PromptGuardError(Exception):
    """Base class for every exception this SDK raises."""


class PromptGuardAPIError(PromptGuardError):
    """Raised for any non-2xx response from the PromptGuard backend that
    isn't one of the specific decision outcomes below (e.g. a 404 for an
    unknown session, a 503 if the backend's detection model is
    unavailable)."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"PromptGuard API error {status_code}: {detail}")


class ToolBlocked(PromptGuardError):
    """Raised by `guarded_tool` when PromptGuard's decision is `"block"`.
    The wrapped tool is NOT run."""

    def __init__(self, detection: CheckResult) -> None:
        self.detection = detection
        super().__init__(f"blocked by PromptGuard: {detection.explanation}")


class ToolDenied(PromptGuardError):
    """Raised by `guarded_tool` when an `approval_required` decision was
    either explicitly denied by an operator, or timed out while still
    pending. Either way the wrapped tool is NOT run. Check
    `approval.status` ("denied" vs. still "pending") to tell them apart."""

    def __init__(self, detection: CheckResult, approval: ApprovalStatus) -> None:
        self.detection = detection
        self.approval = approval
        reason = "denied by operator" if approval.status == "denied" else "approval timed out while still pending"
        super().__init__(f"{reason}: {detection.explanation}")


class ApprovalPending(PromptGuardError):
    """Raised by `guarded_tool(wait=False)` instead of blocking on
    `approval_required`, so a web app can return to its caller
    immediately. Poll `session.approval(step_id)` later, and once it's
    `"approved"`, call the undecorated tool function yourself."""

    def __init__(self, step_id: int, session_id: str, detection: CheckResult) -> None:
        self.step_id = step_id
        self.session_id = session_id
        self.detection = detection
        super().__init__(
            f"approval pending for session_id={session_id!r} step_id={step_id} -- "
            "poll session.approval(step_id) and resume once it's decided"
        )


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        detail = response.json().get("detail", response.text)
    except Exception:
        detail = response.text
    raise PromptGuardAPIError(response.status_code, detail)


# --- Client ------------------------------------------------------------------


class PromptGuard:
    """Entry point: `PromptGuard(base_url, api_key)`. Every `/guard/*`
    request sends `X-PromptGuard-Key: <api_key>` (per the contract in
    docs/plan/00_OVERVIEW.md) -- harmless to send even before the backend
    enforces it.

    `enforce=False` means "log only, never block": every session this
    client starts (unless overridden per-session via `start(..., enforce=)`)
    still calls the real `/guard/check` endpoint and records the real
    decision, but `guarded_tool` runs the tool regardless of what that
    decision was. This is the honest "guard OFF" comparison -- the same
    attack succeeds, and whatever it touches shows the leak, while
    PromptGuard's own records show what it WOULD have done.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        enforce: bool = True,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"X-PromptGuard-Key": api_key} if api_key else {}
        self._client = httpx.Client(base_url=base_url, headers=headers, timeout=timeout, transport=transport)
        self.enforce = enforce

    def start(self, agent_id: str, goal: str, *, enforce: bool | None = None) -> "GuardSession":
        """Calls `POST /guard/session` and returns a `GuardSession` for
        it. `enforce` defaults to this client's own `enforce`; pass it
        explicitly to run one session guarded and another unguarded from
        the same client (the "guard ON vs. OFF" comparison)."""
        response = self._client.post("/guard/session", json={"agent_id": agent_id, "original_goal": goal})
        _raise_for_status(response)
        session_id = response.json()["session_id"]
        return GuardSession(self._client, session_id, enforce=self.enforce if enforce is None else enforce)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PromptGuard":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class GuardSession:
    """A single agent run against PromptGuard. Returned by
    `PromptGuard.start(...)` -- not constructed directly."""

    def __init__(self, client: httpx.Client, session_id: str, *, enforce: bool = True) -> None:
        self._client = client
        self.session_id = session_id
        self.enforce = enforce

    # --- Low-level: one HTTP call each, no side effects beyond that -------

    def check(
        self,
        actor: str,
        action: str,
        params: dict[str, Any],
        input_text: str,
        input_source: str,
        input_provenance: str,
    ) -> CheckResult:
        """Low-level: `POST /guard/check`, scoring a tool call BEFORE it
        runs. Always calls the server, regardless of `self.enforce` --
        does not wait on approval and does not raise based on the
        resulting `decision`; that orchestration is `guarded_tool`'s job.
        Call this directly if you want to make your own enforcement
        decisions instead of using the decorator."""
        response = self._client.post(
            "/guard/check",
            json={
                "session_id": self.session_id,
                "actor": actor,
                "input_text": input_text,
                "input_source": input_source,
                "input_provenance": input_provenance,
                "action": action,
                "action_params": params,
            },
        )
        _raise_for_status(response)
        return _from_json(CheckResult, response.json())

    def observe(self, actor: str, input_text: str, input_source: str, input_provenance: str) -> Detection:
        """`POST /guard/observe`: records content arriving mid-session (a
        tool's output, a web page, an email, a handoff) -- NOT a tool call
        about to run (see `check()` for that). Always calls the server,
        regardless of `self.enforce` -- observing never blocks anything by
        itself."""
        response = self._client.post(
            "/guard/observe",
            json={
                "session_id": self.session_id,
                "actor": actor,
                "input_text": input_text,
                "input_source": input_source,
                "input_provenance": input_provenance,
            },
        )
        _raise_for_status(response)
        return _from_json(Detection, response.json())

    def approval(self, step_id: int) -> ApprovalStatus:
        """One poll of `GET /guard/approval/{session_id}/{step_id}` -- no
        waiting. See `wait_for_approval` to block until it's decided."""
        response = self._client.get(f"/guard/approval/{self.session_id}/{step_id}")
        _raise_for_status(response)
        return _from_json(ApprovalStatus, response.json())

    def wait_for_approval(
        self,
        step_id: int,
        *,
        timeout: float = 120.0,
        poll_interval: float = 2.0,
    ) -> ApprovalStatus:
        """Polls `GET /guard/approval/...` every `poll_interval` seconds
        (default 2s) until an operator decides, or `timeout` seconds
        (default 120s) elapse. Returns the last-seen `ApprovalStatus`
        either way -- still `"pending"` if it timed out."""
        deadline = time.monotonic() + timeout
        status = self.approval(step_id)
        while status.status == "pending" and time.monotonic() < deadline:
            time.sleep(poll_interval)
            status = self.approval(step_id)
        return status

    # --- High-level: the decorator -----------------------------------------

    def guarded_tool(
        self,
        *,
        action: str | None = None,
        actor: str | None = None,
        destructive: bool = False,
        wait: bool = True,
        timeout: float = 120.0,
        poll_interval: float = 2.0,
    ) -> Callable[[Callable[..., T]], Callable[..., T]]:
        """Decorator: wraps a tool function so every call goes through
        `check()` first, then either runs it or raises.

        - `decision == "allow_logged"` -> runs the tool.
        - `decision == "block"` -> raises `ToolBlocked(detection)`; the
          tool is NOT run.
        - `decision == "approval_required"`: if `wait=True` (default),
          polls for up to `timeout` seconds (default 120s, every
          `poll_interval` seconds, default 2s) -- `"approved"` runs the
          tool, `"denied"` or a timeout raises `ToolDenied`. If
          `wait=False`, raises `ApprovalPending(step_id)` immediately
          instead of blocking, so a web app can return right away and
          resume once an operator decides (poll `session.approval(step_id)`
          yourself, then call the undecorated tool).
        - `self.enforce == False`: still calls `check()` (so the real
          decision is recorded -- the honest "guard OFF" comparison), logs
          what it would have done, but always runs the tool regardless of
          the decision.

        `destructive` is not sent to the server (the backend's own gate
        already knows which actions are destructive, via
        `config.DESTRUCTIVE_ACTIONS`) -- it's a local hint included in the
        `enforce=False` log line, so a "guard OFF" run still visibly flags
        which bypassed calls were the risky ones.

        The decorated function's real arguments must be passed as
        keywords (the natural shape for a tool-calling agent's arguments
        anyway) -- they double as `action_params`. Every call also needs
        `input_text`, `input_source`, and `input_provenance` (what
        triggered this call), and optionally `actor` (overrides this
        decorator's own `actor=`, if any).
        """
        default_actor = actor
        resolved_action_override = action

        def decorator(fn: Callable[..., T]) -> Callable[..., T]:
            resolved_action = resolved_action_override or fn.__name__

            @functools.wraps(fn)
            def wrapper(
                *,
                input_text: str,
                input_source: str = "tool_output",
                input_provenance: str = "internal",
                actor: str | None = None,
                **kwargs: Any,
            ) -> T:
                call_actor = actor if actor is not None else default_actor
                if call_actor is None:
                    raise TypeError(
                        f"guarded_tool {resolved_action!r}: no actor given -- "
                        "pass actor= to @guarded_tool(...) or to this call"
                    )

                detection = self.check(call_actor, resolved_action, kwargs, input_text, input_source, input_provenance)

                if not self.enforce:
                    logger.info(
                        "guard disabled (enforce=False), logging only: action=%s actor=%s destructive=%s "
                        "decision=%s risk_score=%s explanation=%s",
                        resolved_action,
                        call_actor,
                        destructive,
                        detection.decision,
                        detection.risk_score,
                        detection.explanation,
                    )
                    return fn(**kwargs)

                if detection.decision == "block":
                    raise ToolBlocked(detection)

                if detection.decision == "approval_required":
                    if not wait:
                        raise ApprovalPending(detection.step_id, self.session_id, detection)
                    approval = self.wait_for_approval(detection.step_id, timeout=timeout, poll_interval=poll_interval)
                    if approval.status == "approved":
                        return fn(**kwargs)
                    raise ToolDenied(detection, approval)

                return fn(**kwargs)  # "allow_logged"

            return wrapper

        return decorator
