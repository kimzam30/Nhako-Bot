# 🤖 NhakoBot

![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-pgvector-blue.svg)
![Ollama](https://img.shields.io/badge/Ollama-Llama_3.2-black.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

NhakoBot is a fully localized, multimodal AI Telegram assistant. Built with privacy and local execution in mind, it leverages Llama 3.2 for natural language processing, PostgreSQL for long-term vector memory, and offline acoustic models for voice recognition. 

It acts as a personal homelab assistant capable of managing schedules, remembering past context, and searching the live internet.

## ✨ Core Features

* 🧠 **Local LLM Engine:** Powered by Llama 3.2 (via Ollama) for zero-latency, private, and cost-free text generation.
* 🗄️ **Long-Term Memory (RAG):** Uses a PostgreSQL database with the `pgvector` extension to store and semantically search past chat context.
* 🎙️ **Voice Transcription:** Intercepts Telegram voice notes and transcribes them entirely offline using `faster-whisper` (CPU-optimized `int8`).
* 🌐 **Live Web Search:** Bypasses standard LLM training cutoffs by silently querying DuckDuckGo to inject real-time facts into conversations.
* 📅 **Google Calendar API:** Autonomously reads, creates, and deletes schedule events directly from natural language prompts.
* 👻 **Invisible Background Service:** Configured to run silently as a headless Windows background process on boot.

## 🏗️ Architecture


The bot uses a custom Python routing system (built on `python-telegram-bot`) to intercept messages and route them to specific internal tools before passing the context to the LLM. 

## 🚀 Installation & Setup

### Prerequisites
* Python 3.11+
* Docker (for the PostgreSQL database)
* Ollama (running `llama3.2`)
* A Telegram Bot Token (from BotFather)

### 1. Clone & Environment
```bash
git clone [https://github.com/yourusername/Nhako-Bot.git](https://github.com/yourusername/Nhako-Bot.git)
cd Nhako-Bot
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt