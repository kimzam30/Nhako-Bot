"""All settings in one place, read from the environment / .env."""
import datetime as dt
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

OFF = ("", "off", "none", "no", "false")


def parse_agenda_time(text: str) -> dt.time | None:
    """'08:00' -> time(8, 0); '' or 'off' disables the morning agenda."""
    if (text or "").strip().lower() in OFF:
        return None
    hour, _, minute = text.strip().partition(":")
    return dt.time(int(hour), int(minute or 0))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    telegram_token: str = ""
    allowed_user_id: int = 0

    # Ollama. The model was picked by scripts/bench.py (docs/REVIVAL_PLAN.md, Phase 0).
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3.5:2b"
    ollama_num_ctx: int = 4096
    ollama_timeout: float = 120         # seconds before a model call is given up
    ollama_keep_alive: str = "30m"      # how long the model stays in VRAM after a message

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
    history_turns: int = 6       # user+assistant messages kept as short-term context for chat
    history_keep: int = 200      # messages kept per chat in the database; older ones are pruned daily

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone {v!r}, use an IANA name like Asia/Kuala_Lumpur") from None
        return v

    @field_validator("agenda_time")
    @classmethod
    def _check_agenda_time(cls, v: str) -> str:
        try:
            parse_agenda_time(v)
        except ValueError:
            raise ValueError(f"AGENDA_TIME must be HH:MM (24h) or 'off', not {v!r}") from None
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def agenda_at(self) -> dt.time | None:
        return parse_agenda_time(self.agenda_time)

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
