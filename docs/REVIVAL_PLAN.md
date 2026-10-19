# Nhako-Bot Revival Plan

Written 2026-10-19 after reading every file in the repo and measuring this machine (kimmain).
Nothing has been changed in the code yet, and no git actions were taken.

---

## 1. What this machine can actually do

### Measured hardware

| Part | What's there | What it means for an LLM |
|---|---|---|
| CPU | Ryzen 5 5500, 6C/12T | OK for whisper and light work. Too slow to be the main LLM engine, and it's shared with three game servers |
| RAM | 31.8 GB DDR4-2666 (4×8 GB, mixed brands) | **28 GB is given to the WSL/Docker VM** (`.wslconfig`), and that budget is already planned for getgud 13G + valheim 6G + kimzamwrld 4.5G + mcweb ~1.9G. Windows keeps a floor of about 3.8 GB (usually around 8–12 GB is free) |
| GPU | GTX 1050 Ti, **4 GB VRAM**, Pascal (compute 6.1), driver 582.66 | This is what makes it workable. **But 1.8 GB is already used by the Windows desktop**, so only about **2.2 GB** is really free |
| Disk | Plenty of free space on D:/E:/F: | Not a constraint |
| Software | Docker 29.8 with 28 containers running. Python 3.14 and 3.11. **Ollama is not installed** | |

### Verdict: yes, but only with a *small* model, on the *GPU*, run by *Ollama natively on Windows*

- **Run Ollama natively on Windows, not in Docker.** The model weights go in VRAM, so it only needs about 0.5–1 GB of Windows RAM. In Docker, it would come out of the 28 GB VM budget, and that budget is already spoken for by the game servers.
- **The GPU is required.** 3–4B models on CPU (dual-channel DDR4-2666) run at about 5–10 tokens/s, and reading the prompt is slow (~20–60 tok/s). A 1.5k-token tool prompt would take 30–70 s before the first word, and it would take CPU away from Valheim/MC. On the 1050 Ti, expect about 15–25 tok/s and a reply in a few seconds.
- **Model size ceiling: about 2–2.5 GB of weights (2–4B parameters at Q4_K_M).** A 7–8B model (~4.5–5.5 GB) will not fit. It would spill to CPU and be slow.
- **Context is small.** With ~2.2 GB free VRAM, keep `num_ctx` around 4k–8k. That's fine for commands. It's not enough for "summarise this 30-page PDF".
- **Pascal support is a real long-term risk.** Ollama/llama.cpp still ship a CUDA 12 build that covers compute 6.1. CUDA 13 dropped Pascal, so a future Ollama release could drop this card. **Plan: pin an Ollama version that works, and don't auto-update blindly.**
- **The desktop's 1.8 GB of VRAM can't be moved.** The Ryzen 5 5500 has no integrated graphics, so the 1050 Ti drives the display too. `OLLAMA_KEEP_ALIVE=15m` at least frees the model's VRAM when the bot is idle.

### Upgrade path (optional, not needed for the goals below)
A used **RTX 3060 12 GB** is the usual next step. It would allow 8–14B models, which are much better at multi-step reasoning and juggling many tools.

---

## 2. What's possible and what isn't

| Wish | Possible? | Notes |
|---|---|---|
| Add / edit / remove calendar events | ✅ Yes | Google Calendar API. Small models are fine at pulling out *title + "next Tuesday 3pm"*. **Date math must be done by code** (`dateparser`), not by the model. Destructive actions need a confirm button |
| Reminders | ✅ Yes | Two kinds: (a) Google Calendar popup reminders on events; (b) the bot itself pushes Telegram messages (daily agenda at 8am, "in 10 min: X") using PTB's JobQueue, stored in SQLite so they survive restarts |
| Basic browsing | ✅ Search + read a page | DuckDuckGo search (`ddgs`) + fetch the page + extract readable text (`trafilatura`) + summarise with sources. **Interactive browsing (clicking, logins, forms) is not realistic**: headless Chromium costs 300–500 MB RAM and a 3B model can't drive it reliably |
| "Who's on getgud?" | ✅ Yes, without the LLM | `mc-server` publishes 25565. The `mcstatus` library pings it for online count + player sample. For the full list, RCON `list` (read-only). The LLM only has to recognise the question, or use `/mc` |
| Status of my Docker stacks | ✅ Yes, read-only | Same pattern as `homepage-socket-proxy`: a dedicated `tecnativa/docker-socket-proxy` with `CONTAINERS=1`, everything else 0. Can also **push alerts** when a container turns unhealthy. Start/stop/restart: possible, but leave it out at first |
| Postiz | ✅ Yes | Postiz has a public API (API key from its settings): list/schedule posts. Check the endpoints against your Postiz version |
| Notion | ✅ Yes | Official API + integration token: add page to a DB, query tasks |
| "Anything else" (MCP servers) | ⚠️ Limited | You *can* plug in MCP servers, but a 2–4B model gets unreliable once it sees more than ~5–8 tools at once. The design below routes to a *category* first and then shows only that category's tools |
| Voice notes | ✅ Already works | faster-whisper `base` int8 on CPU: a 10 s note takes about 1–3 s with `beam_size=1`. Keep it on CPU, since VRAM belongs to the LLM |
| Long-term memory | ⚠️ Simplify | It works today but is costly (see bugs). A small "facts about me" store beats embedding every message |
| Vision (README roadmap) | ❌ Not now | `llama3.2-vision` is 11B and won't fit. A ~3B VLM would have to swap out the chat model every time. Low priority |
| Complex multi-step agent work, long documents | ❌ Not on this GPU | That's the 12 GB-card tier, or a free-tier cloud API as an optional fallback |

---

## 3. Audit of the old code (all checked against the source)

### 🔴 Bugs that break things or are dangerous

1. **Any message with "delete / remove / cancel / clear" goes to calendar deletion.** See `SRC/main.py:125`. "How do I remove a stain?" ends up deleting a calendar event, and `clear_day` wipes a whole day **with no confirmation** (`SRC/my_calendar.py:171`).
2. **The keyword router misfires a lot.** "add", "put", "remind", "set a" (`main.py:151`) catch normal chat ("what should I put in my CV" → create event). `"search" in user_lower` matches "re**search**". `"any"` + "week" sends a lot of chat to the calendar reader.
3. **The Dockerfile can't start.** It runs `CMD ["python", "src/main.py"]`, but the folder is `SRC/`, and Linux is case-sensitive. There's no `.dockerignore`, so `.env`, `token.json`, `credentials.json` and `.venv` would be **copied into the image**. Ollama is `localhost` inside the container, so it can't be reached.
4. **`start_bot.bat` starts two bots at once.** `start pythonw src/main.py` launches one copy, and then the `:loop` launches a second one. Two pollers on the same token cause Telegram `Conflict` errors. The path is hardcoded to `D:\Projects\...`, but the project now lives on `E:\`. `//autrestart logic` isn't a batch comment.
5. **There's no Google OAuth flow anywhere.** `InstalledAppFlow` isn't in the code. The README says "the first run opens a browser for consent", but that isn't true. Refreshed tokens are never written back to `token.json`. `get_upcoming_events` checks `../token.json` and the other three functions don't.
6. **Everything blocks the async loop.** Ollama, whisper, psycopg2 and the Google API are all called synchronously inside `async def chat`. While the model is thinking, the bot is completely frozen.

### 🟠 Wrong behaviour / poor quality

7. The system prompt is sent as `role: "assistant"` instead of `"system"` (`main.py:90`, `main.py:200`), so the model treats it as something it said, not as instructions.
8. The calendar reader fetches only the **next 5 events** (`my_calendar.py:34`). "What do I have next week?" usually can't be answered.
9. Delete searches only future events and deletes **the first match** without asking.
10. There's no **edit** at all, and reminders aren't set on events.
11. The 3B model has to compute ISO datetimes itself (`main.py:158`). That's the most common source of wrong dates.
12. `generate_json` strips ``` by hand. Ollama's `format=` (JSON schema / structured output) does this properly.
13. Web search lowercases the query and strips words by string replace. It returns only snippets. `duckduckgo_search` has been renamed to `ddgs`.
14. Memory: every message makes **two extra embedding calls** (user + assistant). On 4 GB VRAM, that means swapping the chat and embedding models in and out. Each call opens a new DB connection and runs `CREATE EXTENSION` every time. `init_db()` runs at import time. There's no vector index. Only the user's lines are used.
15. Whisper loads at import (startup cost even if you never send voice). Temp `.ogg` files go in the CWD. `beam_size=5` is slow on CPU.
16. Replies over 4096 chars make Telegram's `send_message` fail.
17. Hardcoded values everywhere: model name in 2 files, timezone in 3 places, `datetime.utcnow()` (deprecated).

### 🟡 Housekeeping

18. Dependencies aren't pinned (except `python-telegram-bot==20.7`, which is ~2 years behind 22.x).
19. Logging is a mix of `print()` and `logging`. There are no tests.
20. `check_ollama.py` is a loose script, not a health check the bot uses.
21. A comment says "Clear memory on start", but nothing is cleared.

### 🟢 Worth keeping
- The single-user `ALLOWED_USER_ID` lock.
- The idea of **routing in code before the LLM**. It's actually the right instinct for a small model. It just needs to be done properly.
- Voice → text → normal pipeline.
- DB config already reads from env (commit `e692d96`).

---

## 4. Target architecture

```
Telegram (python-telegram-bot 22.x, async)
   │
   ├── /commands ───────────────► fast path, NO LLM  (/mc, /docker, /today, /remind)
   │
   └── free text / voice ─► whisper (CPU, lazy) ─► Router
                                                     │ 1. cheap intent classification (LLM, JSON schema, ~1 call)
                                                     ▼
                                      only that category's tools (≤5) ─► LLM tool-call loop (max 3 steps)
                                                     │
                       ┌──────────┬──────────┬───────┴──┬──────────┬──────────┐
                    calendar   reminders    web     homelab     postiz     notion
                                                 (mc + docker)
                                                     │
                         destructive action? ──► Telegram inline [✅ Confirm] [❌ Cancel]
```

**Key decisions**
- **LLM host:** Ollama natively on Windows, version pinned, `OLLAMA_KEEP_ALIVE=15m`, models stored on an SSD drive (`OLLAMA_MODELS`). The bot calls it with `ollama.AsyncClient`.
- **Bot host:** a Docker container (`python:3.12-slim`, `mem_limit: 384m`, `oom_score_adj: 800` so it gets killed before any game server, `restart: unless-stopped`). It reaches Ollama at `host.docker.internal:11434`. This fits your existing stack layout (`E:\DockerData\stack\nhako`).
- **Storage: SQLite** (history, reminders, confirmations, facts) instead of a dedicated Postgres container. You already run 6 Postgres instances, and the VM budget has about 1 GB of slack with all three game servers up. If semantic memory earns its keep later, add `sqlite-vec`.
- **Dates:** the LLM extracts `"next tuesday 3pm"` as text, `dateparser` (Asia/Kuala_Lumpur) turns it into a datetime, and the confirmation message shows the result.
- **Tools as plugins:** each integration is one module with `TOOLS = [...]` and a handler. Adding Notion/Postiz/anything means adding a file, not editing a 200-line `if/elif`.
- **Safety:** web page text gets wrapped as untrusted data. Tools that delete or change things always need a button press, so a malicious web page can't trigger "delete my events".

**Proposed layout**
```
nhako/
  __main__.py        # entry point
  config.py          # pydantic-settings, all env in one place
  bot.py             # telegram handlers, auth, confirm buttons, message splitting
  llm.py             # AsyncClient wrapper, tool loop, JSON-schema calls
  router.py          # intent → tool category
  store.py           # SQLite (aiosqlite)
  speech.py          # faster-whisper, lazy-loaded, run in thread
  tools/
    calendar.py  reminders.py  web.py  minecraft.py  docker.py  postiz.py  notion.py
scripts/google_auth.py   # one-time OAuth consent -> token.json
tests/                   # router + date parsing + tool schemas
Dockerfile  .dockerignore  compose.yaml  pyproject.toml (pinned)
```

---

## 5. Phased plan

### Phase 0: Prove the hardware (≈1 evening) **← do first, it decides everything else**
1. Install Ollama for Windows, then check `ollama ps` shows **100% GPU**.
2. Pull 3–4 candidates that fit in about 2.2 GB, for example `qwen3:4b` / the current Qwen 2–4B, `phi4-mini`, `llama3.2:3b`, a Granite small model. Check the Ollama library for what's current and has the **tools** tag.
3. Write `scripts/bench.py`: ~25 real prompts ("remind me to call mum tomorrow 9am", "who's on getgud", "remove the dentist thing", "what's a good stain remover", Malay/English mixes). Measure **correct tool + correct args %**, tokens/s, and time to first token. Turn off "thinking" mode on models that have it.
4. Pick the winner. Go/no-go gate: ≥90% routing accuracy and <6 s typical reply.

#### Phase 0 results (2026-10-19): **done, winner `qwen3.5:2b`**
- Ollama 0.40.5 installed (winget). Models are in `D:\Ollama\models` (SSD), with `OLLAMA_KEEP_ALIVE=15m` as user env vars.
- GPU: the CUDA 13 build skips the 1050 Ti ("compute capability not in compiled architectures"). **The CUDA 12 build picks it up** (`compute=6.1`, 3.3 GiB available). Don't upgrade Ollama without checking `server.log` for that `inference compute ... library=CUDA` line.
- `python scripts/bench.py`, 30 cases, `num_ctx` 4096 (full JSON in `scripts/bench_results/`):

| model | on GPU | router | tool | args | Malay | median | gen t/s | verdict |
|---|---|---|---|---|---|---|---|---|
| **qwen3.5:2b** | 65% | 93% | 100% | 96% | 100% | **3.2 s** | 21.6 | ✅ **PASS**, use this |
| qwen3:4b | 69% | 96% | 96% | 96% | 100% | 34.1 s | 14.6 | ❌ too slow: it writes ~400 tokens of reasoning before every tool call, and neither `think:false` nor `/no_think` stops it |
| qwen3:1.7b | 100% | 89% | 83% | 70% | 67% | 1.2 s | 38.1 | ❌ fast but sloppy (malformed args like `{"title when": ...}`). Possible "instant" fallback for simple commands |
| llama3.2:3b | 82% | 78% | 91% | 87% | 33% | 1.3 s | 23.0 | ❌ router too weak, bad at Malay |
| phi4-mini | ? | 70% | 0% | 0% | 67% | 5.6 s | 16.8 | ❌ never emits tool calls through Ollama, and timed out once |

- Misses shared by most models: "I'm doing some **research** on homelab backups" gets routed to homelab, and "is nextcloud healthy?" often gets no tool call. Phase 1 should cover these with router prompt examples + a regression test.
- Cold load is 10–60 s the first time after idle (keep-alive expired). The bot should send "typing…" straight away and could pre-warm at 07:00.
- Worth trying later: a non-thinking `qwen3:4b-instruct` tag, if the library has one (it was 96% accurate, it just needs the reasoning gone), and `bonsai-2:4b` (1.6 GB ternary).

### Phase 1: Foundations (rewrite the skeleton, keep the behaviour)
- New `nhako/` package, `config.py`, proper logging, pinned deps, Python 3.12.
- Async-safe everything (`AsyncClient`, `asyncio.to_thread` for whisper/Google).
- Fix the Dockerfile, add `.dockerignore`, `compose.yaml`, and **delete `start_bot.bat`** (Docker restart policy replaces it).
- SQLite store. Migrate or drop old pgvector history (it's chat logs, so dropping is fine).
- Message splitting, `/start`, `/help`, `/ping` (checks Ollama + Google token).
- Tests for router and date parsing.

#### Phase 1 results (2026-10-19): **done, except the Telegram run**
- `SRC/` and `start_bot.bat` are removed. The new `nhako/` package has a router → tool → confirm flow, SQLite (`data/nhako.db`), async Ollama, lazy whisper (voice is downloaded to memory, no temp files), and Telegram HTML formatting + 4096-char splitting.
- Ported: calendar list/create/delete (delete now asks first via buttons), web search, chat. `scripts/google_auth.py` added (the OAuth flow the old README promised). Homelab/Postiz/Notion questions get a "coming in Phase N" reply instead of a made-up answer.
- Tests: 49 unit tests (fake LLM) + 5 live routing tests against qwen3.5:2b, all passing. The router prompt needed two fixes for the live "safety" cases ("remove the dentist thing" was routed to chat, "research on homelab backups" to homelab).
- Docker: `python:3.12-slim`, non-root, allowlist `.dockerignore`. The container reaches Ollama on the host at `host.docker.internal`. **Measured peak 427 MiB**, so the cap is 512m, not the 384m guessed above.
- **Not yet verified:** a real Telegram session and a real Google Calendar (no `.env` / `token.json` on this machine yet).
- Seen in passing: the 2B model's general advice can be wrong (it suggested bleach *and* vinegar for a coffee stain on a white shirt). Fine for commands, so don't treat its chat answers as authoritative.

### Phase 2: Calendar v2 + reminders
- `scripts/google_auth.py` (InstalledAppFlow, saves and refreshes `token.json`).
- Tools: `list_events(range)`, `create_event`, `update_event`, `delete_event`. Fuzzy-match the event, then confirm.
- Reminders: Calendar popup overrides + bot-side JobQueue reminders + optional 8:00 daily agenda push.

#### Phase 2 results (2026-10-19): **done, Telegram test pending**
- **Edit:** `update_event` with a before → after confirmation. "to 5pm" keeps the day, "to friday" keeps the time, and the length is preserved.
- **Matching:** vague names ("the dentist thing", "tomorrow's meeting") are matched in code: the day is split out, filler words dropped, titles scored. If several events match equally well, the bot **asks which one** instead of acting on all of them. Clearing a day ("everything on sunday") still lists every event before Confirm.
- **Reminders:** `set_reminder` stores them in SQLite, and `nhako/scheduler.py` checks every 20 s. They survive restarts, and anything missed while the bot was down is sent marked "late". `/reminders` lists them with tap-to-cancel buttons. A reminder with no time defaults to 09:00.
- **Agenda:** `AGENDA_TIME=08:00` sends today's events each morning, at most once a day, skipped if the bot starts more than 3 h late. `/today [range]` shows the same list on demand. No model is involved, so it's instant even when the model is unloaded.
- Google's own popup reminders stay on each event's default (`useDefault`). Per-event "N minutes before" nudges from the bot were left out because Google already does that.
- Tests: 79 unit tests (fake calendar, scheduler ticks with a fake clock) + 9 live tests. The new live test checks that qwen3.5:2b picks the right one of the 5 schedule tools (≥85%), and that move/delete are never confused.

- **Voice fix (2026-10-19):** voice notes failed silently in the container. The Docker build pulled PyAV 19.0.1 (unpinned, a dependency of faster-whisper), which removed an `av.open()` option faster-whisper 1.2.1 uses. Fixed by pinning `av==18.1.0`, catching transcription errors, and adding a global error handler so nothing fails silently again. `tests/test_speech.py` decodes a real Telegram-style OGG/Opus clip and fails on PyAV 19. The Phase 1 smoke test had missed this because it fed whisper raw samples, which skips the decoder.
- **Lesson:** `requirements.txt` pins only direct dependencies, so Docker can resolve newer transitive ones than the tested venv. Consider a full lock (`pip freeze` → `requirements.lock`) before Phase 3.

### Phase 3: Web
- `web_search` (ddgs) and `read_url` (httpx + trafilatura, truncated to fit context). Answers cite links.

#### Phase 3 results (2026-10-19): **done, Telegram test pending**
- **`read_url`**: httpx + trafilatura pull out the main text (no menus or footers). Limits: 2 MB, 15 s, web pages only (PDFs and files are refused), ≤5 redirects. If the page is longer than ~7000 chars (what fits in `num_ctx` 4096), the paragraphs matching the user's question are kept, plus the opening one.
- **Safety:** every hop, including redirects, is resolved and refused if it lands on a private, loopback or link-local address. So a web page or a prompt-injected link can't make the bot read Portainer, NPM or the router admin page. Page text reaches the model marked as untrusted data. Known gap: DNS rebinding between the check and the fetch isn't covered (acceptable for a single-user bot).
- **Sources:** search answers end with the top 3 links, and page answers with 🔗 URL. These are added by code, never written by the model. Follow-ups like "open the first link" work through the last-exchange history (live-tested).
- Telegram formatting now also turns Markdown bullets/headings into • and bold.
- **Locked Docker deps:** `requirements.lock` (73 packages) was generated inside the Linux image after all 100 unit tests passed there. The Dockerfile installs from it. `requirements.txt` stays the human-edited list.
- Measured in the container: page summary ~12–16 s, search + summary ~16 s (prompt processing of the page text plus a ~200-token answer at ~21 tok/s).
- Known weakness: index pages (news lists, homepages) sometimes extract the wrong block. Article-style pages work well.

### Phase 4: Homelab
- `/mc` + natural language: `mcstatus` ping to 25565, RCON `list` for full names (password from env, read-only commands only).
- `/docker`: a dedicated read-only socket proxy (`CONTAINERS=1`, all else 0). Show stacks grouped by compose project, health, uptime.
- Push alerts: container went unhealthy/exited. MC server down.

#### Phase 4 results (2026-10-19): **done, Telegram test pending**
- **Minecraft:** a server-list ping (`mcstatus`) gives the online count and player names. **No RCON password needed**: the ping lists up to ~12 names, and anyone beyond that shows as "and N more". `MC_SERVERS` in compose: getgud on :25565, kimzamwrld on :25566 (loopback-only on the host, reached via `host.docker.internal`). The kimzamwrld path is untested because that server was stopped.
- **Docker:** a dedicated `nhako-docker-proxy` (same image as homepage's, pinned by digest) allows **GET /containers only**. Verified: POST stop → 403, images → 403, exec → 403. It sits on an `internal` network that only the bot can reach. Overview = one line per compose stack. A named stack/container also gets each container's RAM.
- **Answers are built by code, not the model:** instant, and they can't be made up. If the model makes no tool call for a homelab question, a keyword fallback calls the right tool instead of letting the model answer from chat (that would invent a status).
- **Alerts** (checked every 60 s, state in SQLite): a container turning unhealthy, restarting, or exiting with a crash code (anything except 0/130/143, so `docker stop` is silent). getgud gets an alert after 2 missed pings. Recoveries are reported too, and a bot restart doesn't repeat old alerts. `ALERT_IGNORE`, `MC_ALERT_SERVERS`, `ALERTS=false` in `.env`.
- First check on deploy flagged **portainer (exit 2)** and **kimzam-backup (exit 255)**. Both stopped around the same time as other containers (homepage 143, kimzam-server 0), so possibly a manual shutdown rather than crashes. Add them to `ALERT_IGNORE` if that's expected.
- `/mc [server]` and `/docker [name]` skip the model entirely. Proxy uses 24 MiB, bot 74 MiB idle.
- Valheim status beyond "container up/healthy" (player count via Steam A2S query) was left out.

### Phase 5: Integrations
- Postiz (API key): "what's scheduled this week", "draft a post for X".
- Notion (integration token): "add to my tasks", "what's due".
- Optional: an MCP client adapter so community MCP servers can be added as a tool category.

**Results (2026-10-19):**
- **Notion** ([nhako/tools/notion.py](../nhako/tools/notion.py)): `list_tasks` (open, due today / this week / overdue), `add_task` (with a due date parsed in code), `complete_task` (fuzzy name match, asks *which one* if several match, Confirm button before changing anything). Uses API version `2025-09-03` (data sources). The columns are read from the database, so any title + date + status/checkbox database works. Built against the user's "My Tasks" template (Task name / Due / Status / Assignee). New tasks are assigned to the only person in the workspace, so they appear in the template's "assigned to me" view.
- **Postiz** ([nhako/tools/postiz.py](../nhako/tools/postiz.py)): checked against the running Postiz's own source (`public.integrations.controller.ts`). `list_posts` (any period, ⏳ scheduled / ✅ published / 📝 draft / ❌ failed) and `draft_post`: the model writes the post, the bot shows it with a Confirm button, and it is saved as a **draft only** (the API key is org-wide, so the bot never schedules or deletes). A per-post `"type": "draft"` makes Postiz skip each network's settings checks, which drafts don't need. Reached through the public URL (`https://postiz.nhako.com/api`), which works from inside containers.
- `/tasks [today|this week|overdue]`, and the 08:00 agenda now adds tasks due today or overdue. `/ping` checks both.
- Router: "is postiz running?" first routed to the posts category, which was wrong. Fixed with a description note plus an example. The live router tests passed 11/11 on three runs in a row, and tool choice passed 6/6 (with 5 new Phase 5 cases).
- 163 unit tests.
- **Live check (from inside the container):** Postiz works. It found 3 channels (TikTok "nhako", Instagram "Nhako", Threads "thenhako") and listed 190 scheduled posts for the month in local time. Notion: the token is valid, but the "My Tasks" database isn't shared with the "NhakoBot" integration yet (404 `object_not_found`). Add it in Notion under ••• → Connections.
- MCP adapter: not done (not needed for these two).

### Phase 6: Memory (only if you miss it)
- A "facts" table ("I prefer meetings after 10am"), saved via a `remember` tool. Optional `sqlite-vec` search.

**Results (2026-10-19):**
- `facts` table in `data/nhako.db`, plus a `memory` route with `remember`, `forget` (asks first) and `list_memories`. `/memory` lists the facts with a 🗑 button each. Facts are saved only when the user asks: at most 50, deduplicated, one sentence each. Passwords, PINs, keys and IC/card numbers are refused (phone numbers are allowed).
- **Facts go into the system prompt for conversation only.** In the live tests, ten facts in the prompt made the model list events instead of deleting or moving one. Even one related fact ("prefers meetings after 10am" with "push tomorrow's meeting to 4pm") did the same. So the tool-choosing prompt never includes facts. Conversation does: "can I eat prawn mee?" → "No… you have an allergy to prawns".
- **No `sqlite-vec`:** a few dozen facts fit in a 1,500-character budget, and the newest are kept if it overflows. A vector search would add an embedding-model swap on 4 GB of VRAM and a chance of missing a fact.
- **The model's arguments are checked against what the user said.** The 2B model sometimes copied the previous fact from the history, or the example in a tool description. It saved "prefers meetings after 10am, allergy to prawns" when told only about prawns, and set "remind me … at 6pm" as `in 20 minutes`. Now `remember` keeps the user's own words when fewer than half of the fact's words came from the message. `forget` does the same check. Reminders and new events use `pick_when`: if the model's time isn't in the message, the time is taken from the message itself.
- **Fixed a Phase 2 bug found while testing:** for a bare clock time ("9am", "6pm", "3 petang"), dateparser decided "the future" against the real clock rather than `RELATIVE_BASE`. So "6pm" asked at 15:10 became *tomorrow*, and the unit tests only passed because of the time of day they ran. A bare time now starts from today and moves to tomorrow only if it has already passed. Tests now cover 07:10, 15:10, 19:10 and 23:10.
- 208 unit tests and 23 live tests pass. The container run was checked end to end with a throwaway database.

---

## 6. Decisions I need from you
1. ~~SQLite vs Postgres~~: **SQLite** (decided 2026-10-19).
2. ~~Docker vs native~~: **bot in Docker**, Ollama native (decided 2026-10-19).
3. **Docker write actions** (restart a container from Telegram): leave out for now?
4. **Language:** should it understand Malay/English mixes? This affects model choice in Phase 0 and the whisper language setting.
