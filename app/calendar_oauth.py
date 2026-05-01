from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import GoogleOAuth

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"


def _credentials(s: Session) -> Credentials:
    row = s.execute(select(GoogleOAuth).limit(1)).scalar_one_or_none()
    if row is None:
        raise RuntimeError(
            "No Google OAuth row. Run scripts/google_oauth_setup.py first."
        )
    creds = Credentials(
        token=None,
        refresh_token=row.refresh_token,
        client_id=row.client_id,
        client_secret=row.client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[CALENDAR_SCOPE],
    )
    creds.refresh(GoogleRequest())
    return creds


def _service(s: Session):
    return build("calendar", "v3", credentials=_credentials(s), cache_discovery=False)


def list_calendars(s: Session) -> list[dict]:
    svc = _service(s)
    page_token = None
    out: list[dict] = []
    while True:
        resp = svc.calendarList().list(pageToken=page_token).execute()
        for item in resp.get("items", []):
            out.append(
                {
                    "id": item["id"],
                    "name": item.get("summaryOverride") or item.get("summary") or item["id"],
                    "primary": bool(item.get("primary")),
                }
            )
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def fetch_events(
    s: Session,
    calendar_ids: list[str],
    start: date,
    end: date,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Fetch events from given calendars in [start, end). Drops events that
    have already ended (relative to ``now``)."""
    svc = _service(s)
    now = now or datetime.now(UTC)
    time_min = datetime.combine(start, time.min, tzinfo=UTC).isoformat()
    time_max = datetime.combine(end, time.min, tzinfo=UTC).isoformat()

    out: list[dict] = []
    for cal_id in calendar_ids:
        page_token = None
        while True:
            resp = svc.events().list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                pageToken=page_token,
                maxResults=2500,
            ).execute()
            for ev in resp.get("items", []):
                if ev.get("status") == "cancelled":
                    continue
                start_d, all_day = _event_start(ev)
                end_d = _event_end(ev)
                if end_d and end_d < now:
                    continue
                out.append(
                    {
                        "calendar_id": cal_id,
                        "summary": ev.get("summary", "(no title)"),
                        "start": start_d.isoformat() if start_d else None,
                        "end": end_d.isoformat() if end_d else None,
                        "all_day": all_day,
                        "location": ev.get("location"),
                        "description": ev.get("description"),
                    }
                )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    return out


def _event_start(ev: dict) -> tuple[datetime | None, bool]:
    s = ev.get("start", {})
    if "dateTime" in s:
        return datetime.fromisoformat(s["dateTime"].replace("Z", "+00:00")), False
    if "date" in s:
        return datetime.fromisoformat(s["date"]).replace(tzinfo=UTC), True
    return None, False


def _event_end(ev: dict) -> datetime | None:
    e = ev.get("end", {})
    if "dateTime" in e:
        return datetime.fromisoformat(e["dateTime"].replace("Z", "+00:00"))
    if "date" in e:
        # all-day end is exclusive; treat as end-of-day before
        d = datetime.fromisoformat(e["date"]).replace(tzinfo=UTC)
        return d - timedelta(seconds=1)
    return None
