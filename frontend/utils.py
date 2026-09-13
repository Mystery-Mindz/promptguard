import requests

from config import ANALYZE_TRACE_TIMEOUT_SECONDS, BACKEND_URL, DEFAULT_TIMEOUT_SECONDS


def analyze_trace(trace):
    """Sends a trace to the backend's detection pipeline and gets back a
    risk assessment for every step.

    Parameters:
    - `trace`: a dict matching the shared trace JSON schema (trace_id,
      agent_id, original_goal, and a list of steps).

    Returns the parsed JSON response: a list of per-step detection results
    (drift_score, risk_score, provenance_flag, classification, decision,
    explanation). Raises an exception if the backend request fails (e.g.
    connection error or non-2xx response).
    """
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

    Parameters:
    - `trace_id`: the trace to look up.

    Returns a dict with `"trace"` and `"detections"` keys (matching the
    backend's `GET /trace/{trace_id}` response) if found, or `None` if the
    backend responds 404. Raises an exception for any other error response.
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
    """Fetches the current list of steps awaiting a human operator's
    approve/deny decision.

    Takes no parameters. Returns the parsed JSON response: a list of
    pending-approval dicts (trace_id, step_id, risk_score, provenance_flag,
    explanation, timestamp), oldest first. Raises an exception if the
    backend request fails.
    """
    response = requests.get(
        f"{BACKEND_URL}/pending-approvals",
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()

def submit_approval(trace_id, step_id, operator_decision, operator_id):
    """Sends a human operator's approve/deny decision for one flagged step
    to the backend.

    Parameters:
    - `trace_id`, `step_id`: identify which step is being decided on.
    - `operator_decision`: `"approved"` or `"denied"`.
    - `operator_id`: who is making the decision.

    Returns the parsed JSON response (the resulting `final_status` and
    timestamp). Raises an exception if the backend rejects the request
    (e.g. 404 if the step was never analyzed, 409 if it wasn't flagged for
    approval or was already decided) or if the request otherwise fails.
    """
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
