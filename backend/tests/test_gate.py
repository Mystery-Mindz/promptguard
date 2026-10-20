from app.config import DESTRUCTIVE_ACTIONS, RISK_APPROVAL_LOW_THRESHOLD
from app.gate import classification_for, decide


def test_low_risk_internal_allows_logged():
    assert decide(10, "internal") == "allow_logged"


def test_high_risk_blocks():
    assert decide(90, "internal") == "block"


def test_mid_risk_requires_approval():
    assert decide(60, "internal") == "approval_required"


def test_low_risk_tainted_floors_at_approval_required():
    assert decide(10, "tainted") == "approval_required"


def test_threshold_boundaries():
    assert decide(39, "internal") == "allow_logged"
    assert decide(40, "internal") == "approval_required"
    assert decide(80, "internal") == "approval_required"
    assert decide(81, "internal") == "block"


def test_tainted_does_not_downgrade_a_block():
    assert decide(90, "tainted") == "block"


# --- action-severity rule: destructive action + external/tainted + score >= RISK_APPROVAL_LOW_THRESHOLD -> block ---


def test_destructive_action_external_at_low_threshold_blocks():
    assert decide(RISK_APPROVAL_LOW_THRESHOLD, "external", "delete_file") == "block"


def test_destructive_action_tainted_at_low_threshold_blocks():
    assert decide(RISK_APPROVAL_LOW_THRESHOLD, "tainted", "send_message") == "block"


def test_destructive_action_one_point_below_threshold_does_not_block():
    """Just under RISK_APPROVAL_LOW_THRESHOLD, the destructive action alone
    isn't enough -- the tainted floor still applies (approval_required),
    but score-based escalation to block does not fire."""
    assert decide(RISK_APPROVAL_LOW_THRESHOLD - 1, "external", "delete_file") == "allow_logged"
    assert decide(RISK_APPROVAL_LOW_THRESHOLD - 1, "tainted", "send_message") == "approval_required"


def test_destructive_action_internal_provenance_does_not_block():
    """Provenance must be external or tainted -- a destructive action on
    genuinely internal, trusted content is not escalated."""
    assert decide(60, "internal", "delete_file") == "approval_required"


def test_non_destructive_action_external_high_score_does_not_escalate():
    """Action-severity escalation only fires for actions in
    DESTRUCTIVE_ACTIONS -- a non-destructive action at the same score/
    provenance stays at approval_required."""
    assert "read_email" not in DESTRUCTIVE_ACTIONS
    assert decide(60, "external", "read_email") == "approval_required"


def test_no_action_provided_does_not_escalate():
    """`action` is optional; omitting it must not trigger the
    action-severity escalation (treated as "not destructive")."""
    assert decide(60, "external") == "approval_required"
    assert decide(60, "tainted") == "approval_required"


def test_destructive_action_already_above_block_threshold_stays_block():
    assert decide(90, "external", "delete_file") == "block"


def test_destructive_action_does_not_apply_below_low_threshold_even_if_tainted_floor_would_otherwise_allow():
    """A tainted-but-low-score step floors at approval_required, not
    block, even though its action is destructive -- the action-severity
    rule never fires below RISK_APPROVAL_LOW_THRESHOLD."""
    assert decide(0, "tainted", "delete_file") == "approval_required"


# --- classification agrees with the final decision (BUG-11) ---------------


def test_classification_matches_block():
    assert classification_for("block") == "malicious"


def test_classification_matches_approval_required():
    assert classification_for("approval_required") == "suspicious"


def test_classification_matches_allow_logged():
    assert classification_for("allow_logged") == "clean"


def test_classification_agrees_with_tainted_floor_decision():
    """BUG-11: a tainted step floored to approval_required by a low
    risk_score must be classified "suspicious", never "clean"."""
    decision = decide(10, "tainted")
    assert decision == "approval_required"
    assert classification_for(decision) == "suspicious"


def test_classification_agrees_with_action_severity_escalation():
    decision = decide(RISK_APPROVAL_LOW_THRESHOLD, "external", "delete_file")
    assert decision == "block"
    assert classification_for(decision) == "malicious"
