# Nhako Bot

A self-hosted Telegram assistant that runs on your own hardware. It uses a local LLM through Ollama, local speech-to-text and a local SQLite database. Nothing goes to a hosted AI provider.

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)
![Ollama](https://img.shields.io/badge/Ollama_·_qwen3.5:2b-000000?style=flat-square&logo=ollama&logoColor=white)
![License](https://img.shields.io/github/license/kimzam30/Nhako-Bot?style=flat-square)

> **v2 rewrite in progress.** The roadmap, the hardware notes and the reasons behind each choice are in [docs/REVIVAL_PLAN.md](docs/REVIVAL_PLAN.md).

---

## What it does

| Capability | How |
|---|---|
| **Chat** | `qwen3.5:2b` on Ollama, picked by benchmarking on a GTX 1050 Ti ([scripts/bench.py](scripts/bench.py)) |
| **Calendar** | List, add, move, rename and delete Google Calendar events in plain English or Malay. Changes and deletions always ask first (Confirm / Cancel buttons), and vague names ("the dentist thing") ask *which one* if several match |
| **Reminders** | "Remind me in 20 minutes to…": the bot messages you on Telegram. They survive restarts. `/reminders` to cancel |
| **Morning agenda** | Today's events at 08:00 (`AGENDA_TIME`). `/today`, `/today tomorrow` on demand |
| **Web** | DuckDuckGo search, summarised with source links. Paste a link (or say "open the second link") to have a page read and summarised. Local-network addresses are refused |
| **Minecraft** | "Who's on getgud?": player names and counts from a server-list ping. `/mc` |
| **Docker** | "How are my stacks doing?": per-stack status through a read-only socket proxy. `/docker [name]` |
| **Notion tasks** | "Add renew passport to my tasks by friday", "what's due this week?", "I finished the invoice" (asks first). `/tasks`. Due tasks are in the morning agenda |
| **Postiz** | "What's going out on social media this week?", "draft a tweet about the server wipe": saved as a **draft** after you confirm, never published by the bot |
| **Memory** | "Remember that I'm allergic to prawns", "what do you know about me?", "forget the prawn thing" (asks first). Used in conversation. `/memory` shows the facts with a button to remove each. Passwords and card/IC numbers are refused |
| **Homelab alerts** | A message when a container crashes or turns unhealthy, or getgud stops answering, and again when it recovers |
| **Voice notes** | Transcribed offline with `faster-whisper` on CPU |

### How a message is handled

1. **Route.** One quick model call puts the message into a category (`schedule`, `web`, `homelab`, `notion`, `postiz`, `memory`, `chat`).
2. **Tool call.** The model sees *only* that category's tools and picks one with arguments. Times stay in the user's words ("next tuesday 3pm").
3. **Run.** Code does the real work, including all date maths ([nhako/timeparse.py](nhako/timeparse.py)). Anything destructive returns a confirmation instead of acting.

Small models stay accurate when they choose from a few tools at a time, and they never do date arithmetic.

---

## Layout

```
nhako/
  __main__.py      # python -m nhako
  bot.py           # Telegram: auth, commands, typing indicator, confirm buttons
  agent.py         # route -> tool call -> run -> reply
  router.py        # category classification (JSON-schema output)
  llm.py           # async Ollama client
  timeparse.py     # "esok 3 petang" -> datetime
  store.py         # SQLite: history, pending confirmations, reminders, facts
  speech.py        # faster-whisper, lazy-loaded
  config.py        # all settings, from .env
  scheduler.py     # reminders + morning agenda loop
  monitor.py       # homelab alerts (changes only)
  tools/           # one module per integration: calendar, reminders, web, homelab, notion, postiz, memory
scripts/
  bench.py         # model benchmark (Phase 0)
  google_auth.py   # one-time Google consent -> data/token.json
tests/
```

---

## Setup

### 1. Ollama (on the host, with the GPU)

```bash
winget install --id Ollama.Ollama --exact
ollama pull qwen3.5:2b
```

On Pascal cards (GTX 10xx), check that `%LOCALAPPDATA%\Ollama\server.log` has an `inference compute ... library=CUDA` line. Ollama's CUDA 13 build has dropped these cards, and only the CUDA 12 build still supports them.

### 2. Configure

```bash
cp .env.example .env
```

Set `TELEGRAM_TOKEN` (from [@BotFather](https://t.me/botfather)) and `ALLOWED_USER_ID` (your numeric id; everyone else is ignored).

### 3. Google Calendar (optional)

Create a *Desktop app* OAuth client in Google Cloud and save it as `data/credentials.json`. Then run this once on a machine with a browser:

```bash
python scripts/google_auth.py
```

It writes `data/token.json`, and the bot refreshes the token itself from then on.

### 4. Notion and Postiz (optional)

- **Notion:** create an internal integration at notion.so/profile/integrations and put its secret in `NOTION_TOKEN`. Put your tasks database's URL in `NOTION_TASKS_DB`. On the database page, open ••• → Connections and add the integration.
- **Postiz:** put the API key from Postiz → Settings → Public API in `POSTIZ_API_KEY`, and your backend URL (ending in `/api`) in `POSTIZ_URL`.

### 5. Run

**Docker (recommended):**

```bash
docker compose up -d --build
```

The container reaches Ollama at `host.docker.internal:11434`. It's capped at 512 MB (it peaks at ~430 MiB with whisper loaded) and is the first thing the kernel kills if memory runs out.

**Locally:**

```bash
python -m venv .venv
.venv/Scripts/activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
python -m nhako
```

### Tests

```bash
pytest -m "not live"              # unit tests, no model needed
pytest -m live                    # routing against the real local model
```

In Telegram, `/ping` checks the model and the calendar token.

---

## Roadmap

See [docs/REVIVAL_PLAN.md](docs/REVIVAL_PLAN.md). Phases 0–6 are done.

---

## License

MIT, see [LICENSE](LICENSE).

Built as a homelab project by **kimzam**.
