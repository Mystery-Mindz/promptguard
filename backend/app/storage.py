import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from app import config
from app.schemas import DetectionOutput, Trace, TraceStep


@contextmanager
def _connect():
    """Opens a connection to the SQLite database at `config.DATABASE_PATH`
    for use in a `with` block, committing any changes automatically when
    the block exits normally, and always closing the connection afterward.

    Takes no parameters. Yields a `sqlite3.Connection` for the caller to
    run queries on.
    """
    conn = sqlite3.connect(config.DATABASE_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Creates every table PromptGuard needs (`detection_outputs`,
    `approval_decisions`, `traces`, `trace_steps`) if they don't already
    exist, and runs a small one-time migration to backfill the
    `created_at` column on `detection_outputs` for databases created before
    that column existed. Safe to call every time the app starts — existing
    tables and data are left alone.

    Takes no parameters and returns nothing.
    """
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
    """Saves (or overwrites) a full trace — its `agent_id`, `original_goal`,
    and every step's complete data — into the `traces` and `trace_steps`
    tables. This is separate from `save_detection_output`, which only
    stores the computed risk assessment; this function preserves the
    original trace content itself, which is what `GET /trace/{trace_id}`
    (via `get_trace`) reads back.

    Parameters:
    - `trace`: the trace to save.

    Returns nothing. If a trace with the same `trace_id` already exists,
    it (and its steps) are replaced with this version.
    """
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
    """Looks up a previously-saved trace by ID and rebuilds it from the
    database into a `Trace` object.

    Parameters:
    - `trace_id`: the trace to look up.

    Returns the reconstructed `Trace` (with all of its steps, in step_id
    order), or `None` if no trace with this ID was ever saved via
    `save_trace` (e.g. it was only ever run through the old code path that
    predates trace persistence, or the ID is simply wrong).
    """
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
    """Looks up every stored risk-detection result for a given trace.

    Parameters:
    - `trace_id`: the trace whose detection results to fetch.

    Returns a list of dicts (one per analyzed step, ordered by `step_id`),
    each shaped like a `DetectionOutput` — used by `GET /trace/{trace_id}`
    alongside `get_trace` to show both the original trace and what
    PromptGuard concluded about each step.
    """
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT trace_id, step_id, drift_score, risk_score, provenance_flag, classification, explanation, decision "
            "FROM detection_outputs WHERE trace_id = ? ORDER BY step_id ASC",
            (trace_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_detection_output(output: DetectionOutput) -> None:
    """Saves (or overwrites) one step's computed risk-detection result —
    drift score, risk score, provenance flag, classification, explanation,
    and decision — into the `detection_outputs` table, stamped with the
    current time.

    Parameters:
    - `output`: the detection result to save (as produced by
      `main.analyze_trace` from `drift_engine`, `provenance`, and `gate`).

    Returns nothing. If a result already exists for this
    `(trace_id, step_id)`, it is replaced.
    """
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
    """Finds every step that's currently waiting on a human operator's
    approve/deny decision, for the Operator Approval Console to display.

    Takes no parameters. Returns a list of dicts (one per pending step,
    oldest first), each with `trace_id`, `step_id`, `risk_score`,
    `provenance_flag`, `explanation`, and `created_at` — every step whose
    stored `decision` is `"approval_required"` and that has no matching row
    in `approval_decisions` yet (i.e. genuinely still undecided; anything
    already approved or denied is excluded).
    """
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
    """Looks up what PromptGuard's own detection pipeline decided for one
    step (before any human review), used by `POST /approval-decision` to
    check that a step was actually flagged before accepting a decision on
    it.

    Parameters:
    - `trace_id`, `step_id`: identify the step to look up.

    Returns the stored `decision` string (`"block"`, `"approval_required"`,
    or `"allow_logged"`), or `None` if this step was never analyzed at all.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT decision FROM detection_outputs WHERE trace_id = ? AND step_id = ?",
            (trace_id, step_id),
        ).fetchone()
    return row[0] if row else None


def get_approval_decision(trace_id: str, step_id: int) -> dict | None:
    """Looks up whether a human operator has already made a decision on a
    step, used by `POST /approval-decision` to prevent a second, conflicting
    decision from silently overwriting the first.

    Parameters:
    - `trace_id`, `step_id`: identify the step to look up.

    Returns a dict with `operator_decision`, `operator_id`, `final_status`,
    and `timestamp`, or `None` if no operator has decided on this step yet.
    """
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
    """Records a human operator's approve/deny decision for one step, into
    the `approval_decisions` table.

    Parameters:
    - `trace_id`, `step_id`: identify the step being decided on.
    - `operator_decision`: `"approved"` or `"denied"`.
    - `operator_id`: who made the decision (optional — may be `None`).
    - `final_status`: the resulting outcome, e.g. `"approved_and_allowed"`
      or `"denied_and_blocked"`.
    - `timestamp`: when the decision was made (ISO-8601 string).

    Returns nothing. If a decision already exists for this
    `(trace_id, step_id)`, it is replaced — callers are expected to check
    `get_approval_decision` first if a decision should be immutable (as
    `POST /approval-decision` does).
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO approval_decisions
                (trace_id, step_id, operator_decision, operator_id, final_status, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (trace_id, step_id, operator_decision, operator_id, final_status, timestamp),
        )
