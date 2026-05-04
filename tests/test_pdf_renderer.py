from __future__ import annotations

from datetime import date

from app.pdf_renderer import build_pdf_html


def _section(key: str, n_subs: int = 2) -> dict:
    return {
        "key": key,
        "title": f"T-{key}",
        "subsections": [
            {"title": f"h{i}", "text": f"body {i}", "sources": [{"url": "https://x", "title": "T"}]}
            for i in range(n_subs)
        ],
    }


def _full_payload() -> dict:
    return {
        "sections": [
            _section("global", 2),
            _section("us", 1),
        ],
        "stocks": [
            {
                "ticker": "GOOG",
                "price": "$182.45",
                "percent_diff": 2.3,
                "why_it_moved": {"text": "x", "sources": []},
            },
            {
                "ticker": "TSLA",
                "price": "$140",
                "percent_diff": -3.1,
                "why_it_moved": {"text": "y", "sources": []},
            },
        ],
    }


def test_build_pdf_html_top_level_chrome():
    out = build_pdf_html(
        _full_payload(),
        pdf_calendar_html="<section>cal</section>",
        pdf_movies_html="<section>mov</section>",
        weather_strip_html='<div class="weather-strip">Now 12°C ⛅</div>',
        today=date(2026, 5, 3),
    )
    assert out.startswith("<!DOCTYPE html>")
    assert '<header class="masthead">' in out
    assert "The Linh Times" in out
    # Weather appears in the masthead corner; pollen would be stripped if
    # present (we don't pass any here).
    assert "Now" in out
    # Date + Roman volume.
    assert "Sunday, May 3, 2026" in out
    # Stocks footer ribbon with explicit '•' separator.
    assert '<footer class="stocks-footer">' in out
    assert '<span class="stock-sep">•</span>' in out
    # Calendar + movies are injected verbatim into the rail.
    assert "cal" in out and "mov" in out
    # No source links anywhere in the body (PDF stays text-only).
    assert "https://x" not in out


def test_separator_rules_between_stories_and_groups():
    out = build_pdf_html(
        _full_payload(),
        pdf_calendar_html="",
        pdf_movies_html="",
        weather_strip_html="",
        today=date(2026, 5, 3),
    )
    # Within "global" section (2 stories) → exactly 1 sep-story.
    assert out.count('class="sep-story"') == 1
    # Between global and us → exactly 1 sep-group.
    assert out.count('class="sep-group"') == 1


def test_no_section_no_flow():
    out = build_pdf_html(
        {"sections": [], "stocks": []},
        pdf_calendar_html="",
        pdf_movies_html="",
        weather_strip_html="",
        today=date(2026, 5, 3),
    )
    assert '<div class="flow">' not in out
    # No stocks → no footer element. The CSS block always defines the
    # `.stocks-footer` class, so check for the actual <footer ...> tag.
    assert '<footer class="stocks-footer">' not in out


def test_stocks_color_rules():
    out = build_pdf_html(
        _full_payload(),
        pdf_calendar_html="",
        pdf_movies_html="",
        weather_strip_html="",
        today=date(2026, 5, 3),
    )
    assert "#0a7d1f" in out  # GOOG +2.3%
    assert "#b00020" in out  # TSLA -3.1%
