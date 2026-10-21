import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from app import config
from app.schemas import DetectionOutput, Trace, TraceStep, TraceWithDetections


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
            (datetime.now(UTC).isoformat(),),
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
                original_goal TEXT NOT NULL,
                created_at TEXT
            )
            """
        )
        # Migration for DBs created before created_at existed on `traces`
        # (needed for GET /guard/sessions to order/display sessions by
        # creation time) — same backfill pattern as detection_outputs above.
        trace_columns = {row[1] for row in conn.execute("PRAGMA table_info(traces)").fetchall()}
        if "created_at" not in trace_columns:
            conn.execute("ALTER TABLE traces ADD COLUMN created_at TEXT")
        conn.execute(
            "UPDATE traces SET created_at = ? WHERE created_at IS NULL",
            (datetime.now(UTC).isoformat(),),
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

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_cache (
                cache_key TEXT PRIMARY KEY,
                model TEXT NOT NULL,
                value TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )


def _delete_trace_rows(conn: sqlite3.Connection, trace_id: str) -> None:
    """Deletes every row derived from a trace_id across `trace_steps`,
    `detection_outputs`, and `approval_decisions` (but not the `traces` row
    itself), using the caller's already-open connection so the deletes
    share the caller's transaction.

    Parameters:
    - `conn`: an open connection (inside a `with _connect() as conn:` block).
    - `trace_id`: the trace whose derived rows should be deleted.

    Returns nothing.
    """
    conn.execute("DELETE FROM trace_steps WHERE trace_id = ?", (trace_id,))
    conn.execute("DELETE FROM detection_outputs WHERE trace_id = ?", (trace_id,))
    conn.execute("DELETE FROM approval_decisions WHERE trace_id = ?", (trace_id,))


def _insert_step_row(conn: sqlite3.Connection, trace_id: str, step: TraceStep) -> None:
    """Inserts one step's row into `trace_steps`, using the caller's
    already-open connection so it shares the caller's transaction. Shared
    by `save_trace` (which deletes all of a trace_id's old steps first)
    and `append_step` (which adds one step to a trace that already has
    others), so this SQL only lives in one place.

    Parameters:
    - `conn`: an open connection.
    - `trace_id`: which trace this step belongs to.
    - `step`: the step to insert.

    Returns nothing.
    """
    conn.execute(
        """
        INSERT INTO trace_steps
            (trace_id, step_id, actor, input_text, input_source, input_provenance, action, action_params, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trace_id,
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


def save_trace(trace: Trace) -> None:
    """Saves a full trace — its `agent_id`, `original_goal`, and every
    step's complete data — into the `traces` and `trace_steps` tables.
    This is separate from `save_detection_output`, which only stores the
    computed risk assessment; this function preserves the original trace
    content itself, which is what `GET /trace/{trace_id}` (via
    `get_trace`) reads back.

    Parameters:
    - `trace`: the trace to save.

    Returns nothing. If a trace with the same `trace_id` already exists
    (re-analysis of a previously-seen trace_id), its old steps, detection
    outputs, AND approval decisions are all deleted first, in the same
    transaction as writing the new steps — so a stale step from the old
    run can never survive alongside a step from the new one, and a new,
    unrelated step can never silently inherit an old step's approval
    decision (both were confirmed bugs: BUG-01). The trace's original
    `created_at` is preserved across re-analysis (only set the first time
    a given `trace_id` is seen).
    """
    with _connect() as conn:
        _delete_trace_rows(conn, trace.trace_id)

        conn.execute(
            """
            INSERT INTO traces (trace_id, agent_id, original_goal, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(trace_id) DO UPDATE SET
                agent_id = excluded.agent_id,
                original_goal = excluded.original_goal
            """,
            (trace.trace_id, trace.agent_id, trace.original_goal, datetime.now(UTC).isoformat()),
        )
        for step in trace.steps:
            _insert_step_row(conn, trace.trace_id, step)


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


def get_trace_with_detections(trace_id: str) -> TraceWithDetections | None:
    """Looks up a trace together with every stored detection result for
    it — the shape both `GET /trace/{trace_id}` and `GET
    /guard/session/{session_id}` return, factored out here so that logic
    only lives in one place.

    Parameters:
    - `trace_id`: the trace (or guard session, same tables — `trace_id`
      doubles as `session_id`) to look up.

    Returns a `TraceWithDetections`, or `None` if `trace_id` was never
    saved via `save_trace` or `append_step`.
    """
    trace = get_trace(trace_id)
    if trace is None:
        return None
    detections = [DetectionOutput(**row) for row in get_detection_outputs_for_trace(trace_id)]
    return TraceWithDetections(trace=trace, detections=detections)


def append_step(trace_id: str, agent_id: str, original_goal: str, step: TraceStep) -> None:
    """Appends one step to a trace, creating its `traces` row first if it
    doesn't exist yet (e.g. this is the very first step of a new `/guard/*`
    session). Unlike `save_trace`, this does NOT delete any of the trace's
    existing steps, detection outputs, or approval decisions — it's for
    incrementally growing a *live* session across multiple
    `/guard/observe` and `/guard/check` calls, where every earlier step's
    detection and approval must survive every later one (`save_trace`'s
    full-replace semantics are built for `/analyze-trace` re-analysis,
    where that's exactly what should happen instead — see BUG-01).

    Parameters:
    - `trace_id`: the session this step belongs to (`trace_id` ==
      `session_id` for `/guard/*`).
    - `agent_id`, `original_goal`: only written if the `traces` row for
      `trace_id` doesn't exist yet; an existing row's values (and its
      original `created_at`) are left untouched.
    - `step`: the new step to append.

    Returns nothing.
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO traces (trace_id, agent_id, original_goal, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(trace_id) DO NOTHING
            """,
            (trace_id, agent_id, original_goal, datetime.now(UTC).isoformat()),
        )
        _insert_step_row(conn, trace_id, step)


def list_sessions(limit: int = 20) -> list[dict]:
    """Lists guard sessions (traces) newest-first, each summarized for
    `GET /guard/sessions`.

    Parameters:
    - `limit`: maximum number of sessions to return.

    Returns a list of dicts, each with `trace_id`, `agent_id`,
    `original_goal`, `created_at`, `step_count` (from `trace_steps`),
    `max_risk` (highest `risk_score` across this session's detections, or
    `None` if nothing has been scored yet), and `worst_decision` (the most
    severe `decision` recorded — `block` > `approval_required` >
    `allow_logged` — or `None` if nothing has been scored yet).
    """
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                t.trace_id,
                t.agent_id,
                t.original_goal,
                t.created_at,
                (SELECT COUNT(*) FROM trace_steps ts WHERE ts.trace_id = t.trace_id) AS step_count,
                (SELECT MAX(d.risk_score) FROM detection_outputs d WHERE d.trace_id = t.trace_id) AS max_risk,
                (
                    SELECT d2.decision FROM detection_outputs d2 WHERE d2.trace_id = t.trace_id
                    ORDER BY CASE d2.decision
                        WHEN 'block' THEN 3
                        WHEN 'approval_required' THEN 2
                        WHEN 'allow_logged' THEN 1
                        ELSE 0
                    END DESC
                    LIMIT 1
                ) AS worst_decision
            FROM traces t
            ORDER BY t.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_trace(trace_id: str) -> None:
    """Deletes a trace and everything derived from it — its steps,
    detection outputs, and approval decisions — in one transaction. Used
    by `POST /analyze-trace` when a model/provider error interrupts
    analysis partway through, so a trace with some steps scored and others
    missing never persists (BUG-04); the caller should retry
    `/analyze-trace` from scratch once the underlying error clears.

    Parameters:
    - `trace_id`: the trace to delete entirely.

    Returns nothing. Safe to call even if `trace_id` was never saved.
    """
    with _connect() as conn:
        _delete_trace_rows(conn, trace_id)
        conn.execute("DELETE FROM traces WHERE trace_id = ?", (trace_id,))


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
                datetime.now(UTC).isoformat(),
            ),
        )


def get_pending_approvals() -> list[dict]:
    """Finds every step that's currently waiting on a human operator's
    approve/deny decision, for the Operator Approval Console to display.

    Takes no parameters. Returns a list of dicts (one per pending step,
    oldest first), each with `trace_id`, `step_id`, `risk_score`,
    `provenance_flag`, `explanation`, `created_at`, and (added for Prompt
    3.1, joined in from `trace_steps`/`detection_outputs`) `action`,
    `action_params` (parsed back into a dict), `input_text`, `drift_score`,
    `classification` — every step whose stored `decision` is
    `"approval_required"` and that has no matching row in
    `approval_decisions` yet (i.e. genuinely still undecided; anything
    already approved or denied is excluded). Joined against `trace_steps`
    on `(trace_id, step_id)` — both `/analyze-trace` and `/guard/*` always
    write a trace_steps row together with its detection_outputs row (see
    `save_trace`/`append_step` + `save_detection_output`), so every
    genuinely pending row has a matching step to join against.
    """
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                d.trace_id, d.step_id, d.risk_score, d.provenance_flag, d.explanation, d.created_at,
                d.drift_score, d.classification,
                s.action, s.action_params, s.input_text
            FROM detection_outputs d
            JOIN trace_steps s
                ON d.trace_id = s.trace_id AND d.step_id = s.step_id
            LEFT JOIN approval_decisions a
                ON d.trace_id = a.trace_id AND d.step_id = a.step_id
            WHERE d.decision = 'approval_required'
                AND a.trace_id IS NULL
            ORDER BY d.created_at ASC
            """
        ).fetchall()
    return [{**dict(row), "action_params": json.loads(row["action_params"])} for row in rows]


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

    Returns nothing. Raises `sqlite3.IntegrityError` if a decision already
    exists for this `(trace_id, step_id)` — a plain `INSERT` against the
    `(trace_id, step_id)` primary key, not `INSERT OR REPLACE`, so that two
    near-simultaneous calls for the same step can't both "pass" an earlier
    `get_approval_decision` check and have the second silently overwrite
    the first (BUG-12): only one commits, and the other gets a real
    constraint violation to react to. `POST /approval-decision` checks
    `get_approval_decision` first for the common case (a fast, clean 409
    without even trying to write), and catches this exception as the
    fallback for the race the pre-check can't fully close.
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO approval_decisions
                (trace_id, step_id, operator_decision, operator_id, final_status, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (trace_id, step_id, operator_decision, operator_id, final_status, timestamp),
        )


def get_cached_llm_value(cache_key: str) -> Any | None:
    """Looks up a previously-cached model result (a judge's raw text reply
    or an embedding vector) by `cache_key` (a `sha256(model + text)` hex
    digest — see `app.llm`). This is the DB-backed half of `app.llm`'s
    cache; the in-memory LRU in front of it is checked first by the
    caller and is not persisted here.

    Parameters:
    - `cache_key`: the cache key to look up.

    Returns the cached value (JSON-decoded back into its original shape —
    a `str` for a judge reply, a `list[float]` for an embedding), or
    `None` if nothing is cached under this key yet.
    """
    with _connect() as conn:
        row = conn.execute("SELECT value FROM llm_cache WHERE cache_key = ?", (cache_key,)).fetchone()
    return json.loads(row[0]) if row else None


def set_cached_llm_value(cache_key: str, model: str, value: Any) -> None:
    """Stores (or overwrites) one model result in the `llm_cache` table,
    keyed by `cache_key` (see `get_cached_llm_value`).

    Parameters:
    - `cache_key`: the cache key to store under.
    - `model`: the model that actually produced `value` — recorded
      alongside the key for debugging/inspection, not used for lookup
      (the key itself already encodes the model, see `app.llm._cache_key`).
    - `value`: the result to cache (JSON-encoded — must be JSON-serializable,
      e.g. a `str` or a `list[float]`).

    Returns nothing.
    """
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO llm_cache (cache_key, model, value, created_at) VALUES (?, ?, ?, ?)",
            (cache_key, model, json.dumps(value), datetime.now(UTC).isoformat()),
        )
