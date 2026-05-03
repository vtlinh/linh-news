from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import GoogleOAuth

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"

# Per-calendar event-title regex filters. Matching events are dropped before
# they reach the events page or the generation pipeline. Calendar names are
# matched case-insensitively as substrings.
EVENT_TITLE_FILTERS: list[tuple[str, re.Pattern[str]]] = [
    # Dorchester Parent Calendar uses "Day 1" .. "Day 6" markers — noise.
    ("dorchester parent calendar", re.compile(r"^\s*day\s*[1-6]\s*$", re.IGNORECASE)),
]


def is_filtered(calendar_name: str, title: str) -> bool:
    cn = (calendar_name or "").lower()
    for needle, pattern in EVENT_TITLE_FILTERS:
        if needle in cn and pattern.search(title or ""):
            return True
    return "dorchester parent calendar" in cn and _is_other_grade(title or "")


# ── Kid-grade filter ──────────────────────────────────────────────────────
# Anchor: William starts 2nd grade in academic year 2025–2026.
# School year transitions on August 1: anyone reading the events on/after
# August 1 of a given calendar year is in the academic year that starts that
# month. The grade auto-advances by one each August.
KID_GRADE_ANCHOR_YEAR = 2025  # academic year start when kids are GRADE_AT_ANCHOR
GRADE_AT_ANCHOR = 2

# Recognises common ways school events label a grade in the title:
#   "Grade 3", "3rd Grade", "5th-Grade", "Kindergarten"
_GRADE_RE = re.compile(
    r"(?:\bgrade\s*(\d+)\b|\b(\d+)(?:st|nd|rd|th)\s*[-–]?\s*grade\b|\b(kindergarten|kinder)\b)",
    re.IGNORECASE,
)


def current_kid_grade(today: date | None = None) -> int:
    """Return the grade William and Elizabeth are currently in. Auto-advances
    every August when a new school year begins."""
    if today is None:
        from app.settings import local_today as _local_today
        today = _local_today()
    school_year_start = today.year if today.month >= 8 else today.year - 1
    return GRADE_AT_ANCHOR + (school_year_start - KID_GRADE_ANCHOR_YEAR)


def current_kid_age(today: date | None = None) -> int:
    """Approximate age of the youngest kid based on US schooling norms
    (1st grade ≈ age 6, so age = grade + 6)."""
    return current_kid_grade(today) + 6


def allowed_movie_ratings(today: date | None = None) -> list[str]:
    """Movie ratings the kids may watch, tiered by age:
    - under 11: G, PG
    - under 15: G, PG, PG-13
    - 15 and above: G, PG, PG-13, R, NC-17 (everything)"""
    age = current_kid_age(today)
    if age < 11:
        return ["G", "PG"]
    if age < 15:
        return ["G", "PG", "PG-13"]
    return ["G", "PG", "PG-13", "R", "NC-17"]


def _extracted_grade(title: str) -> int | None:
    """Return the grade number embedded in the title, or None if absent."""
    m = _GRADE_RE.search(title)
    if not m:
        return None
    if m.group(3):  # kindergarten
        return 0
    digits = m.group(1) or m.group(2)
    try:
        return int(digits)
    except (TypeError, ValueError):
        return None


def _is_other_grade(title: str) -> bool:
    g = _extracted_grade(title)
    return g is not None and g != current_kid_grade()


# Calendar/title pairs that should ALWAYS be marked important, even without
# an explicit user click on the events page. Useful for things like school
# closures the user obviously cares about every time.
AUTO_IMPORTANT_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("dorchester parent calendar", re.compile(r"school\s*closed", re.IGNORECASE)),
    # Empty calendar substring matches every calendar.
    ("", re.compile(r"report\s*cards?\s*available", re.IGNORECASE)),
]


def is_auto_important(calendar_name: str, title: str) -> bool:
    cn = (calendar_name or "").lower()
    for needle, pattern in AUTO_IMPORTANT_RULES:
        if needle in cn and pattern.search(title or ""):
            return True
    return False


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


def dedupe_events(events: list[dict]) -> list[dict]:
    """Collapse duplicate occurrences across calendars.

    The same occurrence is recognised by ``(summary, start)`` — title +
    start timestamp uniquely identifies an event instance even when the
    same recurring event is mirrored across multiple calendars (each
    with its own ``iCalUID``). Falls back to ``ical_uid`` only when no
    title is present.

    Order is preserved; the first occurrence of each key wins.
    """
    seen: set[tuple] = set()
    out: list[dict] = []
    for ev in events:
        title = (ev.get("summary") or "").strip().lower()
        start = ev.get("start") or ""
        if title:
            key: tuple = ("t", title, start)
        else:
            key = ("u", ev.get("ical_uid") or "", start)
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


def fetch_events(
    s: Session,
    calendar_ids: list[str],
    start: date,
    end: date,
    *,
    now: datetime | None = None,
    calendar_names: dict[str, str] | None = None,
) -> list[dict]:
    """Fetch events from given calendars in [start, end). Drops events that
    have already ended (relative to ``now``) and any titles caught by
    EVENT_TITLE_FILTERS for that calendar."""
    svc = _service(s)
    now = now or datetime.now(UTC)
    time_min = datetime.combine(start, time.min, tzinfo=UTC).isoformat()
    time_max = datetime.combine(end, time.min, tzinfo=UTC).isoformat()
    cal_names = calendar_names or {}

    out: list[dict] = []
    for cal_id in calendar_ids:
        cal_name = cal_names.get(cal_id, "")
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
                summary = ev.get("summary", "(no title)")
                if is_filtered(cal_name, summary):
                    continue
                start_d, all_day = _event_start(ev)
                end_d = _event_end(ev)
                if end_d and end_d < now:
                    continue
                out.append(
                    {
                        "calendar_id": cal_id,
                        "ical_uid": ev.get("iCalUID") or ev.get("id"),
                        "summary": summary,
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
