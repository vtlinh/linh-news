from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import patch

from app import calendar_summary
from app.db import EventEmoji


def _ev(summary: str, start: str, ical_uid: str | None = None) -> dict:
    return {
        "summary": summary,
        "start": start,
        "end": start,
        "ical_uid": ical_uid or summary,
        "calendar_name": "Test",
    }


def test_render_day_html_uses_emoji_map_and_bullet_separator():
    day = date(2026, 5, 2)
    events = [
        _ev("Library visit", "2026-05-02T09:00:00"),
        _ev("Lunch with team", "2026-05-02T12:00:00"),
        _ev("Dad's birthday", "2026-05-02"),  # all-day
    ]
    emoji_for = {
        "Library visit": "📚",
        "Lunch with team": "🍕",
        "Dad's birthday": "🎂",
    }
    html = calendar_summary.render_day_html(day, events, emoji_for)

    assert "<strong>Saturday, May 2:</strong>" in html
    assert " • " in html
    # All-day event has no time; timed events render "emoji title H:MM AM/PM"
    assert "🎂 Dad's birthday" in html
    assert "📚 Library visit 9:00 AM" in html
    assert "🍕 Lunch with team 12:00 PM" in html


def test_emojis_for_titles_uses_db_cache_and_skips_llm(db_session):
    db_session.add(
        EventEmoji(
            title_norm="library visit",
            emoji="📚",
            generated_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    with patch.object(calendar_summary, "_emoji_lookup_single_call") as llm:
        result = calendar_summary.emojis_for_titles(db_session, ["Library Visit"])
    llm.assert_not_called()
    assert result == {"Library Visit": "📚"}


def test_emojis_for_titles_calls_llm_once_for_missing_then_persists(db_session):
    with patch.object(
        calendar_summary,
        "_emoji_lookup_single_call",
        return_value={"Soccer practice": "⚽"},
    ) as llm:
        result = calendar_summary.emojis_for_titles(
            db_session,
            ["Soccer practice"],
        )
    llm.assert_called_once_with(["Soccer practice"])
    assert result == {"Soccer practice": "⚽"}

    # Second call with the same title must hit the DB, not the LLM.
    with patch.object(calendar_summary, "_emoji_lookup_single_call") as llm2:
        result2 = calendar_summary.emojis_for_titles(
            db_session,
            ["soccer practice"],
        )
    llm2.assert_not_called()
    assert result2 == {"soccer practice": "⚽"}


def test_emojis_for_titles_leaves_uncached_when_llm_fails(db_session):
    """LLM failure is non-fatal: missing titles are simply absent from
    the result and are NOT persisted (so the next refresh retries)."""

    def boom(_titles):
        raise RuntimeError("network is down")

    with patch.object(
        calendar_summary,
        "_emoji_lookup_single_call",
        side_effect=boom,
    ):
        result = calendar_summary.emojis_for_titles(
            db_session,
            ["School pickup"],
        )
    assert result == {}
    assert db_session.get(EventEmoji, "school pickup") is None


def test_persist_events_for_days_writes_events_and_loads_inline(db_session):
    """Refresh persists raw events; load_calendar_section renders inline
    from persisted events + the DB emoji map (no LLM at view time)."""
    from app.db import CalendarDaySummary

    db_session.add_all(
        [
            EventEmoji(
                title_norm="library visit",
                emoji="📚",
                generated_at=datetime.now(UTC),
            ),
            EventEmoji(
                title_norm="dad's birthday",
                emoji="🎂",
                generated_at=datetime.now(UTC),
            ),
        ]
    )
    db_session.commit()

    events_by_day = {
        date(2026, 5, 2): [
            _ev("Library visit", "2026-05-02T09:00:00"),
            _ev("Dad's birthday", "2026-05-02"),
        ],
    }
    with patch.object(calendar_summary, "_emoji_lookup_single_call") as llm:
        calendar_summary.persist_events_for_days(db_session, "vtlinh87@gmail.com", events_by_day)
    llm.assert_not_called()

    row = db_session.get(CalendarDaySummary, ("vtlinh87@gmail.com", date(2026, 5, 2)))
    assert row is not None
    assert "Library visit" in row.events_json

    section = calendar_summary.load_calendar_section(
        db_session, "vtlinh87@gmail.com", date(2026, 5, 2)
    )
    assert "📚" in section
    assert "🎂" in section
    assert " • " in section
    assert "<strong>Saturday, May 2:</strong>" in section


def test_load_calendar_section_re_renders_after_renderer_change(db_session):
    """Stale HTML can never appear because no HTML is cached. Persist a
    day, then change the emoji for one of its titles and confirm the
    next view picks up the new emoji without any cache invalidation."""
    from app.db import CalendarDaySummary

    db_session.add(
        EventEmoji(
            title_norm="soccer practice",
            emoji="⚽",
            generated_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    events_by_day = {
        date(2026, 5, 2): [_ev("Soccer practice", "2026-05-02T17:00:00")],
    }
    with patch.object(calendar_summary, "_emoji_lookup_single_call"):
        calendar_summary.persist_events_for_days(db_session, "vtlinh87@gmail.com", events_by_day)

    section1 = calendar_summary.load_calendar_section(
        db_session, "vtlinh87@gmail.com", date(2026, 5, 2)
    )
    assert "⚽" in section1

    # Update the emoji directly; no refresh, no cache bust — just reload.
    row = db_session.get(EventEmoji, "soccer practice")
    row.emoji = "🥅"
    db_session.commit()

    section2 = calendar_summary.load_calendar_section(
        db_session, "vtlinh87@gmail.com", date(2026, 5, 2)
    )
    assert "🥅" in section2
    assert "⚽" not in section2

    # And events_json is still there exactly as written.
    persisted = db_session.get(CalendarDaySummary, ("vtlinh87@gmail.com", date(2026, 5, 2)))
    assert "Soccer practice" in persisted.events_json


def test_read_emoji_map_is_pure_db_read(db_session):
    db_session.add(
        EventEmoji(
            title_norm="dentist",
            emoji="🦷",
            generated_at=datetime.now(UTC),
        )
    )
    db_session.commit()
    out = calendar_summary.read_emoji_map(db_session, ["Dentist", "Unknown"])
    assert out == {"Dentist": "🦷"}
