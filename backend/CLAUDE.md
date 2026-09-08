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
  storage.py       # SQLite persistence for detection outputs + approval decisions
  config.py        # Thresholds, model names, API key loading — ALL tunable values live here, nowhere else
requirements.txt
.env               # GEMINI_API_KEY (never commit this file)
tests/
  test_drift_engine.py
  test_provenance.py
  test_gate.py
  test_main.py
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

- **agent.py**: Runs a multi-step tool-calling loop using `gemini-3.1-flash-lite` (Gemini function calling). Takes `original_goal`, lets the model call tools from `tools.py`, records each step in the trace format above. Stops after the model signals completion or after 6 steps. Also records a `tool_output_received` step whenever `read_email` returns content, so the content itself (not just the fact that the tool was called) is captured for `drift_engine` to see.
- **drift_engine.py**: Embeds `original_goal` and each step's action description (or, for `tool_output_received` steps, the actual returned content — see `_text_to_embed_for_drift`) using `gemini-embedding-2`, computes cosine similarity, converts to `drift_score` (low similarity = high drift). Also calls `gemini-3.1-flash-lite` (`JUDGE_MODEL`) to classify whether the input looks like an injected instruction. Combines both into `risk_score` (0-100). `INJECTION_CLASSIFIER_PROMPT`'s fourth few-shot example (a benign request that mentions a sensitive-sounding topic like a security code, with no imperative/redirect structure) exists specifically to stop the judge from keying off vocabulary instead of structure — removing it reintroduces a confirmed false-positive bug (a legitimate "check my security code" request was scored 0.85 confidence of being an injected instruction before this example was added). Do not simplify this prompt back down to three examples.
- **provenance.py**: Builds a directed graph (NetworkX) of steps, tagging each with `internal`/`external`. When `actor` changes between steps (an agent handoff), the receiving agent's node inherits the `external` tag from any upstream external source, even if it superficially looks like a trusted handoff. Exposes `get_provenance_flag(trace, step_id)`.
- **gate.py**: Pure decision logic. `decide(risk_score, provenance_flag)` — thresholds live in `config.py`, not hardcoded here. Starting thresholds: `risk_score > 80` → `block`; `40 <= risk_score <= 80` → `approval_required`; else `allow_logged`. If `provenance_flag == "tainted"`, floor the decision at `approval_required` regardless of score.
- **config.py**: The only place thresholds, model names, and other tunable constants should live. When tuning during testing, edit here.

## API

- `POST /analyze-trace` — accepts a trace (see schema above), returns a list of detection outputs, one per step. Persists each output to SQLite as it computes it.
- `GET /pending-approvals` — returns every detection currently flagged `approval_required` that has no recorded `operator_decision` yet (i.e. genuinely still pending — anything already approved or denied is excluded). Response is a list of `{trace_id, step_id, risk_score, provenance_flag, explanation, timestamp}` objects (`timestamp` is when `/analyze-trace` recorded that detection, not part of the trace/detection schema above). Lets the frontend poll for real work instead of using hardcoded test data.
- `POST /approval-decision` — accepts a human operator's approve/deny decision for a specific `(trace_id, step_id)` that was flagged `approval_required` by a prior `/analyze-trace` call (see schema above). Rejects (404/409) if that step was never analyzed or wasn't flagged `approval_required`. **Known gap**: it does NOT reject a second decision on an already-decided `(trace_id, step_id)` — confirmed live, a second call silently overwrites the first (`INSERT OR REPLACE`, no check against `approval_decisions`). Fix before a live demo if double-decision protection matters.
- `POST /run-agent` — runs the mock agent loop for a given `original_goal` and returns the resulting `Trace`.
- CORS is enabled for any `http://localhost:<port>` or `http://127.0.0.1:<port>` origin, so the Streamlit frontend can call this API from a different port.

## Coding Conventions

- Type hints on all function signatures.
- Pydantic models for all request/response shapes — no raw dicts crossing the API boundary.
- Keep `drift_engine.py`, `provenance.py`, and `gate.py` independently testable — no direct imports between them; `main.py` orchestrates the calls between components.
- All Gemini calls go through a small wrapper (put it in `config.py` or a `clients.py` if it grows) so the model name is set in one place, not scattered across files.
- Never hardcode the API key — always load from `.env` via `python-dotenv`.

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
