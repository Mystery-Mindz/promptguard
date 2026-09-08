import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from app import config
from app.schemas import DetectionOutput, Trace, TraceStep


@contextmanager
def _connect():
    conn = sqlite3.connect(config.DATABASE_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS detection_outputs (
                trace_id TEXT NOT NULL,
                step_id INTEGER NOT NULL,
                drift_score REAL NOT NULL,
                risk_score INTEGER NOT NULL,
                provenance_flag TEXT NOT NULL,
                classification TEXT NOT NULL,
                explanation TEXT NOT NULL,
                decision TEXT NOT NULL,
                created_at TEXT,
                PRIMARY KEY (trace_id, step_id)
            )
            """
        )
        # Migration for DBs created before created_at existed — add the
        # column and backfill any existing rows so /pending-approvals can
        # rely on it always being present, without discarding real records.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(detection_outputs)").fetchall()}
        if "created_at" not in columns:
            conn.execute("ALTER TABLE detection_outputs ADD COLUMN created_at TEXT")
        conn.execute(
            "UPDATE detection_outputs SET created_at = ? WHERE created_at IS NULL",
            (datetime.now(timezone.utc).isoformat(),),
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS approval_decisions (
                trace_id TEXT NOT NULL,
                step_id INTEGER NOT NULL,
                operator_decision TEXT NOT NULL,
                operator_id TEXT,
                final_status TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                PRIMARY KEY (trace_id, step_id)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS traces (
                trace_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                original_goal TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trace_steps (
                trace_id TEXT NOT NULL,
                step_id INTEGER NOT NULL,
                actor TEXT NOT NULL,
                input_text TEXT NOT NULL,
                input_source TEXT NOT NULL,
                input_provenance TEXT NOT NULL,
                action TEXT NOT NULL,
                action_params TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                PRIMARY KEY (trace_id, step_id)
            )
            """
        )


def save_trace(trace: Trace) -> None:
    """Persists the trace itself (agent_id, original_goal, and every step's full
    data) — separate from detection_outputs, which only stores the computed
    risk assessment. Needed so /trace/{trace_id} can return the original
    trace content, not just what drift_engine/gate concluded about it."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO traces (trace_id, agent_id, original_goal) VALUES (?, ?, ?)",
            (trace.trace_id, trace.agent_id, trace.original_goal),
        )
        for step in trace.steps:
            conn.execute(
                """
                INSERT OR REPLACE INTO trace_steps
                    (trace_id, step_id, actor, input_text, input_source, input_provenance, action, action_params, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace.trace_id,
                    step.step_id,
                    step.actor,
                    step.input_text,
                    step.input_source,
                    step.input_provenance,
                    step.action,
                    json.dumps(step.action_params),
                    step.timestamp.isoformat(),
                ),
            )


def get_trace(trace_id: str) -> Trace | None:
    """Returns the stored Trace for trace_id (reconstructed from traces +
    trace_steps), or None if this trace_id was never analyzed."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        trace_row = conn.execute(
            "SELECT trace_id, agent_id, original_goal FROM traces WHERE trace_id = ?",
            (trace_id,),
        ).fetchone()
        if trace_row is None:
            return None
        step_rows = conn.execute(
            "SELECT step_id, actor, input_text, input_source, input_provenance, action, action_params, timestamp "
            "FROM trace_steps WHERE trace_id = ? ORDER BY step_id ASC",
            (trace_id,),
        ).fetchall()

    steps = [
        TraceStep(
            step_id=row["step_id"],
            actor=row["actor"],
            input_text=row["input_text"],
            input_source=row["input_source"],
            input_provenance=row["input_provenance"],
            action=row["action"],
            action_params=json.loads(row["action_params"]),
            timestamp=row["timestamp"],
        )
        for row in step_rows
    ]
    return Trace(
        trace_id=trace_row["trace_id"],
        agent_id=trace_row["agent_id"],
        original_goal=trace_row["original_goal"],
        steps=steps,
    )


def get_detection_outputs_for_trace(trace_id: str) -> list[dict]:
    """Returns every detection_outputs row for trace_id, ordered by step_id."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT trace_id, step_id, drift_score, risk_score, provenance_flag, classification, explanation, decision "
            "FROM detection_outputs WHERE trace_id = ? ORDER BY step_id ASC",
            (trace_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_detection_output(output: DetectionOutput) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO detection_outputs
                (trace_id, step_id, drift_score, risk_score, provenance_flag, classification, explanation, decision, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                output.trace_id,
                output.step_id,
                output.drift_score,
                output.risk_score,
                output.provenance_flag,
                output.classification,
                output.explanation,
                output.decision,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def get_pending_approvals() -> list[dict]:
    """Returns every detection currently flagged approval_required that has no
    recorded operator_decision yet — i.e. genuinely still pending."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT d.trace_id, d.step_id, d.risk_score, d.provenance_flag, d.explanation, d.created_at
            FROM detection_outputs d
            LEFT JOIN approval_decisions a
                ON d.trace_id = a.trace_id AND d.step_id = a.step_id
            WHERE d.decision = 'approval_required'
                AND a.trace_id IS NULL
            ORDER BY d.created_at ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_detection_decision(trace_id: str, step_id: int) -> str | None:
    """Returns the stored `decision` for (trace_id, step_id), or None if no detection was ever recorded for it."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT decision FROM detection_outputs WHERE trace_id = ? AND step_id = ?",
            (trace_id, step_id),
        ).fetchone()
    return row[0] if row else None


def get_approval_decision(trace_id: str, step_id: int) -> dict | None:
    """Returns the existing recorded decision for (trace_id, step_id), or None if
    no operator has decided on it yet."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT operator_decision, operator_id, final_status, timestamp "
            "FROM approval_decisions WHERE trace_id = ? AND step_id = ?",
            (trace_id, step_id),
        ).fetchone()
    return dict(row) if row else None


def save_approval_decision(
    trace_id: str,
    step_id: int,
    operator_decision: str,
    operator_id: str | None,
    final_status: str,
    timestamp: str,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO approval_decisions
                (trace_id, step_id, operator_decision, operator_id, final_status, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (trace_id, step_id, operator_decision, operator_id, final_status, timestamp),
        )
