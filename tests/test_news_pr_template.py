from __future__ import annotations

from app.settings import REPO_ROOT


def test_news_pr_has_required_placeholders():
    text = (REPO_ROOT / "news.pr").read_text(encoding="utf-8")
    for ph in [
        "{{DATE}}",
        "{{SLOT}}",
        "{{HIDDEN_MOVIES}}",
        "{{HIDDEN_CALENDARS_JSON}}",
        "{{ALL_CALENDARS_JSON}}",
        "{{CALENDAR_EVENTS_JSON}}",
        "{{IMPORTANT_EVENTS_JSON}}",
        "{{WEATHER_COORDS}}",
        "{{CUSTOM_TOPICS}}",
    ]:
        assert ph in text, f"missing placeholder {ph}"


def test_custom_topics_fence_present():
    text = (REPO_ROOT / "news.pr").read_text(encoding="utf-8")
    assert "<!-- CUSTOM_TOPICS_BEGIN -->" in text
    assert "<!-- CUSTOM_TOPICS_END -->" in text
