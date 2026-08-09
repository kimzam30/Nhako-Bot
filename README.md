# Nhako Bot

A self-hosted Telegram assistant that runs entirely on your own hardware — local LLM, local vector memory, local speech-to-text. Nothing is sent to a hosted AI provider.

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL_+_pgvector-4169E1?style=flat-square&logo=postgresql&logoColor=white)
![Ollama](https://img.shields.io/badge/Ollama_·_Llama_3.2-000000?style=flat-square&logo=ollama&logoColor=white)
![License](https://img.shields.io/github/license/kimzam30/Nhako-Bot?style=flat-square)

---

## What it does

| Capability | How |
|---|---|
| **Chat** | Llama 3.2 through Ollama — private, no API bill, no rate limit |
| **Long-term memory** | Conversations embedded and stored in PostgreSQL with `pgvector`; relevant history is retrieved and injected into the prompt (RAG) |
| **Voice notes** | Telegram voice messages transcribed offline with `faster-whisper` (CPU-optimised `int8`) |
| **Live web search** | DuckDuckGo queried on demand so answers aren't capped at the model's training cutoff |
| **Calendar** | Reads, creates, and deletes Google Calendar events from natural language |

A small router in `SRC/main.py` inspects each incoming message and dispatches to the right tool before handing context to the model, rather than relying on the LLM to choose.

---

## Layout

```
Nhako-Bot/
├── SRC/
│   ├── main.py           # Telegram handlers and the message router
│   ├── brain.py          # Ollama chat completion
│   ├── memory.py         # pgvector storage and semantic retrieval
│   ├── ears.py           # faster-whisper voice transcription
│   ├── search.py         # DuckDuckGo lookup
│   ├── my_calendar.py    # Google Calendar integration
│   └── check_ollama.py   # Startup health check
├── Dockerfile
├── requirements.txt
├── .env.example
└── start_bot.bat         # Windows launcher
```

---

## Setup

### Prerequisites

- Python 3.11+
- [Ollama](https://ollama.com) with the model pulled: `ollama pull llama3.2`
- PostgreSQL with the `pgvector` extension (Docker is the easiest route)
- A bot token from [@BotFather](https://t.me/botfather)
- A Google Cloud OAuth client (`credentials.json`) if you want calendar access

### 1. Install

```bash
git clone https://github.com/kimzam30/Nhako-Bot.git
cd Nhako-Bot
python -m venv .venv
source .venv/bin/activate      # Windows: .\.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Database

```bash
docker run -d --name nhako-db \
  -e POSTGRES_PASSWORD=change-me \
  -p 5432:5432 \
  pgvector/pgvector:pg16
```

Then enable the extension:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

### 3. Configure

```bash
cp .env.example .env
```

Set your bot token and database connection string. For calendar support, drop `credentials.json` in the project root — the first run opens a browser for consent and caches a token.

### 4. Run

```bash
python SRC/main.py
```

> The source directory is `SRC/` in capitals. On Linux and macOS the path is case-sensitive, so `python src/main.py` will not work.

### Docker

A `Dockerfile` is included if you'd rather run the bot as a container alongside the database.

---

## Roadmap

- [ ] URL scraping with automatic metadata tagging
- [ ] Vision support via `llama3.2-vision`
- [ ] Homelab monitoring commands (container status, disk, uptime)

---

## License

MIT — see [LICENSE](LICENSE).

Built as a homelab project by **kimzam**.
