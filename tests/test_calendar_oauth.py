from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import MagicMock

from app import calendar_oauth


def _fake_service_with_events(events_by_cal: dict[str, list[dict]]):
    svc = MagicMock()

    def events_list(calendarId, **kwargs):
        items = events_by_cal.get(calendarId, [])
        page = MagicMock()
        page.execute.return_value = {"items": items}
        return page

    svc.events.return_value.list = events_list
    return svc


def test_fetch_events_drops_past_and_cancelled(monkeypatch, db_session):
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    items = [
        {  # already ended -> drop
            "summary": "Old",
            "start": {"dateTime": "2026-04-28T10:00:00+00:00"},
            "end": {"dateTime": "2026-04-28T11:00:00+00:00"},
        },
        {  # cancelled -> drop
            "status": "cancelled",
            "summary": "Skipped",
            "start": {"dateTime": "2026-05-01T10:00:00+00:00"},
            "end": {"dateTime": "2026-05-01T11:00:00+00:00"},
        },
        {  # future -> keep
            "summary": "Future",
            "start": {"dateTime": "2026-05-02T10:00:00+00:00"},
            "end": {"dateTime": "2026-05-02T11:00:00+00:00"},
        },
    ]
    monkeypatch.setattr(
        calendar_oauth, "_service",
        lambda s: _fake_service_with_events({"primary": items}),
    )
    out = calendar_oauth.fetch_events(
        db_session, ["primary"], now.date(), now.date().replace(day=15), now=now,
    )
    assert [e["summary"] for e in out] == ["Future"]


def test_current_kid_grade_advances_in_august():
    # April of academic year 2025–2026 → still 2nd grade.
    assert calendar_oauth.current_kid_grade(date(2026, 4, 30)) == 2
    assert calendar_oauth.current_kid_grade(date(2026, 7, 31)) == 2
    # August 1 2026 starts academic year 2026–2027 → 3rd grade.
    assert calendar_oauth.current_kid_grade(date(2026, 8, 1)) == 3
    assert calendar_oauth.current_kid_grade(date(2027, 5, 15)) == 3
    # Future years keep advancing.
    assert calendar_oauth.current_kid_grade(date(2028, 9, 1)) == 5


def test_allowed_movie_ratings_tiers(monkeypatch):
    # Grade 2 → age 8 → G, PG only
    monkeypatch.setattr(calendar_oauth, "current_kid_grade", lambda *a, **k: 2)
    assert calendar_oauth.allowed_movie_ratings() == ["G", "PG"]
    # Grade 5 → age 11 → adds PG-13
    monkeypatch.setattr(calendar_oauth, "current_kid_grade", lambda *a, **k: 5)
    assert calendar_oauth.allowed_movie_ratings() == ["G", "PG", "PG-13"]
    # Grade 9 → age 15 → adds R, NC-17
    monkeypatch.setattr(calendar_oauth, "current_kid_grade", lambda *a, **k: 9)
    assert calendar_oauth.allowed_movie_ratings() == ["G", "PG", "PG-13", "R", "NC-17"]


def test_dorchester_filters_other_grades(monkeypatch):
    monkeypatch.setattr(calendar_oauth, "current_kid_grade", lambda *a, **k: 2)
    cn = "Dorchester Parent Calendar"
    assert calendar_oauth.is_filtered(cn, "3rd Grade Field Trip") is True
    assert calendar_oauth.is_filtered(cn, "Grade 5 Music Recital") is True
    assert calendar_oauth.is_filtered(cn, "Kindergarten Pickup Drill") is True
    # Same-grade and grade-less events pass through.
    assert calendar_oauth.is_filtered(cn, "2nd Grade Read-Aloud") is False
    assert calendar_oauth.is_filtered(cn, "Grade 2 Art Show") is False
    assert calendar_oauth.is_filtered(cn, "Spirit Week") is False
    # Other calendars are unaffected by the grade filter.
    assert calendar_oauth.is_filtered("Linh calendar", "3rd Grade Field Trip") is False


def test_list_calendars_returns_normalized(monkeypatch, db_session):
    svc = MagicMock()
    page = MagicMock()
    page.execute.return_value = {
        "items": [
            {"id": "a", "summary": "Primary", "primary": True},
            {"id": "b", "summary": "Holidays", "summaryOverride": "US Holidays"},
        ]
    }
    svc.calendarList.return_value.list.return_value = page
    monkeypatch.setattr(calendar_oauth, "_service", lambda s: svc)
    out = calendar_oauth.list_calendars(db_session)
    assert out == [
        {"id": "a", "name": "Primary", "primary": True},
        {"id": "b", "name": "US Holidays", "primary": False},
    ]
