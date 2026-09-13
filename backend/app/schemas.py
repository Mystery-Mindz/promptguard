from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from app.config import DEFAULT_TRACE_ID

InputSource = Literal["user", "tool_output", "document", "agent_handoff"]
InputProvenance = Literal["internal", "external"]
ProvenanceFlag = Literal["internal", "external", "tainted"]
Classification = Literal["clean", "suspicious", "malicious"]
Decision = Literal["block", "approval_required", "allow_logged"]
OperatorDecision = Literal["approved", "denied"]
FinalStatus = Literal["approved_and_allowed", "denied_and_blocked"]


class TraceStep(BaseModel):
    step_id: int
    actor: str
    input_text: str
    input_source: InputSource
    input_provenance: InputProvenance
    action: str
    action_params: dict[str, Any]
    timestamp: datetime


class Trace(BaseModel):
    trace_id: str
    agent_id: str
    original_goal: str
    steps: list[TraceStep]


class DetectionOutput(BaseModel):
    trace_id: str
    step_id: int
    drift_score: float
    risk_score: int
    provenance_flag: ProvenanceFlag
    classification: Classification
    explanation: str
    decision: Decision


class RunAgentRequest(BaseModel):
    original_goal: str
    trace_id: str = DEFAULT_TRACE_ID
    agent_id: str = "agent_A"


class ApprovalDecisionRequest(BaseModel):
    trace_id: str
    step_id: int
    operator_decision: OperatorDecision
    operator_id: str | None = None


class ApprovalDecisionResponse(BaseModel):
    trace_id: str
    step_id: int
    final_status: FinalStatus
    timestamp: datetime


class PendingApproval(BaseModel):
    trace_id: str
    step_id: int
    risk_score: int
    provenance_flag: ProvenanceFlag
    explanation: str
    timestamp: datetime


class TraceWithDetections(BaseModel):
    trace: Trace
    detections: list[DetectionOutput]
