from __future__ import annotations

from datetime import UTC, datetime
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
