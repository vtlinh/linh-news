from __future__ import annotations

from app.llm_schema import SECTION_KEYS, SECTION_TITLES
from app.settings import REPO_ROOT


def test_news_pr_has_required_placeholders():
    text = (REPO_ROOT / "news.pr").read_text(encoding="utf-8")
    for ph in [
        "{{DATE}}",
        "{{WATCHLIST_STOCKS}}",
        "{{DORCHESTER_CALENDAR_EVENTS}}",
        "{{CUSTOM_TOPICS}}",
    ]:
        assert ph in text, f"missing placeholder {ph}"


def test_news_pr_lists_every_required_section_key_and_title():
    text = (REPO_ROOT / "news.pr").read_text(encoding="utf-8")
    for key in SECTION_KEYS:
        assert key in text, f"news.pr should mention section key {key!r}"
    for title in SECTION_TITLES.values():
        assert title in text, f"news.pr should mention section title {title!r}"


def test_news_pr_does_not_ask_for_html():
    """The new contract is structured JSON — the prompt must not instruct
    the LLM to emit HTML tags or wrappers."""
    text = (REPO_ROOT / "news.pr").read_text(encoding="utf-8").lower()
    for needle in ["<section>", "<aside", "<div class", "weather_placeholder"]:
        assert needle not in text, f"news.pr should not reference {needle!r}"


def test_custom_topics_fence_present():
    text = (REPO_ROOT / "news.pr").read_text(encoding="utf-8")
    assert "<!-- CUSTOM_TOPICS_BEGIN -->" in text
    assert "<!-- CUSTOM_TOPICS_END -->" in text
