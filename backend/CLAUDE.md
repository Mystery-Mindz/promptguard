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
- **Orchestrating agent model**: GPT-5.4 mini (OpenAI function calling)
- **Judge/classifier model**: GPT-5.4 nano
- **Embeddings**: text-embedding-3-small
- **Graph/provenance store**: NetworkX (in-memory), SQLite for persistence
- **OpenAI integration**: raw OpenAI Python SDK calls — do NOT introduce LangChain or other orchestration frameworks
- **Testing**: pytest

## Repo Structure

```
/app
  main.py          # FastAPI app + routes
  schemas.py       # Pydantic models — the shared data contract (see below)
  agent.py         # Orchestrating agent (GPT-5.4 mini + function calling)
  tools.py         # Mock tools: read_email, delete_file, send_message
  drift_engine.py  # Intent Drift Engine
  provenance.py    # Provenance Tracer (NetworkX graph)
  gate.py          # Secure Execution Gate (decision logic)
  config.py        # Thresholds, model names, API key loading — ALL tunable values live here, nowhere else
requirements.txt
.env               # OPENAI_API_KEY (never commit this file)
tests/
  test_drift_engine.py
  test_provenance.py
  test_gate.py
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

## Component Responsibilities

- **agent.py**: Runs a multi-step tool-calling loop using GPT-5.4 mini. Takes `original_goal`, lets the model call tools from `tools.py`, records each step in the trace format above. Stops after the model signals completion or after 6 steps.
- **drift_engine.py**: Embeds `original_goal` and each step's action description using text-embedding-3-small, computes cosine similarity, converts to `drift_score` (low similarity = high drift). Also calls GPT-5.4 nano to classify whether the input looks like an injected instruction. Combines both into `risk_score` (0-100).
- **provenance.py**: Builds a directed graph (NetworkX) of steps, tagging each with `internal`/`external`. When `actor` changes between steps (an agent handoff), the receiving agent's node inherits the `external` tag from any upstream external source, even if it superficially looks like a trusted handoff. Exposes `get_provenance_flag(trace, step_id)`.
- **gate.py**: Pure decision logic. `decide(risk_score, provenance_flag)` — thresholds live in `config.py`, not hardcoded here. Starting thresholds: `risk_score > 80` → `block`; `40 <= risk_score <= 80` → `approval_required`; else `allow_logged`. If `provenance_flag == "tainted"`, floor the decision at `approval_required` regardless of score.
- **config.py**: The only place thresholds, model names, and other tunable constants should live. When tuning during testing, edit here.

## API

- `POST /analyze-trace` — accepts a trace (see schema above), returns a list of detection outputs, one per step.

## Coding Conventions

- Type hints on all function signatures.
- Pydantic models for all request/response shapes — no raw dicts crossing the API boundary.
- Keep `drift_engine.py`, `provenance.py`, and `gate.py` independently testable — no direct imports between them; `main.py` orchestrates the calls between components.
- All OpenAI calls go through a small wrapper (put it in `config.py` or a `clients.py` if it grows) so the model name is set in one place, not scattered across files.
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
