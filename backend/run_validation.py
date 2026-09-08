"""
Validates PromptGuard's /analyze-trace endpoint against a folder of
synthetic test traces.

Each trace file is the standard trace JSON (see CLAUDE.md) plus one extra
field, `expected_flagged_steps`: a list of step_ids that should NOT come
back as `allow_logged` (i.e. the steps a correct run should flag with
`approval_required` or `block`). That field is stripped before the trace is
sent to the API — it's test metadata, not part of the shared trace schema.

By default this runs the app in-process via FastAPI's TestClient (no server
needs to be running). Pass --base-url to hit an already-running server
instead.

--test-data-dir accepts one or more directories (each scanned with a flat,
non-recursive glob, so nested subfolders within a given directory are NOT
picked up automatically). The default scans both ../test-data/traces and
../test-data/agentdojo_subset.

Usage:
    python run_validation.py
    python run_validation.py --test-data-dir ../test-data/traces
    python run_validation.py --test-data-dir ../test-data/traces ../test-data/agentdojo_subset
    python run_validation.py --base-url http://127.0.0.1:8000
"""

import argparse
import glob
import json
import os
import sys


def _get_client(base_url: str | None):
    if base_url:
        import httpx

        return httpx.Client(base_url=base_url, timeout=30)

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _load_trace_files(test_data_dirs: list[str]) -> list[str]:
    trace_files = []
    for test_data_dir in test_data_dirs:
        trace_files.extend(glob.glob(os.path.join(test_data_dir, "*.json")))
    return sorted(trace_files)


def run_validation(test_data_dirs: list[str], base_url: str | None) -> int:
    trace_files = _load_trace_files(test_data_dirs)

    if not trace_files:
        print(f"No .json trace files found in {test_data_dirs}")
        return 0

    client = _get_client(base_url)

    total = 0
    passed = 0
    failures = []

    for file_path in trace_files:
        file_name = os.path.basename(file_path)

        with open(file_path) as f:
            data = json.load(f)

        expected_flagged_steps = set(data.pop("expected_flagged_steps", []))
        trace_id = data.get("trace_id", file_name)
        total += 1

        try:
            response = client.post("/analyze-trace", json=data)
            response.raise_for_status()
            outputs = response.json()
        except Exception as exc:
            failures.append(
                {
                    "file": file_name,
                    "trace_id": trace_id,
                    "error": str(exc),
                }
            )
            continue

        actual_flagged_steps = {o["step_id"] for o in outputs if o["decision"] != "allow_logged"}

        if actual_flagged_steps == expected_flagged_steps:
            passed += 1
            continue

        failures.append(
            {
                "file": file_name,
                "trace_id": trace_id,
                "expected_flagged_steps": sorted(expected_flagged_steps),
                "actual_flagged_steps": sorted(actual_flagged_steps),
                "missed": sorted(expected_flagged_steps - actual_flagged_steps),
                "extra": sorted(actual_flagged_steps - expected_flagged_steps),
                "outputs": outputs,
            }
        )

    print(f"\n{passed}/{total} traces handled correctly\n")

    if failures:
        print(f"{len(failures)} failing trace(s):\n")
        for failure in failures:
            print(f"- {failure['file']} (trace_id={failure['trace_id']})")

            if "error" in failure:
                print(f"    request error: {failure['error']}")
                print()
                continue

            print(f"    expected flagged steps: {failure['expected_flagged_steps']}")
            print(f"    actual flagged steps:   {failure['actual_flagged_steps']}")
            if failure["missed"]:
                print(f"    MISSED — should have been flagged, came back allow_logged: {failure['missed']}")
            if failure["extra"]:
                print(f"    EXTRA — flagged but should have been allow_logged: {failure['extra']}")

            relevant_step_ids = set(failure["missed"]) | set(failure["extra"])
            for output in failure["outputs"]:
                if output["step_id"] in relevant_step_ids:
                    print(
                        f"      step {output['step_id']}: drift_score={output['drift_score']:.2f}, "
                        f"risk_score={output['risk_score']}, provenance_flag={output['provenance_flag']}, "
                        f"decision={output['decision']}"
                    )
            print()

    return 0 if not failures else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    parser.add_argument(
        "--test-data-dir",
        nargs="+",
        default=[
            os.path.join(_script_dir, "..", "test-data", "traces"),
            os.path.join(_script_dir, "..", "test-data", "agentdojo_subset"),
        ],
        help=(
            "One or more directories containing *.json trace files (flat glob, "
            "not recursive). Default: ../test-data/traces and "
            "../test-data/agentdojo_subset relative to this script."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Base URL of a running server to hit instead of the in-process app (e.g. http://127.0.0.1:8000)",
    )
    args = parser.parse_args()

    sys.exit(run_validation(args.test_data_dir, args.base_url))
