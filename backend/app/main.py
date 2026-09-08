from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import storage
from app.agent import run_agent
from app.config import RISK_APPROVAL_HIGH_THRESHOLD, RISK_APPROVAL_LOW_THRESHOLD, RISK_BLOCK_THRESHOLD
from app.drift_engine import compute_drift_and_risk
from app.gate import decide
from app.provenance import get_provenance_flag
from app.schemas import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    Classification,
    DetectionOutput,
    PendingApproval,
    ProvenanceFlag,
    RunAgentRequest,
    Trace,
    TraceStep,
)

app = FastAPI(title="PromptGuard")

# Allow the frontend (Streamlit, typically on 8501/8502) to call this API
# from any localhost port — the two run as separate processes/ports.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1):\d+$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

storage.init_db()


# TEMPORARY DEBUG LOGGING — remove once the /analyze-trace 422s reported by
# Person 2's dashboard are diagnosed. Logs the exact raw request body that
# failed validation (not just "it failed"), so we can see what was actually
# sent instead of guessing. Only fires for /analyze-trace; every other
# route still gets FastAPI's normal validation-error response.
@app.exception_handler(RequestValidationError)
async def _log_analyze_trace_validation_errors(request: Request, exc: RequestValidationError) -> JSONResponse:
    if request.url.path == "/analyze-trace":
        raw_body = await request.body()
        print("=" * 70)
        print(f"[422 DEBUG] {request.method} {request.url.path} failed validation")
        print(f"[422 DEBUG] raw request body:\n{raw_body.decode('utf-8', errors='replace')}")
        print(f"[422 DEBUG] validation errors: {exc.errors()}")
        print("=" * 70)
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.post("/run-agent", response_model=Trace)
def run_agent_endpoint(request: RunAgentRequest) -> Trace:
    return run_agent(request.original_goal, request.trace_id, request.agent_id)


def _classification_for(risk_score: int) -> Classification:
    if risk_score > RISK_BLOCK_THRESHOLD:
        return "malicious"
    if RISK_APPROVAL_LOW_THRESHOLD <= risk_score <= RISK_APPROVAL_HIGH_THRESHOLD:
        return "suspicious"
    return "clean"


def _explanation_for(step: TraceStep, drift_score: float, risk_score: int, provenance_flag: ProvenanceFlag) -> str:
    reasons = [f"drift_score={drift_score:.2f}", f"risk_score={risk_score}"]
    if provenance_flag == "tainted":
        reasons.append("input traces back to external content via an agent handoff")
    elif provenance_flag == "external":
        reasons.append("input is externally sourced")
    return f"Step {step.step_id} ({step.action}): " + ", ".join(reasons)


@app.post("/analyze-trace", response_model=list[DetectionOutput])
def analyze_trace(trace: Trace) -> list[DetectionOutput]:
    outputs: list[DetectionOutput] = []

    for step in trace.steps:
        drift_score, risk_score = compute_drift_and_risk(trace, step.step_id)
        provenance_flag = get_provenance_flag(trace, step.step_id)
        decision = decide(risk_score, provenance_flag)

        output = DetectionOutput(
            trace_id=trace.trace_id,
            step_id=step.step_id,
            drift_score=drift_score,
            risk_score=risk_score,
            provenance_flag=provenance_flag,
            classification=_classification_for(risk_score),
            explanation=_explanation_for(step, drift_score, risk_score, provenance_flag),
            decision=decision,
        )
        storage.save_detection_output(output)
        outputs.append(output)

    return outputs


@app.get("/pending-approvals", response_model=list[PendingApproval])
def pending_approvals_endpoint() -> list[PendingApproval]:
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


@app.post("/approval-decision", response_model=ApprovalDecisionResponse)
def approval_decision_endpoint(request: ApprovalDecisionRequest) -> ApprovalDecisionResponse:
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

    final_status: str = "approved_and_allowed" if request.operator_decision == "approved" else "denied_and_blocked"
    timestamp = datetime.now(timezone.utc)

    storage.save_approval_decision(
        trace_id=request.trace_id,
        step_id=request.step_id,
        operator_decision=request.operator_decision,
        operator_id=request.operator_id,
        final_status=final_status,
        timestamp=timestamp.isoformat(),
    )

    return ApprovalDecisionResponse(
        trace_id=request.trace_id,
        step_id=request.step_id,
        final_status=final_status,
        timestamp=timestamp,
    )
