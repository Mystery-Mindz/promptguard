"""End-to-end check of a RUNNING PromptGuard backend (local or deployed).

Uses real Gemini calls: about 3 judge calls + 6 embedding calls for the demo
trace, plus 1–6 agent calls if you pass --with-agent. Mind the free-tier
daily quota.

Usage (from backend/, with the server already running):
    python scripts/live_check.py
    python scripts/live_check.py --base-url https://your-backend.onrender.com --with-agent
"""

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import requests

DEMO_TRACE = Path(__file__).resolve().parents[2] / "test-data" / "demo_cross_agent_handoff.json"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""), flush=True)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--with-agent", action="store_true", help="also call /run-agent (uses agent-model quota)")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    # 1. Health (allow for a sleeping free-tier host)
    for attempt in range(6):
        try:
            r = requests.get(f"{base}/health", timeout=60)
            break
        except requests.RequestException as exc:
            if attempt == 5:
                check("GET /health", False, str(exc))
                return 1
            time.sleep(10)
    if not check("GET /health", r.status_code == 200 and r.json().get("status") == "ok", r.text[:100]):
        return 1

    # 2. Analyse the headline cross-agent trace under a fresh ID
    trace = json.loads(DEMO_TRACE.read_text())
    trace.pop("expected_flagged_steps", None)
    trace_id = f"live_check_{uuid.uuid4().hex[:8]}"
    trace["trace_id"] = trace_id
    print(f"...analysing {len(trace['steps'])}-step demo trace as {trace_id} (can take 1–3 min due to pacing)")
    started = time.monotonic()
    r = requests.post(f"{base}/analyze-trace", json=trace, timeout=600)
    elapsed = time.monotonic() - started
    if not check("POST /analyze-trace", r.status_code == 200, f"{r.status_code} in {elapsed:.0f}s {r.text[:200]}"):
        return 1
    detections = {d["step_id"]: d for d in r.json()}
    for d in detections.values():
        print(f"      step {d['step_id']}: risk={d['risk_score']} drift={d['drift_score']:.2f} "
              f"prov={d['provenance_flag']} decision={d['decision']}")
    last = max(detections)
    check("handoff step is tainted", detections[last]["provenance_flag"] == "tainted", str(detections[last]))
    check("handoff step is not auto-allowed", detections[last]["decision"] != "allow_logged")
    check("analysis latency under 90s (dashboard timeout)", elapsed < 90, f"{elapsed:.0f}s")

    # 3. Read back
    r = requests.get(f"{base}/trace/{trace_id}", timeout=30)
    check("GET /trace/{id}", r.status_code == 200 and len(r.json()["detections"]) == len(trace["steps"]))
    check("GET /trace/unknown → 404", requests.get(f"{base}/trace/nope_{uuid.uuid4().hex}", timeout=30).status_code == 404)

    # 4. Approval flow
    pending = requests.get(f"{base}/pending-approvals", timeout=30).json()
    mine = [p for p in pending if p["trace_id"] == trace_id]
    check("flagged steps appear in /pending-approvals", len(mine) >= 1, f"{len(mine)} pending")
    if mine:
        body = {"trace_id": trace_id, "step_id": mine[0]["step_id"], "operator_decision": "denied", "operator_id": "live_check"}
        r1 = requests.post(f"{base}/approval-decision", json=body, timeout=30)
        check("POST /approval-decision → 200 denied_and_blocked",
              r1.status_code == 200 and r1.json()["final_status"] == "denied_and_blocked", r1.text[:150])
        r2 = requests.post(f"{base}/approval-decision", json=body, timeout=30)
        check("second decision on same step → 409", r2.status_code == 409)
        after = requests.get(f"{base}/pending-approvals", timeout=30).json()
        check("decided step leaves the queue",
              not any(p["trace_id"] == trace_id and p["step_id"] == mine[0]["step_id"] for p in after))
    r = requests.post(f"{base}/approval-decision", timeout=30,
                      json={"trace_id": f"nope_{uuid.uuid4().hex}", "step_id": 1, "operator_decision": "approved"})
    check("decision on unknown step → 404", r.status_code == 404)

    # 5. Optional: real agent run
    if args.with_agent:
        agent_id = f"live_agent_{uuid.uuid4().hex[:8]}"
        print("...running the agent (real Gemini function calling)")
        r = requests.post(f"{base}/run-agent", timeout=600,
                          json={"original_goal": "Read my inbox and summarise anything urgent", "trace_id": agent_id})
        ok = check("POST /run-agent", r.status_code == 200, r.text[:200])
        if ok:
            steps = r.json()["steps"]
            print("      agent actions:", [s["action"] for s in steps])
            check("agent recorded the email content as an external step",
                  any(s["action"] == "tool_output_received" and s["input_provenance"] == "external" for s in steps))

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
