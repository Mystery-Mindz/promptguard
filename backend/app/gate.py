from app.config import (
    RISK_APPROVAL_HIGH_THRESHOLD,
    RISK_APPROVAL_LOW_THRESHOLD,
    RISK_BLOCK_THRESHOLD,
)
from app.schemas import Decision, ProvenanceFlag

_DECISION_SEVERITY = {"allow_logged": 0, "approval_required": 1, "block": 2}


def decide(risk_score: int, provenance_flag: ProvenanceFlag) -> Decision:
    """Returns "block", "approval_required", or "allow_logged" for the given risk score and provenance flag.

    Applies the config.py thresholds (block above RISK_BLOCK_THRESHOLD,
    approval_required within the RISK_APPROVAL_LOW/HIGH_THRESHOLD band,
    otherwise allow_logged), then floors the result at approval_required
    whenever provenance_flag is "tainted" — regardless of risk_score — while
    never downgrading an existing "block".
    """
    if risk_score > RISK_BLOCK_THRESHOLD:
        decision: Decision = "block"
    elif RISK_APPROVAL_LOW_THRESHOLD <= risk_score <= RISK_APPROVAL_HIGH_THRESHOLD:
        decision = "approval_required"
    else:
        decision = "allow_logged"

    # "tainted" provenance floors the decision at approval_required, even if
    # risk_score alone would allow it through. Never downgrades an existing
    # "block".
    if provenance_flag == "tainted" and _DECISION_SEVERITY[decision] < _DECISION_SEVERITY["approval_required"]:
        return "approval_required"

    return decision
