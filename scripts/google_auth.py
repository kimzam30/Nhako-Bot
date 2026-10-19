"""One-time Google Calendar consent. Run on a machine with a browser:

    python scripts/google_auth.py

Needs data/credentials.json (Google Cloud console -> APIs & Services -> Credentials ->
OAuth client ID -> Desktop app). Writes data/token.json, which the bot refreshes itself.
"""
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nhako.config import get_settings  # noqa: E402
from nhako.tools.calendar import SCOPES  # noqa: E402


def main() -> None:
    s = get_settings()
    if not s.google_credentials_path.exists():
        sys.exit(f"Put your OAuth client file at {s.google_credentials_path} first.")
    creds = InstalledAppFlow.from_client_secrets_file(str(s.google_credentials_path), SCOPES).run_local_server(port=0)
    s.google_token_path.write_text(creds.to_json(), encoding="utf-8")
    print(f"Saved {s.google_token_path}")


if __name__ == "__main__":
    main()
