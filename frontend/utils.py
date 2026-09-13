import requests

from config import ANALYZE_TRACE_TIMEOUT_SECONDS, BACKEND_URL, DEFAULT_TIMEOUT_SECONDS


def analyze_trace(trace):
    response = requests.post(
        f"{BACKEND_URL}/analyze-trace",
        json=trace,
        timeout=ANALYZE_TRACE_TIMEOUT_SECONDS,
    )

    response.raise_for_status()

    return response.json()

def get_trace(trace_id):
    """Fetches a previously-analyzed trace and its detections by ID.
    Returns None if trace_id was never analyzed (404), so the caller can show
    a friendly message instead of an exception.
    """
    response = requests.get(
        f"{BACKEND_URL}/trace/{trace_id}",
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()

def get_pending_approvals():
    response = requests.get(
        f"{BACKEND_URL}/pending-approvals",
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()

def submit_approval(trace_id, step_id, operator_decision, operator_id):
    response = requests.post(
        f"{BACKEND_URL}/approval-decision",
        json={
            "trace_id": trace_id,
            "step_id": step_id,
            "operator_decision": operator_decision,
            "operator_id": operator_id,
        },
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()
