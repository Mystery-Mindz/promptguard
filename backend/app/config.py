"""Centralized configuration for the PromptGuard backend.

This is the only place tunable values (API keys, model names, gate
thresholds, scoring weights, timing, and file paths) should be set — every
other module imports from here rather than hardcoding its own copy, so a
value only ever needs to change in one place. See the comment above each
group of constants below for what it controls and why its current value
was chosen.
"""

import os

from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Model names — the only place these should be set.
# Confirmed against a live client.models.list() call on 2026-09-08 — see
# run_validation.py / the Gemini migration notes for the full model list.
# NOTE: the free tier caps EVERY Flash model at only 20 generateContent
# requests/DAY (GenerateRequestsPerDayPerProjectPerModel-FreeTier), tracked
# independently per model — there is no separate, higher-quota "stable" GA
# tier available on this account. The only models whose metadata says
# "Stable version..." are gemini-2.5-flash / gemini-2.5-flash-lite, and both
# 404 as "no longer available to new users." The -latest aliases just
# resolve to gemini-3.8-flash under the hood (same quota bucket). Hit the
# cap on gemini-3.8-flash, gemini-3.6-flash, gemini-3.7-flash, and
# gemini-3.5-flash in succession on 2026-09-08 (4 models in one session).
# On gemini-3.1-flash-lite now (untouched at time of switch, confirmed to
# support function calling) — same 20/day structure applies; re-check
# quota before a live demo needing several re-runs.
AGENT_MODEL = "gemini-3.1-flash-lite"
JUDGE_MODEL = "gemini-3.1-flash-lite"
EMBEDDING_MODEL = "gemini-embedding-2"  # latest stable embedContent model

# Gate thresholds — the only place these should be set.
RISK_BLOCK_THRESHOLD = 80
RISK_APPROVAL_LOW_THRESHOLD = 40
RISK_APPROVAL_HIGH_THRESHOLD = 80

# Drift engine risk_score combination weights — must sum to 1.0.
DRIFT_SCORE_WEIGHT = 0.6
INJECTION_CONFIDENCE_WEIGHT = 0.4

# Agent loop cap — the only place this should be set.
AGENT_MAX_STEPS = 6

# SQLite persistence path — the only place this should be set. Overridable
# via the DATABASE_PATH env var (e.g. to point at a mounted persistent
# volume in a deployment container); defaults to a file next to the repo
# for local dev.
DATABASE_PATH = os.getenv(
    "DATABASE_PATH", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "promptguard.db")
)

# Gemini call pacing/timeouts — the only place these should be set. Shared by
# agent.py (agent loop) and drift_engine.py (embedding + judge calls) so a
# quota-driven change only needs to happen in one place.
MIN_SECONDS_BETWEEN_GEMINI_CALLS = 13.0
GEMINI_CLIENT_TIMEOUT_MS = 30_000

# Default trace_id for /run-agent requests that don't supply their own —
# the only place this default should be set.
DEFAULT_TRACE_ID = "trace_001"
