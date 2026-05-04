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


def test_fetch_events_drops_cancelled_keeps_past(monkeypatch, db_session):
    # fetch_events no longer filters on "already ended" — earlier-today and
    # past-but-in-window events stay so a refresh can't wipe them from
    # per-day persistence. Cancelled events are still dropped.
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    items = [
        {  # already ended -> still kept (in-window)
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
        calendar_oauth,
        "_service",
        lambda s: _fake_service_with_events({"primary": items}),
    )
    out = calendar_oauth.fetch_events(
        db_session,
        ["primary"],
        now.date(),
        now.date().replace(day=15),
    )
    assert sorted(e["summary"] for e in out) == ["Future", "Old"]


def test_kids_grade_for_advances_in_august():
    """``kids.grade_for`` is the pure helper used by both the prompt
    template and the calendar grade filter. A child born 2018-09-01 is
    in 2nd grade for academic year 2025-2026 and 3rd once August 1 2026
    rolls over."""
    from app import kids

    # Born 2017-09-01 → starts K in fall 2023 → 2nd grade in academic year 2025-2026.
    bd = date(2017, 9, 1)
    assert kids.grade_for(bd, date(2026, 4, 30)) == 2
    assert kids.grade_for(bd, date(2026, 7, 31)) == 2
    assert kids.grade_for(bd, date(2026, 8, 1)) == 3
    assert kids.grade_for(bd, date(2027, 5, 15)) == 3
    assert kids.grade_for(bd, date(2028, 9, 1)) == 5


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
    monkeypatch.setattr(calendar_oauth, "current_kid_grades", lambda *a, **k: [2])
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


def test_dedupe_events_collapses_cross_calendar_duplicates():
    """Same occurrence (same title + same start) appearing on two
    subscribed calendars under different iCalUIDs collapses to one.
    First-seen wins so order is preserved."""
    events = [
        {
            "ical_uid": "u1@cal-a",
            "calendar_id": "cal-a",
            "summary": "Racquetball weekly",
            "start": "2026-05-04T19:00:00-04:00",
        },
        # Mirror of the same occurrence on a second subscribed calendar.
        {
            "ical_uid": "u1@cal-b",
            "calendar_id": "cal-b",
            "summary": "Racquetball weekly",
            "start": "2026-05-04T19:00:00-04:00",
        },
        # Different occurrence (next week) — keep.
        {
            "ical_uid": "u1@cal-a",
            "calendar_id": "cal-a",
            "summary": "Racquetball weekly",
            "start": "2026-05-11T19:00:00-04:00",
        },
        # Different event at same time — keep.
        {
            "ical_uid": "u2@cal-a",
            "calendar_id": "cal-a",
            "summary": "Dinner",
            "start": "2026-05-04T19:00:00-04:00",
        },
    ]
    out = calendar_oauth.dedupe_events(events)
    assert len(out) == 3
    # First-seen wins (cal-a's copy of the racquetball occurrence).
    assert out[0]["calendar_id"] == "cal-a"
    assert out[0]["start"] == "2026-05-04T19:00:00-04:00"
    assert out[1]["start"] == "2026-05-11T19:00:00-04:00"
    assert out[2]["summary"] == "Dinner"


def test_dedupe_events_falls_back_to_uid_when_no_title():
    """Untitled events fall back to (ical_uid, start) so they don't
    incorrectly merge with each other."""
    events = [
        {"ical_uid": "u1", "summary": "", "start": "2026-05-04T10:00:00"},
        {"ical_uid": "u2", "summary": "", "start": "2026-05-04T10:00:00"},
        {"ical_uid": "u1", "summary": "", "start": "2026-05-04T10:00:00"},  # dup
    ]
    out = calendar_oauth.dedupe_events(events)
    assert len(out) == 2
    assert {e["ical_uid"] for e in out} == {"u1", "u2"}
