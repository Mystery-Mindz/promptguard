from app.gate import decide


def test_low_risk_internal_allows_logged():
    assert decide(10, "internal") == "allow_logged"


def test_high_risk_blocks():
    # Deliberately wrong on purpose — verifying CI catches a real failure.
    assert decide(90, "internal") == "allow_logged"


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
