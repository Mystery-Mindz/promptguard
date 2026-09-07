from fastapi import FastAPI

from app.agent import run_agent
from app.config import RISK_APPROVAL_HIGH_THRESHOLD, RISK_APPROVAL_LOW_THRESHOLD, RISK_BLOCK_THRESHOLD
from app.drift_engine import compute_drift_and_risk
from app.gate import decide
from app.provenance import get_provenance_flag
from app.schemas import Classification, DetectionOutput, ProvenanceFlag, RunAgentRequest, Trace, TraceStep

app = FastAPI(title="PromptGuard")


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

        outputs.append(
            DetectionOutput(
                trace_id=trace.trace_id,
                step_id=step.step_id,
                drift_score=drift_score,
                risk_score=risk_score,
                provenance_flag=provenance_flag,
                classification=_classification_for(risk_score),
                explanation=_explanation_for(step, drift_score, risk_score, provenance_flag),
                decision=decision,
            )
        )

    return outputs
