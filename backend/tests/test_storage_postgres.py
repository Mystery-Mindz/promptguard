"""Postgres-backed storage tests (Prompt 4.2) -- only run when
TEST_DATABASE_URL is set. Exercises the exact same app/storage.py public
functions the SQLite-backed test suite does, against a REAL Postgres
database, to prove the SQLAlchemy Core rewrite behaves identically on
both backends (upserts, the unique-constraint race on approval_decisions/
operators, the correlated-subquery list_sessions query, the join-based
get_pending_approvals query) -- not just that it doesn't crash on SQLite.

Run locally against a throwaway Postgres (e.g. via Docker):
    docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=test \\
        -e POSTGRES_DB=promptguard_test postgres:16-alpine
    TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/promptguard_test \\
        pytest tests/test_storage_postgres.py -v

No live model-API calls -- this file only exercises storage.py.
"""

import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app import config, storage
from app.schemas import DetectionOutput, Trace, TraceStep

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="set TEST_DATABASE_URL to a real Postgres URL to run these tests"
)


@pytest.fixture(autouse=True)
def postgres_db(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", TEST_DATABASE_URL)
    storage.init_db()


def _unique_id(prefix: str) -> str:
    # Real, persistent Postgres -- unlike SQLite's tests (a fresh tmp_path
    # file every time), so test data needs its own unique IDs rather than
    # relying on a throwaway database. Deleted at the end of each test.
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _make_step(action: str = "read_email", action_params: dict | None = None, input_text: str = "hi") -> TraceStep:
    return TraceStep(
        step_id=1,
        actor="agent_A",
        input_text=input_text,
        input_source="user",
        input_provenance="internal",
        action=action,
        action_params=action_params or {},
        timestamp=datetime.now(UTC),
    )


def test_init_db_is_idempotent():
    storage.init_db()
    storage.init_db()  # must not raise -- CREATE TABLE IF NOT EXISTS, every time


def test_save_and_get_trace_roundtrip():
    trace_id = _unique_id("trace")
    trace = Trace(trace_id=trace_id, agent_id="agent_A", original_goal="goal", steps=[_make_step()])
    try:
        storage.save_trace(trace)
        got = storage.get_trace(trace_id)
        assert got is not None
        assert got.original_goal == "goal"
        assert got.steps[0].action == "read_email"
        assert got.steps[0].action_params == {}
    finally:
        storage.delete_trace(trace_id)


def test_save_trace_upsert_preserves_identity_and_replaces_steps():
    """Re-analysis of the same trace_id (BUG-01): old steps are replaced,
    not accumulated alongside the new ones."""
    trace_id = _unique_id("trace")
    try:
        storage.save_trace(Trace(trace_id=trace_id, agent_id="a", original_goal="g1", steps=[_make_step("read_email")]))
        storage.save_trace(
            Trace(trace_id=trace_id, agent_id="a", original_goal="g2", steps=[_make_step("delete_file")])
        )
        got = storage.get_trace(trace_id)
        assert got.original_goal == "g2"
        assert len(got.steps) == 1
        assert got.steps[0].action == "delete_file"
    finally:
        storage.delete_trace(trace_id)


def test_append_step_creates_trace_row_only_once():
    trace_id = _unique_id("session")
    try:
        storage.append_step(trace_id, "agent_A", "original goal", _make_step())
        second_step = _make_step()
        second_step = second_step.model_copy(update={"step_id": 2})
        storage.append_step(
            trace_id, "agent_A", "a DIFFERENT goal -- must be ignored, the row already exists", second_step
        )
        got = storage.get_trace(trace_id)
        assert got.original_goal == "original goal"
        assert len(got.steps) == 2  # both steps kept -- append_step never replaces earlier ones
    finally:
        storage.delete_trace(trace_id)


def test_save_detection_output_upserts_and_pending_approvals_joins_correctly():
    trace_id = _unique_id("trace")
    try:
        storage.append_step(trace_id, "agent_A", "goal", _make_step(action_params={"folder": "inbox"}))
        storage.save_detection_output(
            DetectionOutput(
                trace_id=trace_id,
                step_id=1,
                drift_score=0.5,
                risk_score=60,
                provenance_flag="external",
                classification="suspicious",
                explanation="e",
                decision="approval_required",
            )
        )
        pending = storage.get_pending_approvals()
        matching = [p for p in pending if p["trace_id"] == trace_id]
        assert len(matching) == 1
        assert matching[0]["action"] == "read_email"
        assert matching[0]["action_params"] == {"folder": "inbox"}

        # Overwrite (upsert) the same (trace_id, step_id) -- must replace, not duplicate.
        storage.save_detection_output(
            DetectionOutput(
                trace_id=trace_id,
                step_id=1,
                drift_score=0.9,
                risk_score=90,
                provenance_flag="tainted",
                classification="malicious",
                explanation="e2",
                decision="block",
            )
        )
        still_pending = [p for p in storage.get_pending_approvals() if p["trace_id"] == trace_id]
        assert still_pending == []  # now "block", not "approval_required" -- no longer pending
    finally:
        storage.delete_trace(trace_id)


def test_save_approval_decision_duplicate_raises_integrity_error():
    trace_id = _unique_id("trace")
    try:
        storage.append_step(trace_id, "a", "g", _make_step())
        storage.save_detection_output(
            DetectionOutput(
                trace_id=trace_id,
                step_id=1,
                drift_score=0.1,
                risk_score=50,
                provenance_flag="internal",
                classification="suspicious",
                explanation="e",
                decision="approval_required",
            )
        )
        storage.save_approval_decision(
            trace_id=trace_id,
            step_id=1,
            operator_decision="approved",
            operator_id="op1",
            final_status="approved_and_allowed",
            timestamp=datetime.now(UTC).isoformat(),
        )
        with pytest.raises(IntegrityError):
            storage.save_approval_decision(
                trace_id=trace_id,
                step_id=1,
                operator_decision="denied",
                operator_id="op2",
                final_status="denied_and_blocked",
                timestamp=datetime.now(UTC).isoformat(),
            )
        unchanged = storage.get_approval_decision(trace_id, 1)
        assert unchanged["operator_id"] == "op1"
    finally:
        storage.delete_trace(trace_id)


def test_list_sessions_summarizes_max_risk_and_worst_decision():
    trace_id = _unique_id("session")
    try:
        storage.append_step(trace_id, "agent_C", "goal C", _make_step(input_text="urgent request"))
        storage.save_detection_output(
            DetectionOutput(
                trace_id=trace_id,
                step_id=1,
                drift_score=0.2,
                risk_score=60,
                provenance_flag="internal",
                classification="suspicious",
                explanation="e",
                decision="approval_required",
            )
        )
        sessions = storage.list_sessions(limit=1000)
        matching = next(s for s in sessions if s["trace_id"] == trace_id)
        assert matching["agent_id"] == "agent_C"
        assert matching["step_count"] == 1
        assert matching["max_risk"] == 60
        assert matching["worst_decision"] == "approval_required"
    finally:
        storage.delete_trace(trace_id)


def test_create_operator_duplicate_username_raises_integrity_error():
    username = _unique_id("operator")
    storage.create_operator(username, "hash1")
    with pytest.raises(IntegrityError):
        storage.create_operator(username, "hash2")


def test_get_operator_by_username_roundtrip():
    username = _unique_id("operator")
    operator_id = storage.create_operator(username, "a-bcrypt-hash")
    looked_up = storage.get_operator_by_username(username)
    assert looked_up is not None
    assert looked_up["id"] == operator_id
    assert looked_up["password_hash"] == "a-bcrypt-hash"


def test_llm_cache_roundtrip_and_overwrite():
    cache_key = _unique_id("cache")
    storage.set_cached_llm_value(cache_key, "model-a", [1.0, 2.0, 3.0])
    assert storage.get_cached_llm_value(cache_key) == [1.0, 2.0, 3.0]
    storage.set_cached_llm_value(cache_key, "model-a", [4.0, 5.0])  # overwrite, not a duplicate error
    assert storage.get_cached_llm_value(cache_key) == [4.0, 5.0]


def test_get_trace_with_detections_for_unknown_trace_is_none():
    assert storage.get_trace_with_detections(_unique_id("nonexistent")) is None
