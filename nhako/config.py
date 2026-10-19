"""All settings in one place, read from the environment / .env."""
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    telegram_token: str = ""
    allowed_user_id: int = 0

    # Ollama. The model was picked by scripts/bench.py (docs/REVIVAL_PLAN.md, Phase 0).
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3.5:2b"
    ollama_num_ctx: int = 4096

    timezone: str = "Asia/Kuala_Lumpur"
    agenda_time: str = "08:00"  # morning agenda message; "off" to disable

    # Everything the bot writes lives under data_dir (a Docker volume in compose.yaml).
    data_dir: Path = Path("data")
    google_credentials_file: str = "credentials.json"
    google_token_file: str = "token.json"

    # Homelab (Phase 4). Names are what you call the servers in chat.
    mc_servers: str = "getgud=127.0.0.1:25565"
    docker_proxy_url: str = ""          # read-only docker-socket-proxy, set in compose.yaml
    alerts: bool = True                 # push a message when a container/server breaks
    alert_ignore: str = ""              # comma-separated container names to never alert on
    mc_alert_servers: str = "getgud"    # servers that should always be up

    # Integrations (Phase 5). Unset: the bot says how to set them up.
    notion_token: str = ""              # internal integration secret (ntn_...)
    notion_tasks_db: str = ""           # tasks database id or URL, shared with the integration
    postiz_url: str = ""                # backend URL, e.g. https://postiz.example.com/api
    postiz_api_key: str = ""            # Postiz → Settings → Public API

    whisper_model: str = "base"
    history_turns: int = 6  # user+assistant messages kept as short-term context for chat

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "nhako.db"

    @property
    def google_credentials_path(self) -> Path:
        return self.data_dir / self.google_credentials_file

    @property
    def google_token_path(self) -> Path:
        return self.data_dir / self.google_token_file


@lru_cache
def get_settings() -> Settings:
    return Settings()
