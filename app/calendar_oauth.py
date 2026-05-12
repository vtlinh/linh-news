from __future__ import annotations

import logging
import re
import threading
from datetime import UTC, date, datetime, time, timedelta

from google.auth.exceptions import RefreshError
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


# Recognises common ways school events label a grade in the title:
#   "Grade 3", "3rd Grade", "5th-Grade", "Kindergarten"
_GRADE_RE = re.compile(
    r"(?:\bgrade\s*(\d+)\b|\b(\d+)(?:st|nd|rd|th)\s*[-–]?\s*grade\b|\b(kindergarten|kinder)\b)",
    re.IGNORECASE,
)


def _admin_children_grades(today: date) -> list[int]:
    """Look up the admin's children list and return their current grades.
    Returns ``[]`` when the row is missing or unreadable so the calendar
    filter behaves as 'no grade filter at all' rather than crashing."""
    from app import kids
    from app.db import UserSettings, session_factory
    from app.settings import get_settings

    try:
        Maker = session_factory()
        with Maker() as s:
            row = s.get(UserSettings, get_settings().admin_email)
            if row is None:
                return []
            return kids.grades_for_today(row.children_json or [], today)
    except Exception:  # noqa: BLE001
        return []


def current_kid_grades(today: date | None = None) -> list[int]:
    """Return the list of grades currently attended by the admin's
    children (sorted, deduped). Empty if no children are configured."""
    if today is None:
        from app.settings import local_today as _local_today

        today = _local_today()
    return _admin_children_grades(today)


def current_kid_grade(today: date | None = None) -> int:
    """Single representative grade — the youngest configured child's
    grade, or 0 if none. Kept for callers that haven't migrated to
    :func:`current_kid_grades`."""
    grades = current_kid_grades(today)
    return grades[0] if grades else 0


def current_kid_age(today: date | None = None) -> int:
    """Approximate age of the youngest kid during the school year
    (kids turn ``grade + 6`` somewhere during their school year).
    Used as the input to the movie-rating tiering only."""
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
    if g is None:
        return False
    grades = current_kid_grades()
    if not grades:
        return False
    return g not in grades


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


class MissingUserCredentials(RuntimeError):
    """Raised when no ``google_oauth`` row exists for the requested user."""


def _credentials(s: Session, email: str | None = None) -> Credentials:
    from app.settings import get_settings

    target = (email or get_settings().admin_email).lower()
    row = s.execute(
        select(GoogleOAuth).where(GoogleOAuth.email == target)
    ).scalar_one_or_none()
    if row is None:
        raise MissingUserCredentials(
            f"No Google OAuth row for {target}. The user must sign in (and re-consent) "
            f"so a refresh token gets captured."
        )
    if row.revoked_at is not None:
        # Token was previously rejected with invalid_grant and is still
        # marked revoked. Don't bother hitting Google again — surface the
        # same MissingUserCredentials so require_viewer can re-consent.
        raise MissingUserCredentials(
            f"Refresh token for {target} is marked revoked since "
            f"{row.revoked_at.isoformat()}; user must re-consent."
        )
    creds = Credentials(
        token=None,
        refresh_token=row.refresh_token,
        client_id=row.client_id,
        client_secret=row.client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[CALENDAR_SCOPE],
    )
    try:
        creds.refresh(GoogleRequest())
    except RefreshError as e:
        # Google returns invalid_grant when the user has revoked access or
        # the refresh token has been invalidated (e.g. password change,
        # 6-month inactivity). The stored token will never work again —
        # mark the row revoked so require_viewer bounces the user to
        # OAuth re-consent and the admin Users page shows the disconnect.
        if "invalid_grant" in str(e):
            from datetime import UTC, datetime

            _log.warning(
                "Refresh token for %s rejected (invalid_grant); marking "
                "google_oauth row revoked so the user is asked to reconnect.",
                target,
            )
            row.revoked_at = datetime.now(UTC)
            s.commit()
            raise MissingUserCredentials(
                f"Refresh token for {target} was revoked; user must re-consent."
            ) from e
        raise
    return creds


def _service(s: Session, email: str | None = None):
    return build(
        "calendar", "v3", credentials=_credentials(s, email), cache_discovery=False
    )


def _resolved_email(email: str | None) -> str:
    from app.settings import get_settings

    return (email or get_settings().admin_email).lower()


def cached_calendars(
    s: Session, *, refresh_if_empty: bool = True, email: str | None = None
) -> list[dict]:
    """Return the cached per-user snapshot of the Google Calendar list. If
    the cache is empty and ``refresh_if_empty`` is True, hits Google and
    repopulates the cache before returning. No live API call on a warm
    cache."""
    from sqlalchemy import select as _select

    from app.db import GoogleCalendar

    target = _resolved_email(email)
    stmt = (
        _select(GoogleCalendar)
        .where(GoogleCalendar.email == target)
        .order_by(GoogleCalendar.primary.desc(), GoogleCalendar.name)
    )
    rows = s.execute(stmt).scalars().all()
    if rows:
        return [{"id": r.id, "name": r.name, "primary": bool(r.primary)} for r in rows]
    if not refresh_if_empty:
        return []
    return refresh_cached_calendars(s, email=target)


def cached_calendars_fetched_at(s: Session, *, email: str | None = None) -> datetime | None:
    """Latest ``fetched_at`` for this user's cached calendars, or None when
    the cache is empty. Drives "is the cache stale enough to refresh in the
    background?" decisions."""
    from sqlalchemy import select as _select

    from app.db import GoogleCalendar

    target = _resolved_email(email)
    stmt = _select(GoogleCalendar.fetched_at).where(GoogleCalendar.email == target)
    rows = s.execute(stmt).scalars().all()
    return max(rows) if rows else None


def refresh_cached_calendars(s: Session, *, email: str | None = None) -> list[dict]:
    """Hit Google's calendarList API, replace this user's cached snapshot,
    return the fresh list. Raises if the OAuth row is missing."""
    from sqlalchemy import delete as _delete

    from app.db import GoogleCalendar

    target = _resolved_email(email)
    fresh = list_calendars(s, target)
    s.execute(_delete(GoogleCalendar).where(GoogleCalendar.email == target))
    now = datetime.now(UTC)
    for cal in fresh:
        s.add(
            GoogleCalendar(
                email=target,
                id=cal["id"],
                name=cal["name"],
                primary=bool(cal.get("primary")),
                fetched_at=now,
            )
        )
    s.commit()
    return fresh


_log = logging.getLogger(__name__)
_calendar_refresh_locks: dict[str, threading.Lock] = {}
_calendar_refresh_locks_master = threading.Lock()


def _calendar_lock_for(email: str) -> threading.Lock:
    with _calendar_refresh_locks_master:
        lk = _calendar_refresh_locks.get(email)
        if lk is None:
            lk = threading.Lock()
            _calendar_refresh_locks[email] = lk
        return lk


def maybe_refresh_calendars_in_background(
    email: str, *, min_interval_seconds: int = 3600
) -> None:
    """If this user's cached calendar list is older than ``min_interval_seconds``,
    spawn a daemon thread to re-fetch it from Google. Returns immediately.

    Mirrors ``cache.maybe_refresh_in_background`` for events: page loads stay
    fast, the cache catches up out-of-band."""
    from app.db import session_factory

    target = email.lower()
    lock = _calendar_lock_for(target)
    if not lock.acquire(blocking=False):
        return  # another refresh already in flight for this user

    def _run() -> None:
        try:
            Maker = session_factory()
            with Maker() as bs:
                fetched = cached_calendars_fetched_at(bs, email=target)
                if fetched is not None:
                    age = (datetime.now(UTC) - fetched).total_seconds()
                    if age < min_interval_seconds:
                        return
                _log.info("Refreshing calendar list for %s in background...", target)
                fresh = refresh_cached_calendars(bs, email=target)
                _log.info("Calendar refresh complete: %d entries.", len(fresh))
        except Exception as e:  # noqa: BLE001
            _log.exception("Calendar background refresh failed: %s", e)
        finally:
            lock.release()

    threading.Thread(
        target=_run, daemon=True, name=f"calendar-refresh-{target}"
    ).start()


def list_calendars(s: Session, email: str | None = None) -> list[dict]:
    svc = _service(s, email)
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
    calendar_names: dict[str, str] | None = None,
    email: str | None = None,
) -> list[dict]:
    """Fetch events from given calendars in [start, end). Drops any titles
    caught by EVENT_TITLE_FILTERS for that calendar.

    Events are NOT filtered by whether they have already ended — keeping
    earlier-today events (and any other events inside the window) in the
    result means a refresh won't wipe them from per-day persistence, so the
    date picker can still surface them when viewing today's or a past
    edition.
    """
    svc = _service(s, email)
    time_min = datetime.combine(start, time.min, tzinfo=UTC).isoformat()
    time_max = datetime.combine(end, time.min, tzinfo=UTC).isoformat()
    cal_names = calendar_names or {}

    out: list[dict] = []
    for cal_id in calendar_ids:
        cal_name = cal_names.get(cal_id, "")
        page_token = None
        while True:
            resp = (
                svc.events()
                .list(
                    calendarId=cal_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    pageToken=page_token,
                    maxResults=2500,
                )
                .execute()
            )
            for ev in resp.get("items", []):
                if ev.get("status") == "cancelled":
                    continue
                summary = ev.get("summary", "(no title)")
                if is_filtered(cal_name, summary):
                    continue
                start_d, all_day = _event_start(ev)
                end_d = _event_end(ev)
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
