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

--limit caps how many NOT-YET-RUN traces this invocation processes (quota
is a real constraint here — see BUG-05 — so a single run doesn't have to
burn through every trace's model calls at once).

--results-file persists each trace's result as it completes; a later run
with the SAME --results-file skips every trace already recorded there
(a real "resumed from cache" skip, not a re-request) and only spends quota
on whatever's left — so --limit and --results-file together let a full
validation be split across several quota-constrained runs (e.g. across
several days) without re-paying for traces already scored. Without
--results-file, --limit still caps how many traces run, it just can't be
resumed later (nothing is persisted to skip on a future invocation).

Usage:
    python run_validation.py
    python run_validation.py --test-data-dir ../test-data/traces
    python run_validation.py --test-data-dir ../test-data/traces ../test-data/agentdojo_subset
    python run_validation.py --base-url http://127.0.0.1:8000
    python run_validation.py --results-file run1.json --limit 10   # today: first 10 new traces
    python run_validation.py --results-file run1.json --limit 10   # tomorrow: next 10 (first 10 skipped)
    python run_validation.py --results-file run1.json              # finish the rest, no cap
"""

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.config import RISK_APPROVAL_LOW_THRESHOLD, RISK_BLOCK_THRESHOLD  # noqa: E402

# How close (in risk_score points) to a gate threshold counts as "thin
# margin" and gets a WARNING printed. LLM judge/embedding output is not
# perfectly stable run-to-run (see validation_results.md's documented
# score variance across runs/models), so a step that happens to pass today
# by only a few points is a real regression risk even when the run as a
# whole is green -- this is a watch-item flag, not a failure.
MARGIN_WARNING_DISTANCE = 5
_GATE_THRESHOLDS = (RISK_APPROVAL_LOW_THRESHOLD, RISK_BLOCK_THRESHOLD)

# A single trace can now need several paced, retried model calls (see
# app/llm.py: pacing + up to MODEL_MAX_RETRIES retries with backoff on a
# 429/503); 30s was already too short before that and is worse now --
# BUG-05. Overridable with --timeout; irrelevant for the default
# in-process TestClient (which has no real network timeout to set).
DEFAULT_BASE_URL_TIMEOUT_SECONDS = 120.0


def _margin_warnings(file_name: str, outputs: list[dict]) -> list[str]:
    """Checks every step's risk_score against both gate thresholds
    (RISK_APPROVAL_LOW_THRESHOLD, RISK_BLOCK_THRESHOLD) and returns one
    warning string per step that landed within MARGIN_WARNING_DISTANCE
    points of either one -- regardless of whether the trace as a whole
    passed or failed, since a thin margin on a currently-passing step is
    exactly the kind of silent regression risk this is meant to surface.
    """
    warnings = []
    for output in outputs:
        risk_score = output["risk_score"]
        for threshold in _GATE_THRESHOLDS:
            distance = abs(risk_score - threshold)
            if distance <= MARGIN_WARNING_DISTANCE:
                warnings.append(
                    f"    WARNING: {file_name} step {output['step_id']} risk_score={risk_score} is only "
                    f"{distance} point(s) from threshold={threshold} (decision={output['decision']!r}) "
                    "-- thin margin, watch for regression on the next run."
                )
    return warnings


def _get_client(base_url: str | None, timeout: float):
    if base_url:
        import httpx

        return httpx.Client(base_url=base_url, timeout=timeout)

    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _load_trace_files(test_data_dirs: list[str]) -> list[str]:
    trace_files = []
    for test_data_dir in test_data_dirs:
        trace_files.extend(glob.glob(os.path.join(test_data_dir, "*.json")))
    return sorted(trace_files)


def _load_results_file(results_file: str | None) -> dict:
    if results_file and os.path.exists(results_file):
        with open(results_file) as f:
            return json.load(f)
    return {}


def _save_results_file(results_file: str | None, results: dict) -> None:
    if not results_file:
        return
    with open(results_file, "w") as f:
        json.dump(results, f, indent=2)


def _score_one_trace(client, file_path: str) -> dict:
    """Sends one trace file to /analyze-trace and scores the response
    against its `expected_flagged_steps`. Returns a dict in the same shape
    persisted to --results-file: `{"status": "passed"|"failed",
    "margin_warnings": [...], "failure": {...} if status == "failed"}`.
    """
    file_name = os.path.basename(file_path)

    with open(file_path) as f:
        data = json.load(f)
    expected_flagged_steps = set(data.pop("expected_flagged_steps", []))
    trace_id = data.get("trace_id", file_name)

    try:
        response = client.post("/analyze-trace", json=data)
        response.raise_for_status()
        outputs = response.json()
    except Exception as exc:
        return {
            "status": "failed",
            "margin_warnings": [],
            "failure": {"file": file_name, "trace_id": trace_id, "error": str(exc)},
        }

    margin_warnings = _margin_warnings(file_name, outputs)
    actual_flagged_steps = {o["step_id"] for o in outputs if o["decision"] != "allow_logged"}

    if actual_flagged_steps == expected_flagged_steps:
        return {"status": "passed", "margin_warnings": margin_warnings}

    return {
        "status": "failed",
        "margin_warnings": margin_warnings,
        "failure": {
            "file": file_name,
            "trace_id": trace_id,
            "expected_flagged_steps": sorted(expected_flagged_steps),
            "actual_flagged_steps": sorted(actual_flagged_steps),
            "missed": sorted(expected_flagged_steps - actual_flagged_steps),
            "extra": sorted(actual_flagged_steps - expected_flagged_steps),
            "outputs": outputs,
        },
    }


def run_validation(
    test_data_dirs: list[str],
    base_url: str | None,
    timeout: float = DEFAULT_BASE_URL_TIMEOUT_SECONDS,
    limit: int | None = None,
    results_file: str | None = None,
) -> int:
    trace_files = _load_trace_files(test_data_dirs)

    if not trace_files:
        print(f"No .json trace files found in {test_data_dirs}")
        return 0

    previous_results = _load_results_file(results_file)
    client = _get_client(base_url, timeout)

    total = 0
    passed = 0
    resumed = 0
    failures = []
    margin_warnings = []
    pending: list[str] = []
    processed_this_run = 0

    for file_path in trace_files:
        file_name = os.path.basename(file_path)
        cached = previous_results.get(file_name)

        if cached is None:
            if limit is not None and processed_this_run >= limit:
                pending.append(file_name)
                continue
            cached = _score_one_trace(client, file_path)
            previous_results[file_name] = cached
            processed_this_run += 1
            # Saved after every trace (not just at the end) so a crash,
            # timeout, or hitting a quota mid-run doesn't lose progress
            # already paid for.
            _save_results_file(results_file, previous_results)
        else:
            resumed += 1

        total += 1
        margin_warnings.extend(cached["margin_warnings"])
        if cached["status"] == "passed":
            passed += 1
        else:
            failures.append(cached["failure"])

    resumed_note = f" ({resumed} resumed from {results_file!r})" if resumed else ""
    print(f"\n{passed}/{total} traces handled correctly{resumed_note}\n")

    if pending:
        print(f"{len(pending)} trace(s) not yet run (--limit {limit} reached): {pending}")
        if results_file:
            print(f"Re-run with --results-file {results_file!r} (same path) to continue where this left off.\n")
        else:
            print("Pass --results-file to persist progress and continue this later.\n")

    if margin_warnings:
        print(f"{len(margin_warnings)} thin-margin warning(s) (within {MARGIN_WARNING_DISTANCE} points of a threshold):\n")
        for warning in margin_warnings:
            print(warning)
        print()

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
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_BASE_URL_TIMEOUT_SECONDS,
        help=f"Per-request timeout in seconds, for --base-url only (default: {DEFAULT_BASE_URL_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most this many NOT-YET-RUN trace files this invocation (quota-constrained runs).",
    )
    parser.add_argument(
        "--results-file",
        default=None,
        help=(
            "Persist each trace's result here (JSON) and skip any trace already recorded there on a "
            "later run with the same path, instead of re-spending quota on it -- combine with --limit "
            "to split a full validation across several quota-constrained runs."
        ),
    )
    args = parser.parse_args()

    sys.exit(run_validation(args.test_data_dir, args.base_url, args.timeout, args.limit, args.results_file))
