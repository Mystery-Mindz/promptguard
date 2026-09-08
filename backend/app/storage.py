import sqlite3
from contextlib import contextmanager

from app import config
from app.schemas import DetectionOutput


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
                PRIMARY KEY (trace_id, step_id)
            )
            """
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


def save_detection_output(output: DetectionOutput) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO detection_outputs
                (trace_id, step_id, drift_score, risk_score, provenance_flag, classification, explanation, decision)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )


def get_detection_decision(trace_id: str, step_id: int) -> str | None:
    """Returns the stored `decision` for (trace_id, step_id), or None if no detection was ever recorded for it."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT decision FROM detection_outputs WHERE trace_id = ? AND step_id = ?",
            (trace_id, step_id),
        ).fetchone()
    return row[0] if row else None


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
