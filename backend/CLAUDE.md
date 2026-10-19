# CLAUDE.md

This file gives Claude Code context for working on this repository. Read this before making changes.

## Project Overview

**PromptGuard** is a hackathon project (48-hour build) that detects prompt injection attacks across multi-step AI agent workflows. It solves two specific gaps in current AI agent security:

1. **Intent Drift Detection** — tracks whether an agent's actions still align with its original stated goal across every step of a workflow, not just the first prompt.
2. **Cross-Agent Provenance Tracing** — tracks whether a piece of content is internally or externally sourced, and makes sure that tag survives even when content passes from one agent to another (so a compromised Agent A can't quietly poison a trusting Agent B).

This repo contains the **backend only**. A separate Streamlit frontend (built by a teammate) calls this backend's API. Do not build frontend code here unless explicitly asked.

## Tech Stack (locked — do not substitute without being told to)

- **Language**: Python 3.11+
- **API framework**: FastAPI
- **Orchestrating agent model**: `gemini-3.1-flash-lite` (Google Gemini API, function calling) — was originally spec'd as OpenAI's GPT-5.4 mini; migrated to Gemini on 2026-09-08 (see note below)
- **Judge/classifier model**: `gemini-3.1-flash-lite` (same model as the agent, not a separate one — was originally spec'd as GPT-5.4 nano)
- **Embeddings**: `gemini-embedding-2` (Google Gemini API) — was originally spec'd as OpenAI's text-embedding-3-small
- **Graph/provenance store**: NetworkX (in-memory), SQLite for persistence (`app/storage.py`, path in `config.DATABASE_PATH`)
- **Gemini integration**: raw `google-genai` Python SDK calls — do NOT introduce LangChain or other orchestration frameworks
- **Testing**: pytest

**On model names**: `AGENT_MODEL`/`JUDGE_MODEL`/`EMBEDDING_MODEL` in `config.py` were pinned by calling `client.models.list()` live against the real API and reading off what's actually available, not hardcoded from documentation or prior knowledge — Gemini's model lineup changes fast enough that a remembered name can be stale within the same day. Case in point: this project cycled through five different Flash models in one evening (`gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.7-flash`, `gemini-3.5-flash`, then `gemini-3.1-flash-lite`) because the free tier caps each one independently at 20 requests/day — landing on `gemini-3.1-flash-lite` wasn't a quality choice, it was the first one with quota headroom left. Re-run `client.models.list()` before assuming any of these three names still resolves to something usable.

## Repo Structure

```
/app
  main.py          # FastAPI app + routes
  schemas.py       # Pydantic models — the shared data contract (see below)
  agent.py         # Orchestrating agent (gemini-3.1-flash-lite + function calling)
  tools.py         # Mock tools: read_email, delete_file, send_message
  drift_engine.py  # Intent Drift Engine
  provenance.py    # Provenance Tracer (NetworkX graph)
  gate.py          # Secure Execution Gate (decision logic)
  guard.py         # /guard/* live guard API router (GUARD_MODE=mock skeleton; mounted in main.py)
  storage.py       # SQLite persistence for traces, detection outputs, approval decisions, and live guard sessions
  config.py        # Thresholds, model names, timing/timeout values, API key loading — ALL tunable values live here, nowhere else
generate_traces.py # Generates the synthetic test-data/traces/*.json fixtures used by run_validation.py
run_validation.py  # Validates /analyze-trace against test-data/traces and test-data/agentdojo_subset (see test-data/validation_results.md)
requirements.txt
.env               # GEMINI_API_KEY (never commit this file)
tests/
  test_drift_engine.py
  test_provenance.py
  test_gate.py
  test_main.py
  test_config.py
  test_guard.py
  test_known_bugs.py
```

## The Shared Data Contract — DO NOT CHANGE WITHOUT ASKING

Two teammates (frontend + test data) are building against these exact JSON shapes in parallel. Changing field names or types here breaks their work. If a change seems necessary, flag it instead of changing it silently.

### Trace input format
```json
{
  "trace_id": "trace_001",
  "agent_id": "agent_A",
  "original_goal": "Read and summarize today's emails",
  "steps": [
    {
      "step_id": 1,
      "actor": "agent_A",
      "input_text": "Please read my inbox and summarize anything urgent.",
      "input_source": "user",
      "input_provenance": "internal",
      "action": "read_email",
      "action_params": {"folder": "inbox"},
      "timestamp": "2026-09-08T10:00:00Z"
    }
  ]
}
```
`input_source` is one of: `user`, `tool_output`, `document`, `agent_handoff`.
`input_provenance` is one of: `internal`, `external`.

### Detection output format (what /analyze-trace returns, one per step)
```json
{
  "trace_id": "trace_001",
  "step_id": 2,
  "drift_score": 0.83,
  "risk_score": 87,
  "provenance_flag": "external",
  "classification": "malicious",
  "explanation": "Agent was asked to read email but is now attempting to delete files.",
  "decision": "block"
}
```
`provenance_flag` is one of: `internal`, `external`, `tainted` (tainted = looks internal but traces back to an external origin — this is the cross-agent case).
`classification` is one of: `clean`, `suspicious`, `malicious`.
`decision` is one of: `block`, `approval_required`, `allow_logged`.

### Trace-with-detections format (what `GET /trace/{trace_id}` returns)

`/analyze-trace` now persists the trace itself (`traces`/`trace_steps` tables), not just its detection outputs — this endpoint reads that back for a `trace_id` previously analyzed, wrapping the original Trace input format and Detection output format above together:
```json
{
  "trace": {
    "trace_id": "trace_001",
    "agent_id": "agent_A",
    "original_goal": "Read and summarize today's emails",
    "steps": [ /* ...same TraceStep shape as the Trace input format above... */ ]
  },
  "detections": [
    {
      "trace_id": "trace_001",
      "step_id": 2,
      "drift_score": 0.83,
      "risk_score": 87,
      "provenance_flag": "external",
      "classification": "malicious",
      "explanation": "Agent was asked to read email but is now attempting to delete files.",
      "decision": "block"
    }
  ]
}
```
404 (`{"detail": "..."}`) if `trace_id` was never analyzed via `/analyze-trace`.

### Approval decision format (added 2026-09-08 — request/response for `POST /approval-decision`)

Every `/analyze-trace` call now persists its detection outputs to SQLite (`config.DATABASE_PATH`), keyed by `(trace_id, step_id)`. `/approval-decision` looks up that stored record before accepting a decision — it is NOT derived from anything in the trace's own `input_text` or content (see "Things to Never Do" below); the decision comes from the request body only, which the frontend is responsible for populating from a real human action on a separate, authenticated channel.

Request:
```json
{
  "trace_id": "trace_16",
  "step_id": 2,
  "operator_decision": "approved",
  "operator_id": "reviewer_jane"
}
```
`operator_decision` is one of: `approved`, `denied`. `operator_id` is optional.

Response (200 — copied verbatim from a real run against `trace_16`, whose step 2 was genuinely flagged `approval_required` by `/analyze-trace` first):
```json
{
  "trace_id": "trace_16",
  "step_id": 2,
  "final_status": "approved_and_allowed",
  "timestamp": "2026-09-08T00:27:43.562854Z"
}
```
`final_status` is one of: `approved_and_allowed`, `denied_and_blocked`.

Rejections (standard FastAPI error shape, `{"detail": "..."}`), confirmed live:
- **404** — no `/analyze-trace` record exists at all for that `(trace_id, step_id)`.
- **409** — a record exists but its `decision` wasn't `approval_required` (e.g. it was already `allow_logged` or `block`) — nothing to approve or deny.

## Component Responsibilities

- **agent.py**: Runs a multi-step tool-calling loop using `gemini-3.1-flash-lite` (Gemini function calling). Takes `original_goal`, lets the model call tools from `tools.py`, records each step in the trace format above. Stops after the model signals completion or after 6 steps. Also records a `tool_output_received` step whenever `read_email` returns content, so the content itself (not just the fact that the tool was called) is captured for `drift_engine` to see. A declared tool called with bad arguments (missing/extra — a `TypeError`) is caught the same way as a hallucinated tool name: recorded as its own step and answered with an error `function_response`, instead of crashing `/run-agent` (fixed 2026-10-19, BUG-03). When a single model turn returns several function calls, all of their `function_response`s are now collected and sent back in ONE `user` `Content`, not one `Content` per call — Gemini expects a turn's function responses together and can reject or misbehave on the next request otherwise (fixed 2026-10-19, BUG-08).
- **drift_engine.py**: Embeds `original_goal` and each step's action description (or, for `tool_output_received` steps, the actual returned content — see `_text_to_embed_for_drift`) using `gemini-embedding-2`, computes cosine similarity, converts to `drift_score` (low similarity = high drift). Also calls `gemini-3.1-flash-lite` (`JUDGE_MODEL`) to classify whether the input looks like an injected instruction. Combines both into `risk_score` (0-100). `INJECTION_CLASSIFIER_PROMPT`'s fourth few-shot example (a benign request that mentions a sensitive-sounding topic like a security code, with no imperative/redirect structure) exists specifically to stop the judge from keying off vocabulary instead of structure — removing it reintroduces a confirmed false-positive bug (a legitimate "check my security code" request was scored 0.85 confidence of being an injected instruction before this example was added). Do not simplify this prompt back down to three examples. The actual text being judged (attacker-controllable — e.g. an email body) is wrapped in a boundary tag with a random suffix generated fresh per call (`secrets.token_hex`, in `_classify_injection`), not a fixed `"""` delimiter — a fixed delimiter let an attacker pre-write their own closing delimiter plus a fake `Score: 0.01` line into the text to escape the untrusted block and dictate their own score (fixed 2026-10-19, BUG-07). The prompt tells the judge everything inside the tag is data, never instructions, and any literal occurrence of that exact tag already inside the input text is stripped before wrapping so it can't prematurely close the block either.
- **provenance.py**: Builds a directed graph (NetworkX) of steps, tagging each with `internal`/`external`. When `actor` changes between steps (an agent handoff) and external content exists anywhere upstream, the receiving agent is tainted — and taint now flows forward through every later step that same actor takes (tracked per-actor in one forward pass over the trace), not just the first step right after the handoff, until a fresh, directly user-sourced input (`input_source == "user"`) for that actor resets it (fixed 2026-10-19, BUG-02). A handoff with no external content anywhere upstream still correctly stays `internal` — taint only flows from actual external content, not from the mere fact of a handoff. Exposes `get_provenance_flag(trace, step_id)`.
- **gate.py**: Pure decision logic. `decide(risk_score, provenance_flag)` — thresholds live in `config.py`, not hardcoded here. Starting thresholds: `risk_score > 80` → `block`; `40 <= risk_score <= 80` → `approval_required`; else `allow_logged`. If `provenance_flag == "tainted"`, floor the decision at `approval_required` regardless of score. Also exposes `classification_for(risk_score)` (moved here from `main.py` so `/guard/*` can share it without a circular import) — maps a risk score to `"malicious"`/`"suspicious"`/`"clean"` using the same thresholds as `decide`, so the label always agrees with the decision shown alongside it.
- **guard.py**: The `/guard/*` live guard API router (mounted in `main.py`), see the API section below for the full contract.
- **config.py**: The only place thresholds, model names, and other tunable constants should live. When tuning during testing, edit here.
- **storage.py**: SQLite persistence for traces, detection outputs, approval decisions, and (as of `/guard/*`) live sessions — a session is just a trace (`trace_id` == `session_id`) that grows one step at a time. `save_trace` (full replace, for `/analyze-trace` re-analysis) and `append_step` (adds one step without touching any existing steps/detections/approvals, for a live `/guard/*` session) are deliberately different functions with different atomicity guarantees — don't use one where the other is needed. `get_trace_with_detections` and `list_sessions` back `GET /trace/{trace_id}` + `GET /guard/session/{id}`, and `GET /guard/sessions`, respectively.

## API

- `POST /analyze-trace` — accepts a trace (see schema above), returns a list of detection outputs, one per step. Persists the trace itself (`traces`/`trace_steps` tables) as well as each detection output to SQLite as it computes it. Re-analysing a `trace_id` that was already analyzed atomically deletes its old steps, detection outputs, AND approval decisions first (one transaction, in `storage.save_trace`), so no stale step, detection, or approval from the previous run survives alongside — or gets silently inherited by — the new one (fixed 2026-10-19, BUG-01). If the drift engine's underlying Gemini call fails partway through analysis (e.g. a 429/503 from the model provider), responds 503 with a readable `detail` and deletes the trace entirely (`storage.delete_trace`) rather than leaving some steps scored and others missing (fixed 2026-10-19, BUG-04).
- `GET /trace/{trace_id}` — returns `{"trace": Trace, "detections": [DetectionOutput, ...]}` for a trace previously analyzed via `/analyze-trace`. 404 if that `trace_id` was never analyzed. Lets a client (e.g. the dashboard) look up one specific real trace by ID instead of resending a fixed payload.
- `GET /pending-approvals` — returns every detection currently flagged `approval_required` that has no recorded `operator_decision` yet (i.e. genuinely still pending — anything already approved or denied is excluded). Response is a list of `{trace_id, step_id, risk_score, provenance_flag, explanation, timestamp}` objects (`timestamp` is when `/analyze-trace` recorded that detection, not part of the trace/detection schema above). Lets the frontend poll for real work instead of using hardcoded test data.
- `POST /approval-decision` — accepts a human operator's approve/deny decision for a specific `(trace_id, step_id)` that was flagged `approval_required` by a prior `/analyze-trace` call (see schema above). Rejects with 404 if that step was never analyzed, 409 if it wasn't flagged `approval_required`, and 409 if it was already decided (states who decided it, what they decided, and when — fixed 2026-09-08; previously a second call silently overwrote the first). `storage.save_approval_decision` now does a plain `INSERT` (not `INSERT OR REPLACE`) against the `(trace_id, step_id)` primary key, so two near-simultaneous requests that both pass the "already decided?" pre-check can't both write: the loser gets a `sqlite3.IntegrityError`, which the endpoint turns into the same 409 instead of letting it silently overwrite the winner (fixed 2026-10-19, BUG-12).
- `POST /run-agent` — runs the mock agent loop for a given `original_goal` and returns the resulting `Trace`. If `trace_id` is omitted (or `null`), a fresh unique id is generated (`"run_" + 8 hex chars`, e.g. `"run_a1b2c3d4"`) instead of falling back to a fixed default — two runs without an explicit `trace_id` never collide on the same trace (fixed 2026-10-19, BUG-01).
- CORS is enabled for any `http://localhost:<port>` or `http://127.0.0.1:<port>` origin, so the Streamlit frontend can call this API from a different port.

### `/guard/*` — live guard API (added 2026-10-19, Prompt 1.3; owner: P1)

The frozen contract from `docs/plan/00_OVERVIEW.md` section A, as a working skeleton. A "session" is stored as an ordinary trace — `trace_id` IS `session_id` — that grows one step at a time via `storage.append_step` (never `storage.save_trace`, which would wipe earlier steps/detections/approvals; see `storage.py` above). `DetectionOutput` here is the exact same schema as `/analyze-trace`'s (see "The Shared Data Contract" above) — nothing new was added to it.

**Mode gate:** every `/guard/*` route calls `guard._require_mock_mode()` first, which reads `GUARD_MODE` fresh from the environment on every request (not cached at import time like the rest of `config.py`, so it behaves like a live feature flag):
- `GUARD_MODE=mock` → the deterministic scoring below, no LLM calls.
- Anything else (including unset) → `501 {"detail": "..."}`. The real implementation (`GUARD_MODE` unset or `"live"`, using the actual drift_engine + provenance + gate pipeline) lands Day 3 (Prompt 3.1) — this skeleton exists so P2 (the Protected Assistant) can integrate against the real contract today without waiting on that.

**Mock-mode scoring** (`guard._mock_score`), in priority order:
1. The step's `action` is `delete_file` or `send_message` AND any earlier step in this session had `input_provenance == "external"` → `risk_score=85`, `decision="block"`.
2. Else the step's `input_text` contains (case-insensitively) `"ignore previous"`, `"urgent"`, `"administrator"`, `"forward"`, or `"delete all"` → `risk_score=60`, `decision="approval_required"`.
3. Else → `risk_score=15`, `decision="allow_logged"`.

`drift_score` is always `0.0` in mock mode (no real drift signal is computed); `provenance_flag` and `classification` are still computed for real via `provenance.get_provenance_flag` and `gate.classification_for` (neither makes an LLM call, so there's no reason to mock them).

| Method & path | Body | Returns |
|---|---|---|
| `POST /guard/session` | `{agent_id, original_goal}` | `{session_id}` — a fresh `"session_" + 8 hex chars` id |
| `POST /guard/observe` | `{session_id, actor, input_text, input_source, input_provenance}` | `DetectionOutput` — content arriving (an email, a web page, a handoff); recorded with a fixed `action="observe"` |
| `POST /guard/check` | `{session_id, actor, input_text, input_source, input_provenance, action, action_params}` | `DetectionOutput` + `approval_status: "not_required" \| "pending"` (`"pending"` only when `decision == "approval_required"`). Call **before** running the tool. |
| `GET /guard/approval/{session_id}/{step_id}` | — | `{status: "pending" \| "approved" \| "denied", decided_by, decided_at}` — reads the same `approval_decisions` table `POST /approval-decision` writes to |
| `GET /guard/session/{session_id}` | — | `{trace, detections}`, identical shape to `GET /trace/{trace_id}` (both now backed by `storage.get_trace_with_detections`) |
| `GET /guard/sessions?limit=20` | — | `[{session_id, agent_id, original_goal, created_at, step_count, max_risk, worst_decision}]`, newest first. `max_risk`/`worst_decision` are `null` for a session with no `/guard/observe`/`/guard/check` calls yet. |

`/guard/observe` and `/guard/check` 404 if `session_id` was never created via `POST /guard/session`. `X-PromptGuard-Key` enforcement (per the contract table in `00_OVERVIEW.md`) is NOT implemented yet — that's Day 4 (Prompt 4.1, fixes BUG-06); every `/guard/*` route is unauthenticated for now.

## Coding Conventions

- Type hints on all function signatures.
- Pydantic models for all request/response shapes — no raw dicts crossing the API boundary.
- Keep `drift_engine.py`, `provenance.py`, and `gate.py` independently testable — no direct imports between them; `main.py` orchestrates the calls between components.
- All Gemini calls go through a small wrapper (put it in `config.py` or a `clients.py` if it grows) so the model name is set in one place, not scattered across files.
- Never hardcode the API key — always load from `.env` via `python-dotenv`. `config._resolve_gemini_api_key` treats an empty `GEMINI_API_KEY` env value the same as a missing one and falls back to `.env` in that case (an empty-but-exported shell var used to silently shadow a real key in `.env` — BUG-14), but never uses `load_dotenv(override=True)`, so a real platform-set env var (e.g. on Render) still always wins over a stray local `.env`. Fails fast with `RuntimeError` at import time if no non-empty key is found anywhere (fixed 2026-10-19).

## Build Priority Order (this is a hackathon — build in this order, not all at once)

1. Scaffold the FastAPI app, schemas, and empty function stubs for drift_engine/provenance/gate (correct signatures, no real logic yet) so other files can import them without errors.
2. Build `agent.py` with mock tools wired in.
3. Implement `drift_engine.py` for real.
4. Implement `provenance.py` for real, including the cross-agent handoff case.
5. Implement `gate.py` decision logic.
6. Run test traces through `/analyze-trace`, tune thresholds in `config.py` based on real results.
7. Make sure one clean cross-agent-handoff trace works end to end and correctly flags as `tainted` — this is a headline demo case, treat it as a priority, not an edge case.

## Testing

- `pytest tests/` should pass before considering any component "done."
- `test_drift_engine.py`: verify a clearly-aligned action gets a low drift score and a clearly-misaligned action gets a high one.
- `test_provenance.py`: verify an external tag survives an agent-to-agent handoff and is correctly reported as `tainted`.
- `test_gate.py`: verify the threshold boundaries produce the expected decision at each of the three tiers, and that `tainted` always floors at `approval_required`.

## Things to Never Do

- Never route a human-approval request back through the same content/data path that triggered the flag (this is a known "confused deputy" vulnerability — approval must go through a separate, authenticated channel, which is the frontend team's responsibility, not this repo's — but never build backend logic that would make that mistake possible either, e.g. don't auto-approve based on any field originating from `input_text`).
- Never change the shared JSON contract field names/types without flagging it first.
- Never introduce LangChain, additional orchestration frameworks, or a different vector DB — stack is locked for the hackathon.
- Never commit `.env` or any real API key.
- Never simplify `drift_engine.py`'s `INJECTION_CLASSIFIER_PROMPT` back down to three few-shot examples. The fourth (benign-topic-vs-malicious-structure) example is load-bearing — it fixes a confirmed judge false positive on sensitive-sounding-but-non-imperative text (e.g. "check my security code"), not decorative padding.
