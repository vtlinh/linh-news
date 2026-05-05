from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionRow, get_session
from app.settings import get_settings

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
# Login also requests calendar.readonly so cron can fetch each user's
# Google Calendar without a second consent dance.
LOGIN_SCOPES = (
    "openid email profile https://www.googleapis.com/auth/calendar.readonly"
)
SESSION_COOKIE = "linh_news_session"


def load_allowlist(session: Session) -> set[str]:
    """Authorized emails — every row in ``user_settings`` plus the admin
    (always allowed even if their row is missing, so a stray DELETE on
    the Users page can't lock the admin out)."""
    from app.db import UserSettings

    rows = session.execute(select(UserSettings.email)).scalars().all()
    out = {(e or "").lower() for e in rows if e}
    out.add(get_settings().admin_email.lower())
    out.discard("")
    return out


def is_allowed(email: str, session: Session) -> bool:
    return email.lower() in load_allowlist(session)


def is_admin(email: str) -> bool:
    return email.lower() == get_settings().admin_email.lower()


def login_url(state: str) -> str:
    s = get_settings()
    params = {
        "client_id": s.google_client_id,
        "redirect_uri": f"{s.public_base_url}/auth/callback",
        "response_type": "code",
        "scope": LOGIN_SCOPES,
        "state": state,
        # Offline + consent so Google emits a refresh_token even on
        # re-consent — without prompt=consent it only comes back the
        # very first time a user authorizes the app.
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_email(code: str) -> tuple[str, str | None, dict]:
    """Exchange the authorization code for ``(email, refresh_token, profile)``.

    ``refresh_token`` may be ``None`` when Google chooses not to emit one
    on this round-trip (typically after a consent skip). Callers should
    keep any previously-stored refresh token in that case rather than
    overwriting it with NULL.

    ``profile`` is the raw userinfo payload — useful keys: ``given_name``,
    ``family_name``, ``name``, ``picture``.
    """
    s = get_settings()
    async with httpx.AsyncClient(timeout=15) as c:
        token_resp = await c.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": s.google_client_id,
                "client_secret": s.google_client_secret,
                "redirect_uri": f"{s.public_base_url}/auth/callback",
                "grant_type": "authorization_code",
            },
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()
        access_token = token_data["access_token"]
        refresh_token = token_data.get("refresh_token")
        info = await c.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        info.raise_for_status()
        data = info.json()
    if not data.get("email_verified"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Email not verified")
    return data["email"].lower(), refresh_token, data


def create_session(s: Session, email: str) -> str:
    sid = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(days=get_settings().session_ttl_days)
    s.add(SessionRow(id=sid, email=email, expires_at=expires))
    s.commit()
    return sid


def delete_session(s: Session, sid: str) -> None:
    row = s.get(SessionRow, sid)
    if row:
        s.delete(row)
        s.commit()


def _current_email(request: Request, session: Session) -> str | None:
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return None
    row = session.get(SessionRow, sid)
    if not row:
        return None
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
    if expires < datetime.now(UTC):
        session.delete(row)
        session.commit()
        return None
    return row.email.lower()


def require_viewer(request: Request, session: Session = Depends(get_session)) -> str:
    email = _current_email(request, session)
    if not email or not is_allowed(email, session):
        if request.headers.get("accept", "").startswith("text/html"):
            raise HTTPException(
                status.HTTP_307_TEMPORARY_REDIRECT,
                headers={"Location": "/login"},
            )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    # Browser viewers whose session predates the calendar.readonly scope
    # have no google_oauth row — bounce them to /login so the consent
    # dance captures a refresh token before they get back to /.
    if request.headers.get("accept", "").startswith("text/html"):
        from app.db import GoogleOAuth

        has_creds = session.execute(
            select(GoogleOAuth.email).where(GoogleOAuth.email == email)
        ).scalar_one_or_none()
        if not has_creds:
            sid = request.cookies.get(SESSION_COOKIE)
            if sid:
                row = session.get(SessionRow, sid)
                if row:
                    session.delete(row)
                    session.commit()
            raise HTTPException(
                status.HTTP_307_TEMPORARY_REDIRECT,
                headers={"Location": "/login?error=needs_reconsent"},
            )
    return email


def require_admin(request: Request, session: Session = Depends(get_session)) -> str:
    email = _current_email(request, session)
    if not email or not is_allowed(email, session):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    if not is_admin(email):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return email


def redirect_to_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)
