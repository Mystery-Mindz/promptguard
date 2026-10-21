"""Tests for run_validation.py's --limit and --results-file (resume)
behavior, and the thin-margin WARNING output. Runs the real /analyze-trace
endpoint in-process (no --base-url, no real network), with every model
call mocked -- no live API calls.
"""

import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, storage  # noqa: E402
from run_validation import run_validation  # noqa: E402


def _write_trace(path: str, trace_id: str, expected_flagged_steps: list[int]) -> None:
    """Writes a minimal one-step trace fixture. The actual risk_score is
    baked into the mocked judge/embedding response via the test's own
    patches, not into the file -- the file just needs a step for
    /analyze-trace to score."""
    with open(path, "w") as f:
        json.dump(
            {
                "trace_id": trace_id,
                "agent_id": "agent_A",
                "original_goal": "g",
                "expected_flagged_steps": expected_flagged_steps,
                "steps": [
                    {
                        "step_id": 1,
                        "actor": "agent_A",
                        "input_text": "hi",
                        "input_source": "user",
                        "input_provenance": "internal",
                        "action": "read_email",
                        "action_params": {},
                        "timestamp": "2026-01-01T00:00:00Z",
                    }
                ],
            },
            f,
        )


def _mock_llm(monkeypatch, judge_confidence: str, embed_vector: list[float]):
    monkeypatch.setattr("app.llm.judge", lambda prompt, **kwargs: judge_confidence)
    monkeypatch.setattr("app.llm.embed_many", lambda texts: [embed_vector for _ in texts])


def test_limit_caps_how_many_new_traces_run_this_invocation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    storage.init_db()
    _mock_llm(monkeypatch, judge_confidence="0.05", embed_vector=[1.0, 0.0])

    traces_dir = tmp_path / "traces"
    traces_dir.mkdir()
    for i in range(3):
        _write_trace(str(traces_dir / f"t{i}.json"), f"t{i}", [])

    exit_code = run_validation([str(traces_dir)], base_url=None, limit=1)

    assert exit_code == 0


def test_results_file_resumes_without_re_calling_the_model(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    storage.init_db()

    traces_dir = tmp_path / "traces"
    traces_dir.mkdir()
    for i in range(3):
        _write_trace(str(traces_dir / f"t{i}.json"), f"t{i}", [])

    results_file = str(tmp_path / "results.json")

    with patch("app.llm.judge", return_value="0.05") as mock_judge, patch(
        "app.llm.embed_many", side_effect=lambda texts: [[1.0, 0.0] for _ in texts]
    ) as mock_embed:
        run_validation([str(traces_dir)], base_url=None, limit=1, results_file=results_file)

    assert mock_judge.call_count == 1, "only the one trace within --limit 1 should have been scored"
    assert mock_embed.call_count == 1

    with open(results_file) as f:
        saved = json.load(f)
    assert len(saved) == 1, "--results-file must persist exactly the one trace actually run"

    # Second invocation, same results_file, no --limit: must finish the
    # other two WITHOUT re-calling the model for the first (already cached).
    with patch("app.llm.judge", return_value="0.05") as mock_judge_2, patch(
        "app.llm.embed_many", side_effect=lambda texts: [[1.0, 0.0] for _ in texts]
    ) as mock_embed_2:
        exit_code = run_validation([str(traces_dir)], base_url=None, results_file=results_file)

    assert exit_code == 0
    assert mock_judge_2.call_count == 2, "the first trace must be resumed from the results file, not re-scored"
    assert mock_embed_2.call_count == 2

    with open(results_file) as f:
        saved = json.load(f)
    assert len(saved) == 3, "all three traces must now be recorded"


def test_margin_warning_prints_for_a_thin_risk_score(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    storage.init_db()

    traces_dir = tmp_path / "traces"
    traces_dir.mkdir()
    _write_trace(str(traces_dir / "thin.json"), "thin", [1])

    # The mocked embed_many returns the SAME vector for the goal and the
    # step text, so drift_score is always 0 here -- risk_score is then
    # purely INJECTION_CONFIDENCE_WEIGHT * confidence. Solve for a
    # confidence that lands risk_score just 2 points above
    # RISK_APPROVAL_LOW_THRESHOLD, regardless of the weights currently
    # configured.
    target_risk_score = config.RISK_APPROVAL_LOW_THRESHOLD + 2
    confidence = target_risk_score / (100 * config.INJECTION_CONFIDENCE_WEIGHT)
    with (
        patch("app.llm.judge", return_value=str(confidence)),
        patch("app.llm.embed_many", side_effect=lambda texts: [[1.0, 0.0] for _ in texts]),
    ):
        run_validation([str(traces_dir)], base_url=None)

    captured = capsys.readouterr()
    assert "WARNING" in captured.out
    assert "thin.json" in captured.out
