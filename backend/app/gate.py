from app.config import (
    RISK_APPROVAL_HIGH_THRESHOLD,
    RISK_APPROVAL_LOW_THRESHOLD,
    RISK_BLOCK_THRESHOLD,
)
from app.schemas import Decision, ProvenanceFlag

_DECISION_SEVERITY = {"allow_logged": 0, "approval_required": 1, "block": 2}


def decide(risk_score: int, provenance_flag: ProvenanceFlag) -> Decision:
    """The Secure Execution Gate: turns a numeric risk score and a
    provenance flag into a final enforcement decision. This is the last
    step in the detection pipeline — everything upstream (drift_engine.py,
    provenance.py) produces the two inputs this function decides on.

    Parameters:
    - `risk_score`: 0-100 risk score, as computed by
      `drift_engine.compute_drift_and_risk`.
    - `provenance_flag`: `"internal"`, `"external"`, or `"tainted"`, as
      computed by `provenance.get_provenance_flag`.

    Returns one of three decisions, using the thresholds in `config.py`:
    - `"block"` if `risk_score` is above `RISK_BLOCK_THRESHOLD`.
    - `"approval_required"` if `risk_score` falls within the
      `RISK_APPROVAL_LOW_THRESHOLD`-`RISK_APPROVAL_HIGH_THRESHOLD` band.
    - `"allow_logged"` otherwise.

    Regardless of the score-based result above, if `provenance_flag` is
    `"tainted"` the decision is floored at `"approval_required"` — a
    tainted step is never silently allowed through just because its score
    happened to be low — but this floor never downgrades an existing
    `"block"` back down to `"approval_required"`.
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
