import os

# Backend API base URL — the only place this should be set. Override via the
# BACKEND_URL env var when running against a different host/port.
BACKEND_URL = os.getenv("BACKEND_URL", "http://192.168.29.70:8000")

# Request timeouts (seconds) — the only place these should be set.
DEFAULT_TIMEOUT_SECONDS = 30
ANALYZE_TRACE_TIMEOUT_SECONDS = 90  # analyze-trace can trigger several live Gemini calls

# Demo-only operator "authentication" for the Operator Approval Console — a
# single shared token, not real auth (no per-user identity, no expiry, no
# hashing). Intentional for a hackathon demo; replace with real auth before
# this ever handles non-demo data. Override via the APPROVAL_TOKEN env var.
APPROVAL_TOKEN = os.getenv("APPROVAL_TOKEN", "promptguard-demo")
