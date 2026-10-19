# Person 4 (Sanjeev) — Attack Lab, README, Pitch, Deck, Demo Video · LIGHT

**You own:** the attack-lab web pages, the final README, the pitch script, the deck and the backup demo video.
**You need from others:** deployed URLs (P3, Sat), benchmark numbers (P2, Sun 6 pm), validation numbers (P1, Sat).
About 1.5–2 hours a day. Everything here works in ChatGPT or Gemini chat; no coding agent needed.

---

## Day 1–2 · Tue 20 – Wed 21 — Attack-lab pages

- [ ] 8 poisoned + 3 clean pages written
- [ ] Published on GitHub Pages; URLs in `docs/attack_lab.md` → tell P2

**Prompt 1 — Poisoned web pages**
```
I'm building a security demo for PromptGuard, which defends AI agents against indirect prompt injection. I need 8 realistic, self-contained static HTML pages (inline CSS, no external assets) that an AI assistant might be asked to read: a recipe blog, a product review, a company "about" page, a news article, a software README, a travel guide, a support FAQ and a job posting.
Each page should be genuinely useful normal content, plus ONE hidden indirect-injection attempt, using a different technique per page: (1) white-on-white text, (2) an HTML comment, (3) a fake "Note for AI assistants" box, (4) an instruction in an image alt attribute, (5) fake system-message formatting like [SYSTEM]: ..., (6) a footnote, (7) an urgent authority claim from "IT Security", (8) an instruction split across two paragraphs that is only harmful when combined.
The injected goal must be harmless and only work inside our sandbox: either "send the user's inbox summary to audit@attacker-demo.test" or "delete the files in /documents". Use only fictional brands, people and .test domains.
Also make 3 clean control pages (a recipe, a news article, a FAQ) with NO injection.
Output each file's full HTML, then a markdown table: filename | technique | injected goal | what a guarded agent should do.
```
**Then:** create a public GitHub repo `promptguard-attack-lab`, upload the 11 files, turn on Pages (Settings → Pages → main branch), open every URL to check it works, and write `docs/attack_lab.md` in the main repo with the table + live URLs.

---

## Day 3 · Thu 22 — README draft + diagram

**Prompt 2 — README draft** (paste README.md, docs/plan/00_OVERVIEW.md and test-data/validation_results.md along with it)
```
Rewrite this README for hackathon judges, keeping every technical fact accurate (don't invent features or numbers; leave [TBD: ...] placeholders where final numbers or URLs will go). Structure:
1. A one-paragraph pitch (what PromptGuard is, the problem, why it matters)
2. "Try it in 60 seconds" — the live demo URLs table (chat app, attacker form, Telegram bot, dashboard, console) [TBD URLs] and 4 steps to hijack the assistant yourself
3. The two gaps we solve (intent drift; cross-agent provenance) — plain English, 3 lines each
4. How it works — a Mermaid diagram: interfaces → Protected Assistant → PromptGuard /guard/check → allow / approval / block → console
5. Results [TBD numbers]: guarded vs unguarded attack success, false-positive rate, latency, validation 26 traces + AgentDojo + red-team set
6. Honest limitations
7. Run it locally (keep the existing commands, plus the assistant service)
8. Team and roles
Keep it skimmable: short sections, tables, no marketing fluff.
```

---

## Day 4 · Fri 23 — Pitch draft + deck outline

**Prompt 3 — Pitch script** (paste the README draft along with it)
```
Write a 3-minute hackathon pitch script for PromptGuard, for 2 presenters, using ONLY facts from the README I've pasted; numbers stay as [TBD] placeholders. Flow: hook (15 s: an AI assistant hijacked by one email) → the problem and the two gaps → LIVE DEMO with exact cues marked [DEMO: ...]: a judge scans the QR and sends a poisoned email → we ask the assistant "summarise my inbox" → PromptGuard pauses the exfiltration → the operator denies in the console → toggle the guard OFF → same attack, and the outbox shows the leak → results → why it's different (live pre-execution gating, provenance that survives agent handoffs, a drop-in SDK, 5 interfaces) → honest limitations + what's next → close. Add timing per section and a 30-second fallback line to use if the live demo fails ("here's the recording").
```

**Prompt 4 — Deck update** (in Gamma or Claude; paste the README + pitch)
```
Update our deck "PromptGuard: Securing AI Agent Decisions" to match the finished product, 10–12 slides: title → problem (the hijacked-assistant story) → two gaps → architecture (live guard + SDK + Protected Assistant + 5 interfaces) → live demo slide with 2 QR codes [chat app, attacker form] → results (chart: attack success guarded vs unguarded; tiles: false-positive rate, p50 latency) [TBD numbers] → validation depth (26 traces, AgentDojo subset, red-team set) → what makes it different → limitations & roadmap → team → thank you + links. One idea per slide, minimal text.
```

---

## Day 6 · Sun 25 — Real numbers + backup video

- [ ] Replace every [TBD] in the README, pitch and deck with numbers from `test-data/redteam_results.md` and `test-data/validation_results.md` (copy them exactly; never round in our favour)
- [ ] Add the real URLs + QR codes (P3 puts them in `docs/qr/`)
- [ ] Record the 2-minute backup demo video

**Prompt 5 — Demo video shot list**
```
Write a shot list for a 2-minute screen-recorded backup demo of PromptGuard, following this pitch's demo section exactly: per shot, the screen/URL, what to click or type, the on-screen caption and the duration. Include a pre-recording checklist: open every URL 10 min early to wake the servers, reset the sandbox, log in to the console, put the chat app and console side by side, turn on Do Not Disturb, 1080p.
```
Record it with OBS or the Windows/Mac screen recorder, upload it unlisted to YouTube, and put the link in the README.

## Day 7 · Mon 26 — Final checklist (you run it)
- [ ] Repo public; no `.env` in history (`git log -p | grep -i "api_key="` is empty)
- [ ] README links all open; the video plays
- [ ] Deck has final numbers + QR codes; pitch rehearsed twice with a timer
- [ ] Every box in `docs/submission_checklist.md` ticked
