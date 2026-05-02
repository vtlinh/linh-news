from __future__ import annotations

from app.settings import REPO_ROOT


def test_news_pr_has_required_placeholders():
    text = (REPO_ROOT / "news.pr").read_text(encoding="utf-8")
    for ph in [
        "{{DATE}}",
        "{{WATCHLIST_STOCKS}}",
        "{{DORCHESTER_CALENDAR_EVENTS}}",
        "{{CUSTOM_TOPICS}}",
        "<!-- WEATHER_PLACEHOLDER -->",
        "<!-- CALENDAR_PLACEHOLDER -->",
        "<!-- MOVIES_PLACEHOLDER -->",
    ]:
        assert ph in text, f"missing placeholder {ph}"


def test_custom_topics_fence_present():
    text = (REPO_ROOT / "news.pr").read_text(encoding="utf-8")
    assert "<!-- CUSTOM_TOPICS_BEGIN -->" in text
    assert "<!-- CUSTOM_TOPICS_END -->" in text
