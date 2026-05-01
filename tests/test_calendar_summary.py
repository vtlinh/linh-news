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
    # All-day event has no time; timed events render "H:MM AM/PM emoji title"
    assert "🎂 Dad's birthday" in html
    assert "9:00 AM 📚 Library visit" in html
    assert "12:00 PM 🍕 Lunch with team" in html


def test_emojis_for_titles_uses_db_cache_and_skips_llm(db_session):
    db_session.add(EventEmoji(
        title_norm="library visit",
        emoji="📚",
        generated_at=datetime.now(UTC),
    ))
    db_session.commit()

    with patch.object(calendar_summary, "_llm_emoji_lookup") as llm:
        result = calendar_summary.emojis_for_titles(db_session, ["Library Visit"])
    llm.assert_not_called()
    assert result == {"Library Visit": "📚"}


def test_emojis_for_titles_calls_llm_once_for_missing_then_persists(db_session):
    with patch.object(
        calendar_summary, "_llm_emoji_lookup",
        return_value={"Soccer practice": "⚽"},
    ) as llm:
        result = calendar_summary.emojis_for_titles(
            db_session, ["Soccer practice"],
        )
    llm.assert_called_once_with(["Soccer practice"])
    assert result == {"Soccer practice": "⚽"}

    # Second call with the same title must hit the DB, not the LLM.
    with patch.object(calendar_summary, "_llm_emoji_lookup") as llm2:
        result2 = calendar_summary.emojis_for_titles(
            db_session, ["soccer practice"],
        )
    llm2.assert_not_called()
    assert result2 == {"soccer practice": "⚽"}


def test_emojis_for_titles_falls_back_when_llm_fails(db_session):
    def boom(_titles):
        raise RuntimeError("network is down")

    with patch.object(calendar_summary, "_llm_emoji_lookup", side_effect=boom):
        result = calendar_summary.emojis_for_titles(
            db_session, ["School pickup"],
        )
    # Keyword fallback rule maps "school" → 🏫
    assert result == {"School pickup": "🏫"}
    row = db_session.get(EventEmoji, "school pickup")
    assert row is not None and row.emoji == "🏫"


def test_get_or_generate_summaries_no_llm_when_emojis_cached(db_session):
    db_session.add_all([
        EventEmoji(
            title_norm="library visit", emoji="📚",
            generated_at=datetime.now(UTC),
        ),
        EventEmoji(
            title_norm="dad's birthday", emoji="🎂",
            generated_at=datetime.now(UTC),
        ),
    ])
    db_session.commit()

    events_by_day = {
        date(2026, 5, 2): [
            _ev("Library visit", "2026-05-02T09:00:00"),
            _ev("Dad's birthday", "2026-05-02"),
        ],
    }
    with patch.object(calendar_summary, "_llm_emoji_lookup") as llm:
        out = calendar_summary.get_or_generate_summaries(
            db_session, events_by_day,
        )
    llm.assert_not_called()
    html = out[date(2026, 5, 2)]
    assert "📚" in html
    assert "🎂" in html
    assert " • " in html
