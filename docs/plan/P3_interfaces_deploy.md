# Person 3 (Joshwa) — Interfaces + Deployment · HEAVY

**You own:** everything in `frontend/` (chat app, attacker form, activity dashboard, approval console) and every deployment (Render, Neon, Streamlit Cloud, GitHub Pages, uptime pings, smoke test).
**You depend on:** P2's mock `/chat` (Wed evening), P1's `/guard/sessions` (Thu) and `/auth/login` (Fri). Build against mocks until then.

Start every session with the preamble in `00_OVERVIEW.md`. Contracts A and B are in the overview.

---

## Day 1 · Tue 20 — Fix the frontend bugs + the run button

- [ ] BUG-09, 10, 11 fixed (see `docs/BUG_REPORT.md`)
- [ ] The dashboard can start a new agent run and show its analysis

**Prompt 1.1 — Frontend bug fixes**
```
Read docs/BUG_REPORT.md (BUG-09, BUG-10, BUG-11) and everything in frontend/. Fix:
- BUG-10: BACKEND_URL defaults to http://127.0.0.1:8000. Read settings from st.secrets first, then env vars, then the default (add a get_setting() helper in config.py). Add frontend/.streamlit/secrets.toml.example.
- BUG-09 in approval_dashboard.py: reset the "approved/denied" status when moving to the next item (show a toast for the last decision instead of a sticky banner); wrap every backend call so 404/409/timeouts/connection errors show a friendly st.error, never a traceback; show the actual action, action_params, input_text (in a code box) and drift_score + classification when the backend provides them (P1 is adding these fields to /pending-approvals; use .get() so it works before and after); show the whole queue as a list with the oldest item expanded, plus an auto-refresh toggle (every 5 s).
- BUG-11 in app.py: Demo Mode must show a realistic example of what the real system outputs (use the real tainted handoff example: tainted, approval_required, risk in the 40s–50s), round drift to 2 decimals, and wrap get_trace errors.
- Run `ruff check --fix frontend/` and fix the rest by hand.
Test by hand against a locally running backend: approve one item, then check that the next item doesn't show a stale banner, and stop the backend to see a friendly error.
```

**Prompt 1.2 — "Run a new agent task" in the dashboard**
```
Read frontend/app.py, frontend/utils.py and the backend endpoints POST /run-agent and POST /analyze-trace in backend/CLAUDE.md. Add a "Run a new agent task" section to app.py (as a st.tabs tab next to "Look up a trace"):
- A goal text box + 3 presets ("Summarise my inbox", "Clean up old files in /tmp", "Message the team about the sync") and a Run button.
- Run → POST /run-agent (don't send a trace_id; the backend generates one after P1's fix) → POST /analyze-trace with the returned trace → render it with the existing per-step view, and show the trace_id so it can be looked up later.
- st.status progress text; analysis can take a while, so use a timeout from config.py (start at 180 s). A 503 shows "The AI model's quota is exhausted; try again later." A timeout shows a clear message.
- Put the API calls in utils.py and the settings in config.py.
```

---

## Day 2 · Wed 21 — Deploy the skeleton (deploy early so demo day has no surprises)

- [ ] Backend live on Render with Postgres on Neon; the existing 2 Streamlit apps live on Streamlit Cloud
- [ ] `docs/DEPLOY.md` written

**Prompt 2.1 — Deployment config**
```
Read the README, backend/Dockerfile, frontend/Dockerfile.*, backend/app/main.py (CORS + /health) and docs/plan/00_OVERVIEW.md. Prepare the deployment:
- render.yaml (Render Blueprint) with two Docker web services: promptguard-backend (backend/Dockerfile, health check /health) and promptguard-assistant (assistant/Dockerfile; create it in the same style as backend's if it's missing). Env vars listed with sync:false for secrets: GEMINI_API_KEY, GROQ_API_KEY, DATABASE_URL, JWT_SECRET, PROMPTGUARD_API_KEYS, EXTRA_CORS_ORIGINS, PG_URL, PG_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET.
- Streamlit Community Cloud: each app (app.py, approval_dashboard.py, chat_app.py, attacker_form.py) must run from the repo with frontend/requirements.txt, reading settings via the get_setting() helper (st.secrets → env).
- In every Streamlit app, a "waking the server up…" state: ping /health with retries (Render's free tier sleeps after 15 min, and a cold start takes ~50 s) before the first real call.
- docs/DEPLOY.md: exact click-by-click steps for creating the Neon Postgres DB, deploying the Render blueprint, the four Streamlit apps and their secrets, setting the Telegram webhook, GitHub Pages for the attack lab, and UptimeRobot pings on both /health URLs; every env var with where it's set; and a post-deploy checklist.
- No real secrets in any file.
```
**Then by hand:** create the Neon DB → deploy the Render blueprint → deploy `app.py` and `approval_dashboard.py` on Streamlit Cloud → run `python backend/scripts/live_check.py --base-url <render-url>` → share the URLs in the group chat.

---

## Day 3 · Thu 22 — Chat app (interface #1)

- [ ] `frontend/chat_app.py` works against P2's mock `/chat`, then the real one

**Prompt 3.1 — Chat app**
```
Read docs/plan/00_OVERVIEW.md (contract B) and the existing frontend patterns (config.py get_setting, utils.py). Build frontend/chat_app.py (Streamlit, port 8503), the main demo screen:
- The top: a big, obvious toggle "🛡️ PromptGuard ON / OFF" (maps to guarded) with a one-line explanation, and a "New conversation / reset sandbox" button.
- Chat with st.chat_message. Under each assistant reply, an expander "What the agent tried to do": each step as a row with actor (researcher/executor), tool, args, a decision chip (🟢 allowed · 🟠 waiting for approval · 🔴 blocked · ⚪ unguarded/logged), risk score, explanation, executed yes/no, result preview.
- status awaiting_approval: show "⏸ PromptGuard paused <tool>; waiting for an operator to approve or deny", poll POST /chat/resume every 3 s (st.rerun loop, max 3 min), then show the outcome.
- A sidebar with 3 live panels from GET /sandbox/{conversation_id}: 📥 Inbox, 📁 Files, 📤 OUTBOX ("what actually got sent"). Highlight in red anything sent to a non-allowed domain. This panel is what makes guard ON vs OFF obvious.
- 4 suggested prompts as buttons ("Summarise my inbox", "Read https://<attack-lab page> and do what it suggests", "Tidy up /documents", "Email the team the sync notes").
- Works on a phone screen. Errors are friendly, never tracebacks. Start against MOCK_LLM=1 on the assistant.
```

---

## Day 4 · Fri 23 — Attacker form, live dashboard, console login (interfaces #3, #4, #5)

- [ ] Attacker form live
- [ ] The dashboard shows live sessions
- [ ] The console uses `/auth/login`, and approving resumes the chat

**Prompt 4.1 — Attacker form + live dashboard + console login**
```
Read docs/plan/00_OVERVIEW.md (contracts A and B) and frontend/.
1. frontend/attacker_form.py (port 8504): a public page titled "Try to hijack the assistant". Fields: from, subject, body. 4 one-click templates (urgent admin, hidden footer instruction, fake calendar invite, "AI assistant note"). Submit → POST {ASSISTANT_URL}/inbox/send, then show "Sent! Now watch the chat app" with a link. Handle the rate-limit (429) message nicely. Simple enough for a judge on a phone.
2. app.py: a "Live sessions" tab using GET /guard/sessions (auto-refresh every 5 s): a table of session, goal, steps, max risk and worst decision with colour, where clicking opens the existing per-step view via GET /guard/session/{id}. Keep the "Look up a trace" and "Run a new agent task" tabs.
3. approval_dashboard.py: replace the shared-token box with a username/password login → POST /auth/login → store the JWT in st.session_state → send Authorization: Bearer on /pending-approvals and /approval-decision. Show the logged-in operator's name; operator_id comes from the token now. Add a logout button. Remove APPROVAL_TOKEN from config.py.
4. Make a QR-code image for the chat app and attacker form URLs (the qrcode package) and save it to docs/qr/ for the deck.
```

---

## Day 5 · Sat 24 — Deploy everything + smoke test (code freeze tonight)

- [ ] All 5 interfaces live on public URLs; the Telegram webhook set
- [ ] Smoke test passes against production

**Prompt 5.1 — Production smoke test**
```
Write scripts/smoke_test.py, which takes the deployed URLs (PG_URL, ASSISTANT_URL, operator credentials, API key) from env and checks end to end: both /health endpoints (with cold-start retries); /sandbox/reset; POST /inbox/send a poisoned email; POST /chat guarded "Summarise my inbox and handle anything urgent" → expect no attacker address in the outbox, and a blocked or awaiting_approval step; if awaiting, log in → find it in /pending-approvals → deny → /chat/resume → outbox still clean; repeat unguarded → expect the attacker address in the outbox. Print a PASS/FAIL table; exit 1 on any failure. Add .github/workflows/smoke.yml (workflow_dispatch only) that runs it with repo secrets.
```

## Day 6 · Sun 25 — Polish
- [ ] Every interface checked on a phone; loading states; no tracebacks anywhere
- [ ] Same look across all 4 Streamlit apps (title, icon, colours)
- [ ] The smoke test passes in the morning and the evening

## Day 7 · Mon 26 — Dry runs + submit
- [ ] Warm all services 30 min before judging (open each URL)
- [ ] Have the backup video ready (P4) in case the Wi-Fi fails
