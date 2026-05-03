from __future__ import annotations

from datetime import date
from unittest.mock import patch

from app import generate
from app.db import Edition


def test_run_upserts_latest_wins(db_session, monkeypatch, tmp_path):
    # Patch news.pr to a tiny stub so we don't read the real one
    pr = tmp_path / "news.pr"
    pr.write_text("Date={{DATE}} Slot={{SLOT}}", encoding="utf-8")
    monkeypatch.setattr(generate.get_settings(), "news_pr_path", pr, raising=False)

    # Stub Claude + WeasyPrint + calendar
    fake_claude = patch.object(
        generate.claude_client,
        "generate_edition",
        return_value={"html": "<p>hi v1</p>"},
    )
    fake_pdf = patch.object(generate.pdf, "html_to_pdf", return_value=b"%PDF-v1")
    fake_list = patch.object(generate.calendar_oauth, "list_calendars", return_value=[])
    fake_fetch = patch.object(generate.calendar_oauth, "fetch_events", return_value=[])
    fake_movies = patch.object(
        generate.movies_mod, "get_movies", return_value=[],
    )
    fake_forecast = patch.object(generate.weather, "fetch_forecast", return_value={})
    fake_alerts = patch.object(generate.weather, "fetch_alerts", return_value=[])
    fake_now = patch.object(generate.weather, "get_now_cached", return_value="")
    today = date(2026, 4, 30)

    with fake_claude, fake_pdf, fake_list, fake_fetch, fake_movies, \
            fake_forecast, fake_alerts, fake_now:
        generate.run("morning", today=today)
        row = db_session.get(Edition, today)
        assert row.html == "<p>hi v1</p>"
        assert row.pdf == b"%PDF-v1"

    fake_claude2 = patch.object(
        generate.claude_client,
        "generate_edition",
        return_value={"html": "<p>hi v2</p>", "pdf_html": "<p>pdf v2</p>"},
    )
    fake_pdf2 = patch.object(generate.pdf, "html_to_pdf", return_value=b"%PDF-v2")
    with fake_claude2, fake_pdf2, fake_list, fake_fetch, fake_movies, \
            fake_forecast, fake_alerts, fake_now:
        generate.run("evening", today=today)

    db_session.expire_all()
    row = db_session.get(Edition, today)
    assert row.html == "<p>hi v2</p>"
    assert row.pdf == b"%PDF-v2"


def test_build_context_includes_overlays(db_session, monkeypatch):
    today = date(2026, 4, 30)
    with patch.object(generate.calendar_oauth, "list_calendars", return_value=[]), \
         patch.object(generate.calendar_oauth, "fetch_events", return_value=[]), \
         patch.object(
             generate.movies_mod, "get_movies", return_value=[],
         ), \
         patch.object(generate.weather, "fetch_forecast", return_value={}), \
         patch.object(generate.weather, "fetch_alerts", return_value=[]):
        ctx = generate._build_context(db_session, today, "evening")
    assert ctx["DATE"] == "2026-04-30"
    assert ctx["WATCHLIST_STOCKS"] == []
    # Weather + calendar are rendered natively; nothing about them should
    # leak into the LLM-bound public context.
    assert "WEATHER_COORDS" not in ctx
    assert "NOW_WEATHER" not in ctx
    assert "PDF_CALENDAR_HTML" not in ctx
    # Dorchester Parent Calendar is the lone exception — passed in so the
    # LLM can ground the school news section in real upcoming events.
    assert ctx["DORCHESTER_CALENDAR_EVENTS"] == "(none)"
    # Private keys are present here but get popped before the LLM call.
    assert "_pdf_calendar_html" in ctx
    assert "_pdf_movies_html" in ctx
    assert "_weather_forecast" in ctx
    assert "_weather_alerts" in ctx


def test_build_context_dorchester_passthrough(db_session, monkeypatch):
    today = date(2026, 4, 30)
    cals = [
        {"id": "dor", "name": "Dorchester Parent Calendar"},
        {"id": "fam", "name": "Family"},
    ]
    events = [
        {
            "calendar_id": "dor",
            "ical_uid": "u1",
            "summary": "Spring concert",
            "start": "2026-05-08T09:00:00",
        },
        {
            "calendar_id": "dor",
            "ical_uid": "u2",
            "summary": "Field day",
            "start": "2026-05-15",
        },
        {
            "calendar_id": "fam",
            "ical_uid": "u3",
            "summary": "Dentist",
            "start": "2026-05-03T14:00:00",
        },
    ]
    with patch.object(generate.calendar_oauth, "list_calendars", return_value=cals), \
         patch.object(generate.calendar_oauth, "fetch_events", return_value=events), \
         patch.object(generate.calendar_summary, "persist_events_for_days", return_value=None), \
         patch.object(generate.movies_mod, "get_movies", return_value=[]), \
         patch.object(generate.weather, "fetch_forecast", return_value={}), \
         patch.object(generate.weather, "fetch_alerts", return_value=[]):
        ctx = generate._build_context(db_session, today, "evening")
    txt = ctx["DORCHESTER_CALENDAR_EVENTS"]
    assert "Spring concert" in txt
    assert "Field day" in txt
    # Dentist is on the Family calendar — must NOT leak in.
    assert "Dentist" not in txt
    # Time formatting smoke check.
    assert "9:00 AM" in txt
    assert "all-day" in txt


def test_build_context_dedupes_calendar_events(db_session, monkeypatch):
    today = date(2026, 4, 30)
    cals = [
        {"id": "dor", "name": "Dorchester Parent Calendar"},
        {"id": "fam", "name": "Family"},
    ]
    # Same iCalUID + start on two calendars (cross-calendar duplicate) →
    # should collapse to one. Two more entries share the uid but differ on
    # start (recurring instances) → both must survive.
    events = [
        {
            "calendar_id": "dor",
            "ical_uid": "racquetball-uid",
            "summary": "Racquetball weekly",
            "start": "2026-05-04T19:00:00",
        },
        {
            "calendar_id": "fam",
            "ical_uid": "racquetball-uid",
            "summary": "Racquetball weekly",
            "start": "2026-05-04T19:00:00",
        },
        {
            "calendar_id": "dor",
            "ical_uid": "racquetball-uid",
            "summary": "Racquetball weekly",
            "start": "2026-05-11T19:00:00",
        },
    ]
    captured: dict = {}

    def fake_persist(s, by_day, **kwargs):
        captured["by_day"] = by_day

    with patch.object(generate.calendar_oauth, "list_calendars", return_value=cals), \
         patch.object(generate.calendar_oauth, "fetch_events", return_value=events), \
         patch.object(generate.calendar_summary, "persist_events_for_days",
                      side_effect=fake_persist), \
         patch.object(generate.movies_mod, "get_movies", return_value=[]), \
         patch.object(generate.weather, "fetch_forecast", return_value={}), \
         patch.object(generate.weather, "fetch_alerts", return_value=[]):
        generate._build_context(db_session, today, "evening")

    by_day = captured["by_day"]
    may4 = by_day.get(date(2026, 5, 4), [])
    may11 = by_day.get(date(2026, 5, 11), [])
    # Cross-calendar duplicate collapsed to a single entry.
    assert len(may4) == 1, may4
    # Distinct recurring instance preserved.
    assert len(may11) == 1, may11
