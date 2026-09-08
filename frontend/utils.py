import requests


BACKEND_URL = "http://172.16.129.37:8000"
    

def analyze_trace(trace):
    response = requests.post(
        f"{BACKEND_URL}/analyze-trace",
        json=trace,
        timeout=90,
    )

    response.raise_for_status()

    return response.json()

def get_pending_approvals():
    response = requests.get(
        f"{BACKEND_URL}/pending-approvals",
        timeout=30,
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
        timeout=30,
    )
    response.raise_for_status()
    return response.json()
