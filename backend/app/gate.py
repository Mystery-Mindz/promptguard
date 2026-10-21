from app.config import (
    DESTRUCTIVE_ACTIONS,
    RISK_APPROVAL_HIGH_THRESHOLD,
    RISK_APPROVAL_LOW_THRESHOLD,
    RISK_BLOCK_THRESHOLD,
)
from app.schemas import Classification, Decision, ProvenanceFlag, TraceStep

_DECISION_SEVERITY = {"allow_logged": 0, "approval_required": 1, "block": 2}


def explanation_for_step(step: TraceStep, drift_score: float, risk_score: int, provenance_flag: ProvenanceFlag) -> str:
    """Builds the plain-English `explanation` string shown alongside a
    step's real (non-mock) detection result, so a human reviewer can see at
    a glance why a step was scored the way it was. Shared by
    `/analyze-trace` (`main.py`) and the real `/guard/*` pipeline
    (`guard.py`) so both report explanations in the same style.

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


def classification_for(decision: Decision) -> Classification:
    """Maps a final enforcement decision to its human-readable label, so
    the label shown alongside a decision always agrees with it (BUG-11: a
    tainted or action-severity step floored/escalated by `decide` must not
    report a classification that contradicts the decision it's shown next
    to — e.g. `approval_required` labelled `"clean"`). Shared by
    `/analyze-trace` and `/guard/*` so both ways of scoring a step report a
    classification with the same meaning.

    Parameters:
    - `decision`: the final decision from `decide`, for the SAME step.

    Returns `"malicious"` for `"block"`, `"suspicious"` for
    `"approval_required"`, `"clean"` for `"allow_logged"` — a direct,
    always-consistent mapping rather than a second, independent read of
    `risk_score` that could disagree with whatever floored or escalated
    the actual decision.
    """
    if decision == "block":
        return "malicious"
    if decision == "approval_required":
        return "suspicious"
    return "clean"


def decide(risk_score: int, provenance_flag: ProvenanceFlag, action: str | None = None) -> Decision:
    """The Secure Execution Gate: turns a numeric risk score, a provenance
    flag, and (optionally) the action being taken into a final enforcement
    decision. This is the last step in the detection pipeline — everything
    upstream (drift_engine.py, provenance.py) produces the inputs this
    function decides on.

    Parameters:
    - `risk_score`: 0-100 risk score, as computed by
      `drift_engine.compute_drift_and_risk`.
    - `provenance_flag`: `"internal"`, `"external"`, or `"tainted"`, as
      computed by `provenance.get_provenance_flag`.
    - `action`: the action name this step is about to take (e.g.
      `"delete_file"`), if known. Optional and defaults to `None` (treated
      as "not destructive") since not every caller has an action to offer.

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

    On top of that floor, an action-severity rule escalates straight to
    `"block"` when `action` is in `config.DESTRUCTIVE_ACTIONS` AND
    `provenance_flag` is `"external"` or `"tainted"` AND `risk_score` is
    already at least `RISK_APPROVAL_LOW_THRESHOLD`: the real judge/drift
    pipeline's injected-instruction attacks currently top out well under
    `RISK_BLOCK_THRESHOLD` (see test-data/validation_results.md), so an
    untrusted step about to take an irreversible, world-affecting action
    shouldn't have to cross 80 on score alone before it's blocked outright.
    This never fires on a low (`< RISK_APPROVAL_LOW_THRESHOLD`) score, so a
    merely-tainted-but-benign step still only floors at
    `"approval_required"`, same as before — it only ever escalates a step
    that's already at least `"approval_required"` by score, never promotes
    a genuinely low-risk one.
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
        decision = "approval_required"

    # Action-severity escalation: a destructive action on untrusted content
    # that's already at least at the approval-required score band gets
    # blocked outright, rather than waiting on risk_score alone to clear
    # RISK_BLOCK_THRESHOLD.
    if (
        decision != "block"
        and action in DESTRUCTIVE_ACTIONS
        and provenance_flag in ("external", "tainted")
        and risk_score >= RISK_APPROVAL_LOW_THRESHOLD
    ):
        decision = "block"

    return decision
