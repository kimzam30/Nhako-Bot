"""python -m nhako"""
import logging
import sys

from .bot import build_app
from .config import get_settings


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    for noisy in ("httpx", "primp", "ddgs"):  # one line per Telegram poll / search engine otherwise
        logging.getLogger(noisy).setLevel(logging.WARNING)

    settings = get_settings()
    if not settings.telegram_token or not settings.allowed_user_id:
        sys.exit("TELEGRAM_TOKEN and ALLOWED_USER_ID must be set (see .env.example).")
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    logging.getLogger(__name__).info("Nhako starting on %s via %s", settings.ollama_model, settings.ollama_host)
    build_app(settings).run_polling()  # messages sent while the bot was down are answered, not dropped


if __name__ == "__main__":
    main()
