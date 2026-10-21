"""Persistence for PromptGuard, via SQLAlchemy Core (no ORM) so one code
path serves both backends (Prompt 4.2): SQLite locally and in tests,
Postgres in production (Render's free disk is wiped on every redeploy —
Neon's Postgres isn't). Chosen by `config.DATABASE_URL`
(`sqlite:///path/to/file.db` or `postgresql://user:pass@host/db`); every
public function below has the exact same signature and behavior
regardless of which one is active.

Every table lives in `metadata` below: `traces`/`trace_steps` (a live
`/guard/*` session IS a trace, `trace_id` == `session_id` — there's no
separate "sessions" table), `detection_outputs`, `approval_decisions`,
`operators`, and `llm_cache`.
"""

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Column,
    Engine,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    create_engine,
    delete,
    insert,
    select,
    text,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Connection

from app import config
from app.schemas import DetectionOutput, Trace, TraceStep, TraceWithDetections

metadata = MetaData()

traces_table = Table(
    "traces",
    metadata,
    Column("trace_id", String, primary_key=True),
    Column("agent_id", String, nullable=False),
    Column("original_goal", Text, nullable=False),
    Column("created_at", String),
)

trace_steps_table = Table(
    "trace_steps",
    metadata,
    Column("trace_id", String, primary_key=True),
    Column("step_id", Integer, primary_key=True),
    Column("actor", String, nullable=False),
    Column("input_text", Text, nullable=False),
    Column("input_source", String, nullable=False),
    Column("input_provenance", String, nullable=False),
    Column("action", String, nullable=False),
    Column("action_params", Text, nullable=False),  # JSON-encoded
    Column("timestamp", String, nullable=False),
)

detection_outputs_table = Table(
    "detection_outputs",
    metadata,
    Column("trace_id", String, primary_key=True),
    Column("step_id", Integer, primary_key=True),
    Column("drift_score", Float, nullable=False),
    Column("risk_score", Integer, nullable=False),
    Column("provenance_flag", String, nullable=False),
    Column("classification", String, nullable=False),
    Column("explanation", Text, nullable=False),
    Column("decision", String, nullable=False),
    Column("created_at", String),
)

approval_decisions_table = Table(
    "approval_decisions",
    metadata,
    Column("trace_id", String, primary_key=True),
    Column("step_id", Integer, primary_key=True),
    Column("operator_decision", String, nullable=False),
    Column("operator_id", String),
    Column("final_status", String, nullable=False),
    Column("timestamp", String, nullable=False),
)

llm_cache_table = Table(
    "llm_cache",
    metadata,
    Column("cache_key", String, primary_key=True),
    Column("model", String, nullable=False),
    Column("value", Text, nullable=False),
    Column("created_at", String, nullable=False),
)

operators_table = Table(
    "operators",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("username", String, nullable=False, unique=True),
    Column("password_hash", Text, nullable=False),
    Column("created_at", String, nullable=False),
)


def _normalize_database_url(url: str) -> str:
    """`config.DATABASE_URL` is documented (and typically copy-pasted from
    Render/Neon) as a plain `postgresql://...` URL — but SQLAlchemy's bare
    `postgresql://` dialect defaults to the (unmaintained-for-new-projects)
    psycopg2 driver. Rewrites that one scheme to `postgresql+psycopg://`
    so the modern psycopg (3.x) driver is used instead, without requiring
    every deployment's `DATABASE_URL` to know that detail. Every other
    scheme (`sqlite://...`, or an already-explicit `postgresql+<driver>://`)
    passes through unchanged.
    """
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


# Cached per URL (not a single module-level global) so a test that
# monkeypatches config.DATABASE_URL to a fresh temp-file URL transparently
# gets its own engine, while production (DATABASE_URL never changes at
# runtime) gets exactly one persistent, connection-pooled Engine reused
# across every call -- the same trade-off a hardcoded module-level engine
# would make, minus the test-isolation problem that would cause.
_engines: dict[str, Engine] = {}


def _get_engine() -> Engine:
    url = config.DATABASE_URL
    engine = _engines.get(url)
    if engine is None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        engine = create_engine(_normalize_database_url(url), connect_args=connect_args, future=True)
        _engines[url] = engine
    return engine


def _upsert(table: Table):
    """Returns the dialect-appropriate `INSERT ... ON CONFLICT` construct
    for `table` -- SQLite and Postgres both implement `ON CONFLICT`
    (unlike, say, MySQL's `ON DUPLICATE KEY UPDATE`), so both dialects'
    `insert()` expose the same `on_conflict_do_update`/`on_conflict_do_nothing`
    methods; only the import differs.
    """
    if _get_engine().dialect.name == "postgresql":
        return postgresql.insert(table)
    return sqlite.insert(table)


def init_db() -> None:
    """Creates every table PromptGuard needs if it doesn't already exist
    (`metadata.create_all(..., checkfirst=True)` — the SQLAlchemy
    equivalent of `CREATE TABLE IF NOT EXISTS`, for both SQLite and
    Postgres). Safe to call every time the app starts — existing tables
    and data are left alone; this never ALTERs an existing table.

    Takes no parameters and returns nothing.
    """
    metadata.create_all(_get_engine(), checkfirst=True)


def _delete_trace_rows(conn: Connection, trace_id: str) -> None:
    """Deletes every row derived from a trace_id across `trace_steps`,
    `detection_outputs`, and `approval_decisions` (but not the `traces` row
    itself), using the caller's already-open connection so the deletes
    share the caller's transaction.

    Parameters:
    - `conn`: an open connection (inside a `with engine.begin() as conn:` block).
    - `trace_id`: the trace whose derived rows should be deleted.

    Returns nothing.
    """
    conn.execute(delete(trace_steps_table).where(trace_steps_table.c.trace_id == trace_id))
    conn.execute(delete(detection_outputs_table).where(detection_outputs_table.c.trace_id == trace_id))
    conn.execute(delete(approval_decisions_table).where(approval_decisions_table.c.trace_id == trace_id))


def _insert_step_row(conn: Connection, trace_id: str, step: TraceStep) -> None:
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
        insert(trace_steps_table).values(
            trace_id=trace_id,
            step_id=step.step_id,
            actor=step.actor,
            input_text=step.input_text,
            input_source=step.input_source,
            input_provenance=step.input_provenance,
            action=step.action,
            action_params=json.dumps(step.action_params),
            timestamp=step.timestamp.isoformat(),
        )
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
    with _get_engine().begin() as conn:
        _delete_trace_rows(conn, trace.trace_id)

        upsert_stmt = _upsert(traces_table).values(
            trace_id=trace.trace_id,
            agent_id=trace.agent_id,
            original_goal=trace.original_goal,
            created_at=datetime.now(UTC).isoformat(),
        )
        conn.execute(
            upsert_stmt.on_conflict_do_update(
                index_elements=["trace_id"],
                set_={
                    "agent_id": upsert_stmt.excluded.agent_id,
                    "original_goal": upsert_stmt.excluded.original_goal,
                },
            )
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
    with _get_engine().connect() as conn:
        trace_row = conn.execute(
            select(traces_table.c.trace_id, traces_table.c.agent_id, traces_table.c.original_goal).where(
                traces_table.c.trace_id == trace_id
            )
        ).mappings().fetchone()
        if trace_row is None:
            return None
        step_rows = (
            conn.execute(
                select(
                    trace_steps_table.c.step_id,
                    trace_steps_table.c.actor,
                    trace_steps_table.c.input_text,
                    trace_steps_table.c.input_source,
                    trace_steps_table.c.input_provenance,
                    trace_steps_table.c.action,
                    trace_steps_table.c.action_params,
                    trace_steps_table.c.timestamp,
                )
                .where(trace_steps_table.c.trace_id == trace_id)
                .order_by(trace_steps_table.c.step_id.asc())
            )
            .mappings()
            .fetchall()
        )

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
    with _get_engine().begin() as conn:
        conn.execute(
            _upsert(traces_table)
            .values(
                trace_id=trace_id,
                agent_id=agent_id,
                original_goal=original_goal,
                created_at=datetime.now(UTC).isoformat(),
            )
            .on_conflict_do_nothing(index_elements=["trace_id"])
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

    Expressed as one `text()` query (standard SQL — correlated subqueries
    and a `CASE` expression, nothing dialect-specific) rather than
    SQLAlchemy Core's expression language, since translating it 1:1 would
    be far more verbose for no behavioral difference; SQLAlchemy still
    handles the `:limit` bind parameter per-dialect.
    """
    query = text(
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
        LIMIT :limit
        """
    )
    with _get_engine().connect() as conn:
        rows = conn.execute(query, {"limit": limit}).mappings().fetchall()
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
    with _get_engine().begin() as conn:
        _delete_trace_rows(conn, trace_id)
        conn.execute(delete(traces_table).where(traces_table.c.trace_id == trace_id))


def get_detection_outputs_for_trace(trace_id: str) -> list[dict]:
    """Looks up every stored risk-detection result for a given trace.

    Parameters:
    - `trace_id`: the trace whose detection results to fetch.

    Returns a list of dicts (one per analyzed step, ordered by `step_id`),
    each shaped like a `DetectionOutput` — used by `GET /trace/{trace_id}`
    alongside `get_trace` to show both the original trace and what
    PromptGuard concluded about each step.
    """
    with _get_engine().connect() as conn:
        rows = (
            conn.execute(
                select(
                    detection_outputs_table.c.trace_id,
                    detection_outputs_table.c.step_id,
                    detection_outputs_table.c.drift_score,
                    detection_outputs_table.c.risk_score,
                    detection_outputs_table.c.provenance_flag,
                    detection_outputs_table.c.classification,
                    detection_outputs_table.c.explanation,
                    detection_outputs_table.c.decision,
                )
                .where(detection_outputs_table.c.trace_id == trace_id)
                .order_by(detection_outputs_table.c.step_id.asc())
            )
            .mappings()
            .fetchall()
        )
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
    upsert_stmt = _upsert(detection_outputs_table).values(
        trace_id=output.trace_id,
        step_id=output.step_id,
        drift_score=output.drift_score,
        risk_score=output.risk_score,
        provenance_flag=output.provenance_flag,
        classification=output.classification,
        explanation=output.explanation,
        decision=output.decision,
        created_at=datetime.now(UTC).isoformat(),
    )
    with _get_engine().begin() as conn:
        conn.execute(
            upsert_stmt.on_conflict_do_update(
                index_elements=["trace_id", "step_id"],
                set_={
                    "drift_score": upsert_stmt.excluded.drift_score,
                    "risk_score": upsert_stmt.excluded.risk_score,
                    "provenance_flag": upsert_stmt.excluded.provenance_flag,
                    "classification": upsert_stmt.excluded.classification,
                    "explanation": upsert_stmt.excluded.explanation,
                    "decision": upsert_stmt.excluded.decision,
                    "created_at": upsert_stmt.excluded.created_at,
                },
            )
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
    d = detection_outputs_table
    s = trace_steps_table
    a = approval_decisions_table
    query = (
        select(
            d.c.trace_id,
            d.c.step_id,
            d.c.risk_score,
            d.c.provenance_flag,
            d.c.explanation,
            d.c.created_at,
            d.c.drift_score,
            d.c.classification,
            s.c.action,
            s.c.action_params,
            s.c.input_text,
        )
        .join(s, and_(d.c.trace_id == s.c.trace_id, d.c.step_id == s.c.step_id))
        .outerjoin(a, and_(d.c.trace_id == a.c.trace_id, d.c.step_id == a.c.step_id))
        .where(d.c.decision == "approval_required", a.c.trace_id.is_(None))
        .order_by(d.c.created_at.asc())
    )
    with _get_engine().connect() as conn:
        rows = conn.execute(query).mappings().fetchall()
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
    with _get_engine().connect() as conn:
        row = conn.execute(
            select(detection_outputs_table.c.decision).where(
                detection_outputs_table.c.trace_id == trace_id, detection_outputs_table.c.step_id == step_id
            )
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
    with _get_engine().connect() as conn:
        row = (
            conn.execute(
                select(
                    approval_decisions_table.c.operator_decision,
                    approval_decisions_table.c.operator_id,
                    approval_decisions_table.c.final_status,
                    approval_decisions_table.c.timestamp,
                ).where(
                    approval_decisions_table.c.trace_id == trace_id, approval_decisions_table.c.step_id == step_id
                )
            )
            .mappings()
            .fetchone()
        )
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

    Returns nothing. Raises `sqlalchemy.exc.IntegrityError` if a decision
    already exists for this `(trace_id, step_id)` — a plain `INSERT`
    against the `(trace_id, step_id)` primary key, not an upsert, so that
    two near-simultaneous calls for the same step can't both "pass" an
    earlier `get_approval_decision` check and have the second silently
    overwrite the first (BUG-12): only one commits, and the other gets a
    real constraint violation to react to — `sqlalchemy.exc.IntegrityError`
    uniformly wraps the underlying driver's own exception
    (`sqlite3.IntegrityError`, psycopg's `UniqueViolation`, etc.), so
    callers don't need to know which backend raised it. `POST
    /approval-decision` checks `get_approval_decision` first for the
    common case (a fast, clean 409 without even trying to write), and
    catches this exception as the fallback for the race the pre-check
    can't fully close.
    """
    with _get_engine().begin() as conn:
        conn.execute(
            insert(approval_decisions_table).values(
                trace_id=trace_id,
                step_id=step_id,
                operator_decision=operator_decision,
                operator_id=operator_id,
                final_status=final_status,
                timestamp=timestamp,
            )
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
    with _get_engine().connect() as conn:
        row = conn.execute(
            select(llm_cache_table.c.value).where(llm_cache_table.c.cache_key == cache_key)
        ).fetchone()
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
    upsert_stmt = _upsert(llm_cache_table).values(
        cache_key=cache_key,
        model=model,
        value=json.dumps(value),
        created_at=datetime.now(UTC).isoformat(),
    )
    with _get_engine().begin() as conn:
        conn.execute(
            upsert_stmt.on_conflict_do_update(
                index_elements=["cache_key"],
                set_={
                    "model": upsert_stmt.excluded.model,
                    "value": upsert_stmt.excluded.value,
                    "created_at": upsert_stmt.excluded.created_at,
                },
            )
        )


def create_operator(username: str, password_hash: str) -> int:
    """Inserts a new operator account into the `operators` table. See
    `app.auth.hash_password` for producing `password_hash` -- this
    function never hashes a plaintext password itself.

    Parameters:
    - `username`: the operator's login name. Must be unique.
    - `password_hash`: the bcrypt hash of their password.

    Returns the new operator's `id`. Raises `sqlalchemy.exc.IntegrityError`
    if `username` is already taken (`operators.username` is `UNIQUE`) --
    `python -m app.create_operator` turns that into a friendly message.
    """
    with _get_engine().begin() as conn:
        result = conn.execute(
            insert(operators_table).values(
                username=username, password_hash=password_hash, created_at=datetime.now(UTC).isoformat()
            )
        )
        inserted_primary_key = result.inserted_primary_key
    assert inserted_primary_key is not None
    operator_id = inserted_primary_key[0]
    assert operator_id is not None
    return operator_id


def get_operator_by_username(username: str) -> dict | None:
    """Looks up an operator account by username, for `POST /auth/login`
    to verify a password against.

    Parameters:
    - `username`: the login name to look up.

    Returns a dict with `id`, `username`, `password_hash`, `created_at`,
    or `None` if no operator with this username exists.
    """
    with _get_engine().connect() as conn:
        row = (
            conn.execute(
                select(
                    operators_table.c.id,
                    operators_table.c.username,
                    operators_table.c.password_hash,
                    operators_table.c.created_at,
                ).where(operators_table.c.username == username)
            )
            .mappings()
            .fetchone()
        )
    return dict(row) if row else None
