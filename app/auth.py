from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import SessionRow, get_session
from app.settings import ADMIN_EMAIL, get_settings

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
LOGIN_SCOPES = "openid email profile"
SESSION_COOKIE = "linh_news_session"


def load_allowlist(path: Path | None = None) -> set[str]:
    p = path or get_settings().users_file
    if not p.exists():
        return set()
    out: set[str] = set()
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line.lower())
    return out


def is_allowed(email: str, path: Path | None = None) -> bool:
    return email.lower() in load_allowlist(path)


def is_admin(email: str) -> bool:
    return email.lower() == ADMIN_EMAIL.lower()


def login_url(state: str) -> str:
    s = get_settings()
    params = {
        "client_id": s.google_client_id,
        "redirect_uri": f"{s.public_base_url}/auth/callback",
        "response_type": "code",
        "scope": LOGIN_SCOPES,
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_email(code: str) -> str:
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
        access_token = token_resp.json()["access_token"]
        info = await c.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        info.raise_for_status()
        data = info.json()
    if not data.get("email_verified"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Email not verified")
    return data["email"].lower()


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
    if not email or not is_allowed(email):
        if request.headers.get("accept", "").startswith("text/html"):
            raise HTTPException(
                status.HTTP_307_TEMPORARY_REDIRECT,
                headers={"Location": "/login"},
            )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return email


def require_admin(request: Request, session: Session = Depends(get_session)) -> str:
    email = _current_email(request, session)
    if not email or not is_allowed(email):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    if not is_admin(email):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return email


def redirect_to_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)
