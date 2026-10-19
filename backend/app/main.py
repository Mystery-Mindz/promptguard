import os
import secrets
import sqlite3
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app import guard, storage
from app.agent import run_agent
from app.drift_engine import compute_drift_and_risk
from app.gate import classification_for, decide
from app.provenance import get_provenance_flag
from app.schemas import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    DetectionOutput,
    FinalStatus,
    PendingApproval,
    ProvenanceFlag,
    RunAgentRequest,
    Trace,
    TraceStep,
    TraceWithDetections,
)

app = FastAPI(title="PromptGuard")

# Allow the frontend to call this API from:
#  - any localhost/127.0.0.1 port (local dev — Streamlit runs on 8501/8502
#    as two separate processes, and the port can vary)
#  - any *.streamlit.app subdomain over HTTPS (Streamlit Cloud, where the
#    deployed frontend actually lives — a real internet URL, not localhost)
# Anchored at both ends so this can't be satisfied by, e.g., a domain that
# merely contains "streamlit.app" as a substring (https://streamlit.app.evil.com).
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1):\d+$|^https://([a-zA-Z0-9-]+\.)*streamlit\.app$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

storage.init_db()
app.include_router(guard.router)


@app.get("/health")
def health_endpoint() -> dict[str, str]:
    """API endpoint: `GET /health`. A basic liveness check for deployment
    platforms (load balancers, container orchestrators) to confirm the
    process is up and serving requests. Deliberately shallow — no database
    or Gemini connectivity check, just confirms the app itself is alive.

    Takes no parameters. Returns `{"status": "ok"}` with HTTP 200.
    """
    return {"status": "ok"}


def _generate_trace_id() -> str:
    """Generates a fresh, collision-resistant trace_id for a `/run-agent`
    call that didn't supply its own, so two runs without an explicit
    `trace_id` never collide and silently share (and overwrite) each
    other's steps, detections, and approval decisions.

    Takes no parameters. Returns a string like `"run_a1b2c3d4"` — the
    `"run_"` prefix plus 8 hex characters (4 random bytes).
    """
    return f"run_{secrets.token_hex(4)}"


@app.post("/run-agent", response_model=Trace)
def run_agent_endpoint(request: RunAgentRequest) -> Trace:
    """API endpoint: `POST /run-agent`. Runs the simulated AI agent
    (`agent.run_agent`) against a goal and returns the resulting trace — but
    does NOT run it through detection or save it; call `POST /analyze-trace`
    with the returned trace afterward to do that.

    Parameters (from the request body, see `RunAgentRequest`):
    - `original_goal`: the task to give the agent.
    - `trace_id`: identifier for the resulting trace. If omitted (or
      `null`), a fresh unique id is generated (see `_generate_trace_id`) so
      concurrent/repeated runs never collide on the same trace_id.
    - `agent_id`: which agent is acting (has a default).

    Returns the resulting `Trace`.
    """
    trace_id = request.trace_id or _generate_trace_id()
    return run_agent(request.original_goal, trace_id, request.agent_id)


def _explanation_for(step: TraceStep, drift_score: float, risk_score: int, provenance_flag: ProvenanceFlag) -> str:
    """Builds the plain-English `explanation` string shown alongside a
    step's detection result, so a human reviewer can see at a glance why a
    step was scored the way it was.

    Parameters:
    - `step`: the trace step being explained.
    - `drift_score`, `risk_score`: this step's computed scores.
    - `provenance_flag`: this step's provenance flag.

    Returns a string like "Step 2 (delete_file): drift_score=0.83,
    risk_score=87, input is externally sourced" — always includes the
    scores, and adds a note about provenance when the step is `"tainted"`
    or `"external"`.
    """
    reasons = [f"drift_score={drift_score:.2f}", f"risk_score={risk_score}"]
    if provenance_flag == "tainted":
        reasons.append("input traces back to external content via an agent handoff")
    elif provenance_flag == "external":
        reasons.append("input is externally sourced")
    return f"Step {step.step_id} ({step.action}): " + ", ".join(reasons)


@app.post("/analyze-trace", response_model=list[DetectionOutput])
def analyze_trace(trace: Trace) -> list[DetectionOutput]:
    """API endpoint: `POST /analyze-trace`. The core of PromptGuard: runs
    every step of a trace through the full detection pipeline (Intent
    Drift Engine → Provenance Tracer → Secure Execution Gate), saves both
    the trace itself and each step's result, and returns the results.

    Parameters (request body): a `Trace` — can come from `run_agent`, or be
    hand-built/loaded from a test file; either way it just needs to match
    the shared trace JSON schema.

    Returns a list of `DetectionOutput`, one per step, in step order. As a
    side effect, persists the trace (so it can be looked up later via
    `GET /trace/{trace_id}`) and each detection result (so
    `GET /pending-approvals` and `POST /approval-decision` can see it).

    If the drift engine's underlying Gemini call fails partway through
    (e.g. a 429/503 from the model provider), responds with 503 and a
    readable `detail` message instead of a bare 500 — and deletes the
    trace entirely rather than leaving some steps scored and others
    missing; retry `/analyze-trace` from scratch once the error clears.
    """
    storage.save_trace(trace)
    outputs: list[DetectionOutput] = []

    for step in trace.steps:
        try:
            drift_score, risk_score = compute_drift_and_risk(trace, step.step_id)
        except Exception as e:
            storage.delete_trace(trace.trace_id)
            raise HTTPException(
                status_code=503,
                detail=f"Detection model is currently unavailable, analyzing trace_id={trace.trace_id!r} "
                f"step {step.step_id} failed: {e}",
            ) from e

        provenance_flag = get_provenance_flag(trace, step.step_id)
        decision = decide(risk_score, provenance_flag)

        output = DetectionOutput(
            trace_id=trace.trace_id,
            step_id=step.step_id,
            drift_score=drift_score,
            risk_score=risk_score,
            provenance_flag=provenance_flag,
            classification=classification_for(risk_score),
            explanation=_explanation_for(step, drift_score, risk_score, provenance_flag),
            decision=decision,
        )
        storage.save_detection_output(output)
        outputs.append(output)

    return outputs


@app.get("/trace/{trace_id}", response_model=TraceWithDetections)
def get_trace_endpoint(trace_id: str) -> TraceWithDetections:
    """API endpoint: `GET /trace/{trace_id}`. Looks up a previously-analyzed
    trace by ID, so a client (e.g. the dashboard) can display its real,
    stored results instead of re-sending a fixed payload.

    Parameters:
    - `trace_id` (URL path parameter): the trace to look up.

    Returns a `TraceWithDetections` — the original trace plus every step's
    detection result. Responds with 404 if no trace with this ID was ever
    analyzed via `POST /analyze-trace`.
    """
    result = storage.get_trace_with_detections(trace_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No trace found for trace_id={trace_id!r}. Run /analyze-trace on it first.",
        )
    return result


@app.get("/pending-approvals", response_model=list[PendingApproval])
def pending_approvals_endpoint() -> list[PendingApproval]:
    """API endpoint: `GET /pending-approvals`. Lets the Operator Approval
    Console poll for real work — every step currently awaiting a human
    decision.

    Takes no parameters. Returns a list of `PendingApproval` (oldest
    first): every step flagged `approval_required` by `/analyze-trace`
    that hasn't been approved or denied yet.
    """
    return [
        PendingApproval(
            trace_id=row["trace_id"],
            step_id=row["step_id"],
            risk_score=row["risk_score"],
            provenance_flag=row["provenance_flag"],
            explanation=row["explanation"],
            timestamp=row["created_at"],
        )
        for row in storage.get_pending_approvals()
    ]


def _already_decided_error(trace_id: str, step_id: int, existing_decision: dict) -> HTTPException:
    """Builds the 409 raised when a `(trace_id, step_id)` already has a
    recorded operator decision — shared by the normal pre-check in
    `approval_decision_endpoint` and by its `sqlite3.IntegrityError`
    fallback for the near-simultaneous-request race (BUG-12), so both
    paths report the exact same thing to the caller.

    Parameters:
    - `trace_id`, `step_id`: identify the step.
    - `existing_decision`: the stored decision dict (see
      `storage.get_approval_decision`) to quote in the error message.

    Returns an `HTTPException` (status 409) ready to be raised.
    """
    return HTTPException(
        status_code=409,
        detail=f"trace_id={trace_id!r}, step_id={step_id} was already "
        f"{existing_decision['operator_decision']!r} by operator_id="
        f"{existing_decision['operator_id']!r} at {existing_decision['timestamp']}; "
        "a decision cannot be changed once made.",
    )


@app.post("/approval-decision", response_model=ApprovalDecisionResponse)
def approval_decision_endpoint(request: ApprovalDecisionRequest) -> ApprovalDecisionResponse:
    """API endpoint: `POST /approval-decision`. Records a human operator's
    approve/deny call on a step that PromptGuard flagged for review. This
    decision must come from the request body (a real human action on a
    separate, authenticated channel) — never derived from the trace's own
    content, to avoid a "confused deputy" bug where flagged content could
    approve itself.

    Parameters (request body, see `ApprovalDecisionRequest`):
    - `trace_id`, `step_id`: which step is being decided on.
    - `operator_decision`: `"approved"` or `"denied"`.
    - `operator_id`: optional identifier for who decided.

    Returns an `ApprovalDecisionResponse` with the resulting `final_status`
    (`"approved_and_allowed"` or `"denied_and_blocked"`) and a timestamp.
    Responds with 404 if this step was never analyzed, or 409 if it either
    wasn't flagged `approval_required` in the first place, or was already
    decided (a decision can't be changed once made).
    """
    stored_decision = storage.get_detection_decision(request.trace_id, request.step_id)

    if stored_decision is None:
        raise HTTPException(
            status_code=404,
            detail=f"No detection found for trace_id={request.trace_id!r}, step_id={request.step_id}. "
            "Run /analyze-trace on this trace first.",
        )
    if stored_decision != "approval_required":
        raise HTTPException(
            status_code=409,
            detail=f"trace_id={request.trace_id!r}, step_id={request.step_id} was not flagged "
            f"approval_required (recorded decision: {stored_decision!r}); nothing to approve or deny.",
        )

    existing_decision = storage.get_approval_decision(request.trace_id, request.step_id)
    if existing_decision is not None:
        raise _already_decided_error(request.trace_id, request.step_id, existing_decision)

    final_status: FinalStatus = "approved_and_allowed" if request.operator_decision == "approved" else "denied_and_blocked"
    timestamp = datetime.now(UTC)

    try:
        storage.save_approval_decision(
            trace_id=request.trace_id,
            step_id=request.step_id,
            operator_decision=request.operator_decision,
            operator_id=request.operator_id,
            final_status=final_status,
            timestamp=timestamp.isoformat(),
        )
    except sqlite3.IntegrityError:
        # A second, near-simultaneous request for this same (trace_id,
        # step_id) raced past the check above before this one committed its
        # INSERT. The unique (trace_id, step_id) primary key on
        # approval_decisions turns that race into a real constraint
        # violation instead of silently overwriting the winner (BUG-12) —
        # treat it exactly like the already-decided case above.
        winning_decision = storage.get_approval_decision(request.trace_id, request.step_id)
        assert winning_decision is not None, "IntegrityError implies a row now exists"
        raise _already_decided_error(request.trace_id, request.step_id, winning_decision) from None

    return ApprovalDecisionResponse(
        trace_id=request.trace_id,
        step_id=request.step_id,
        final_status=final_status,
        timestamp=timestamp,
    )


if __name__ == "__main__":
    # Lets a deployment platform (Render, Railway, Cloud Run, etc.) run this
    # directly (`python -m app.main`) and have it bind to whatever port the
    # platform assigns via the PORT env var, instead of a hardcoded one.
    # Local dev and the Dockerfile can still invoke uvicorn's own CLI
    # directly if preferred — this block only runs when main.py is executed
    # as a script, not when uvicorn imports `app.main:app` as a module.
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
