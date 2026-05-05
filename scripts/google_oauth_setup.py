"""One-time local script: mint a Google Calendar refresh token and persist it.

Run from the repo root:

    uv run python scripts/google_oauth_setup.py

You will be sent through the standard installed-app OAuth flow (consent
screen in your browser). The resulting refresh token, plus the client_id /
client_secret, is written to the `google_oauth` table so the production
server can fetch calendar events without any further interactive consent.
"""

from __future__ import annotations

import os

from google_auth_oauthlib.flow import InstalledAppFlow
from sqlalchemy import delete

from app.db import GoogleOAuth, session_factory
from app.settings import get_settings

CALENDAR_SCOPE = ["https://www.googleapis.com/auth/calendar.readonly"]

# Fixed local port so the redirect URI is stable. Register
# http://localhost:8765/ as an Authorized redirect URI on the OAuth client.
OAUTH_LOCAL_PORT = 53129


def main() -> None:
    s = get_settings()
    if not s.google_client_id or not s.google_client_secret:
        raise SystemExit("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set")

    client_config = {
        "installed": {
            "client_id": s.google_client_id,
            "client_secret": s.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [f"http://localhost:{OAUTH_LOCAL_PORT}/"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, CALENDAR_SCOPE)
    creds = flow.run_local_server(port=OAUTH_LOCAL_PORT, prompt="consent", access_type="offline")
    if not creds.refresh_token:
        raise SystemExit("No refresh token returned. Re-run with prompt=consent.")

    from datetime import UTC, datetime

    admin_email = s.admin_email.lower()
    Maker = session_factory()
    with Maker() as db:
        db.execute(delete(GoogleOAuth).where(GoogleOAuth.email == admin_email))
        db.add(
            GoogleOAuth(
                email=admin_email,
                refresh_token=creds.refresh_token,
                client_id=s.google_client_id,
                client_secret=s.google_client_secret,
                personalized_enabled=True,
                created_at=datetime.now(UTC),
            )
        )
        db.commit()
    print(f"Stored refresh token in google_oauth (email={admin_email}).")


if __name__ == "__main__":
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    main()
