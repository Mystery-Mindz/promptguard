# PromptGuard — 7-Day Finish Plan (Overview: everyone reads this)

**Today:** Mon 19 Oct · **Final demo/submission:** Mon 26 Oct · **Code freeze:** Sat 24 Oct, 11:59 pm

## The finished product

A real AI assistant, protected live by PromptGuard, usable through **5 interfaces**, deployed on public URLs:

| # | Interface | What it's for | Built by |
|---|---|---|---|
| 1 | **Web chat app** | Talk to the Protected Assistant; PromptGuard ON/OFF toggle; see every action it tried | Joshwa (P3) |
| 2 | **Telegram bot** | Same assistant from your phone | Nikil (P2) |
| 3 | **Attacker form** | Anyone (a judge!) sends a poisoned email into the assistant's inbox | Joshwa (UI) + Nikil (endpoint) |
| 4 | **Agent Activity Dashboard** | Live sessions, risk per step, "run agent" button | Joshwa (P3) |
| 5 | **Operator Approval Console** | Real logins; approve/deny paused actions; the agent resumes | Joshwa (UI) + Aadhira (auth API) |

```
 Chat app / Telegram / Attacker form
              │
              ▼
   Protected Assistant service  (Nikil)  — real LLM, real sandboxed tools:
   web_fetch · inbox · files · send_message · researcher→executor handoff
              │ every tool call, BEFORE it runs (SDK)
              ▼
   PromptGuard backend          (Aadhira)  — /guard/*: drift + judge + provenance → allow / approve / block
              │
              ▼
   Dashboard + Approval Console (Joshwa)  — deployed on Streamlit Cloud; backends on Render; DB on Neon
```

**The demo:** a judge scans a QR code and sends a poisoned email → we ask the assistant (chat or Telegram) "summarise my inbox" → PromptGuard pauses the hijacked `send_message` → the console lights up → the operator denies → we switch the guard OFF → the same attack succeeds, and the outbox shows the leak.

## Who does what

| Person | Role | Load | File |
|---|---|---|---|
| **Person 1 (Aadhira)** | Core backend: bug fixes, LLM provider layer, live guard API, SDK, auth | Heavy | `P1_core_backend.md` |
| **Person 2 (Nikil)** | Protected Assistant: real agent, real tools, multi-agent, Telegram, red-team benchmark | Heavy | `P2_agent_integrations.md` |
| **Person 3 (Joshwa)** | Interfaces + deployment: chat app, attacker form, dashboard, console, all deploys | Heavy | `P3_interfaces_deploy.md` |
| **Person 4 (Sanjeev)** | Attack-lab pages, README, pitch, deck, demo video | **Light** | `P4_docs_demo.md` |

## Day-by-day at a glance

| Day | Aadhira (P1) Core | Nikil (P2) Agent | Joshwa (P3) Interfaces/Deploy | Sanjeev (P4) Docs |
|---|---|---|---|---|
| **Tue 20** | Run tests; fix backend bugs; **publish guard contract + mock mode** | Assistant scaffold, sandbox DB, real tools | Fix frontend bugs; dashboard run button | Attack-lab pages |
| **Wed 21** | Provider layer + speed fixes; block tier; trace_18 | Agent loop, guarded/unguarded, pause/resume; **mock /chat for P3** | Deploy skeleton (Render + Neon + Streamlit Cloud) | Publish pages on GitHub Pages |
| **Thu 22** | Real `/guard/*` + SDK | Multi-agent handoff; switch to the real SDK | Chat app | README draft + diagram |
| **Fri 23** | Auth, API keys, rate limits; Postgres storage | Telegram bot | Attacker form, live sessions, console login + resume | Pitch draft, deck outline |
| **Sat 24** | Integration + validation re-run · **CODE FREEZE 11:59 pm** | Red-team scenarios + benchmark runner | Deploy everything; smoke test | — |
| **Sun 25** | Bug-fix only; security pass | Run the benchmark → final numbers | Polish, mobile check, QR codes | Real numbers into deck/README; record backup video |
| **Mon 26** | 2 full dry runs in the morning → **submit** | ← same | ← same | Final checklist |

## Rules that keep four people from breaking each other

1. **Branch per task**, merge to `main` via PR, and **CI must be green**. One person reviews each PR (anyone).
2. **The contracts below are frozen once published.** You may ADD fields. Never rename or remove one without telling the group.
3. **Mocks first.** P1 ships `GUARD_MODE=mock` on Day 1 and P2 ships a mock `/chat` on Day 2, so nobody waits on anybody.
4. **Never commit `.env` or keys.** Secrets go in each platform's settings.
5. **Daily 15-minute sync at 9 pm**: what merged, what's blocked, and what you're doing tomorrow. Tick boxes in your plan file.
6. **Quota discipline:** don't run the full validation or the benchmark casually. Use mocks in tests.

## Shared contracts (frozen Tue 20 Oct, 6 pm)

### A. PromptGuard live guard API (owner: P1, base URL `PG_URL`)
All `/guard/*` calls send the header `X-PromptGuard-Key: <key>`.
| Method & path | Body | Returns |
|---|---|---|
| `POST /guard/session` | `{agent_id, original_goal}` | `{session_id}` |
| `POST /guard/observe` | `{session_id, actor, input_text, input_source, input_provenance}` | DetectionOutput (content arriving: an email, a web page, a handoff) |
| `POST /guard/check` | `{session_id, actor, input_text, input_source, input_provenance, action, action_params}` | DetectionOutput + `approval_status: "not_required" \| "pending"`. Call **before** running the tool. |
| `GET /guard/approval/{session_id}/{step_id}` | — | `{status: "pending" \| "approved" \| "denied", decided_by, decided_at}` |
| `GET /guard/session/{session_id}` | — | `{trace, detections}` (same as `GET /trace/{id}`) |
| `GET /guard/sessions?limit=20` | — | `[{session_id, agent_id, original_goal, created_at, step_count, max_risk, worst_decision}]` |
| `GET /pending-approvals` | — | existing fields **+** `action, action_params, input_text, drift_score, classification` |
| `POST /auth/login` | `{username, password}` | `{token, operator_id}`. The console then sends `Authorization: Bearer <token>` to `/pending-approvals` and `/approval-decision`. |

DetectionOutput = `{trace_id, step_id, drift_score, risk_score, provenance_flag, classification, explanation, decision}` (unchanged; `trace_id` = `session_id`).

### B. Protected Assistant API (owner: P2, base URL `ASSISTANT_URL`)
| Method & path | Body | Returns |
|---|---|---|
| `POST /chat` | `{conversation_id, message, guarded: bool}` | `ChatResult` |
| `POST /chat/resume` | `{conversation_id}` | `ChatResult` (call after the operator decides) |
| `POST /inbox/send` | `{from_addr, subject, body}` | `{email_id}` (public, rate-limited) |
| `GET /sandbox/{conversation_id}` | — | `{inbox:[{id,from_addr,subject,body,received_at}], files:[{path,size}], outbox:[{to,content,sent_at,guarded}]}` |
| `POST /sandbox/reset` | `{conversation_id}` | `{ok: true}` |
| `POST /telegram/webhook/{secret}` | Telegram update | 200 |

`ChatResult = {status: "done" | "awaiting_approval" | "blocked" | "error", reply, session_id, steps: [{step_id, actor, tool, args, decision, risk_score, explanation, executed, result_preview}], pending_step_id?}`

## Still to decide
- **Team:** P1 = Aadhira · P2 = Nikil · P3 = Joshwa · P4 = Sanjeev
- **The LLM provider (Aadhira, by Tue night).** Options: Groq free tier for the agent + judge (recommended; much higher free limits), paid Gemini, or free Gemini only (the demo will run out of quota). P1's Day 2 prompt has a slot for the answer.

## Using the prompts
Each person's file has prompts written for a **coding agent opened at the repo root** (Claude Code, Codex CLI, Gemini CLI). If you only have chat ChatGPT/Gemini, paste the files a prompt names into the chat along with it. Start every session with:
```
Read backend/CLAUDE.md, docs/plan/00_OVERVIEW.md and docs/BUG_REPORT.md before doing anything. Follow the shared contracts in 00_OVERVIEW.md exactly. Work on a new git branch, run the tests before you finish, and tell me what you changed and how you verified it.
```
