import streamlit as st
from config import APPROVAL_TOKEN
from utils import submit_approval, get_pending_approvals


# ---------------------------------------------------------
# Session state
# ---------------------------------------------------------

if "approval_status" not in st.session_state:
    st.session_state.approval_status = "pending"


# ---------------------------------------------------------
# Page configuration
# ---------------------------------------------------------

st.set_page_config(
    page_title="PromptGuard - Operator Approval",
    page_icon="🔐",
    layout="wide",
)


# ---------------------------------------------------------
# Demo authentication — a single shared token (see config.py), not real
# per-operator auth. Intentional for the hackathon demo, not an accidental
# leftover; swap for real auth before this ever guards non-demo data.
# ---------------------------------------------------------

st.title("🔐 PromptGuard")
st.caption("Operator Approval Console")

st.divider()

st.markdown("## Operator Authentication")

token = st.text_input(
    "Enter operator token",
    type="password",
)

if token != APPROVAL_TOKEN:
    st.warning("Authentication required.")
    st.stop()


# ---------------------------------------------------------
# Authenticated operator area
# ---------------------------------------------------------

st.success("Authenticated successfully.")

st.markdown("## Approval Requests")

pending_approvals = get_pending_approvals()

if not pending_approvals:
    st.success("No approval requests are currently pending.")
    st.stop()

approval_request = pending_approvals[0]


# ---------------------------------------------------------
# Approval request details
# ---------------------------------------------------------

st.warning("⚠️ **Operator approval required**")

st.markdown(
    f"### Step {approval_request['step_id']}"
)


# ---------------------------------------------------------
# Risk / decision summary
# ---------------------------------------------------------

col1, col2, col3 = st.columns(3)

with col1:
    st.metric(
        "Risk Score",
        f"{approval_request['risk_score']}/100"
    )

with col2:
    st.metric(
        "Drift Score",
        "See reasoning"
    )

with col3:
    st.metric(
        "Decision",
        "APPROVAL_REQUIRED"
    )


# ---------------------------------------------------------
# Approval request information
# ---------------------------------------------------------

st.markdown("### Approval Request")

st.write(
    f"**Trace ID:** `{approval_request['trace_id']}`"
)

st.write(
    f"**Step ID:** `{approval_request['step_id']}`"
)

st.write(
    f"**Timestamp:** `{approval_request['timestamp']}`"
)

st.write(
    approval_request["explanation"]
)

st.write(
    f"**Provenance:** "
    f"`{approval_request['provenance_flag']}`"
)

st.write(
    "**Classification:** "
    "Not provided by pending-approvals endpoint"
)


# ---------------------------------------------------------
# Operator decision
# ---------------------------------------------------------

st.markdown("### Operator Decision")

approve_col, deny_col = st.columns(2)


# ---------------------------------------------------------
# Approve
# ---------------------------------------------------------

with approve_col:
    if st.button(
        "✅ Approve Action",
        use_container_width=True
    ):
        response = submit_approval(
            trace_id=approval_request["trace_id"],
            step_id=approval_request["step_id"],
            operator_decision="approved",
            operator_id="reviewer_jane",
        )

        st.session_state.approval_status = "approved"
        st.session_state.approval_response = response

        st.rerun()


# ---------------------------------------------------------
# Deny
# ---------------------------------------------------------

with deny_col:
    if st.button(
        "❌ Deny Action",
        use_container_width=True
    ):
        response = submit_approval(
            trace_id=approval_request["trace_id"],
            step_id=approval_request["step_id"],
            operator_decision="denied",
            operator_id="reviewer_jane",
        )

        st.session_state.approval_status = "denied"
        st.session_state.approval_response = response

        st.rerun()


# ---------------------------------------------------------
# Decision status
# ---------------------------------------------------------

if st.session_state.approval_status == "approved":
    st.success(
        "✅ **Action approved by the operator.**"
    )

elif st.session_state.approval_status == "denied":
    st.error(
        "❌ **Action denied by the operator.**"
    )

else:
    st.info(
        "⏳ Waiting for operator decision."
    )

