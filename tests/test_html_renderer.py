from __future__ import annotations

from app.html_renderer import (
    CALENDAR_PLACEHOLDER,
    MOVIES_PLACEHOLDER,
    WEATHER_PLACEHOLDER,
    render_edition_html,
    text_to_html,
)


def _section(key: str, n_subs: int = 1) -> dict:
    return {
        "key": key,
        "title": f"T-{key}",
        "subsections": [
            {
                "title": f"head-{i}",
                "text": f"body-{i}",
                "sources": [{"url": f"https://x/{i}", "title": f"src-{i}"}],
            }
            for i in range(n_subs)
        ],
    }


def test_render_edition_html_emits_placeholders_and_layout():
    out = render_edition_html({"sections": [_section("us")], "stocks": []})
    assert WEATHER_PLACEHOLDER in out
    assert '<div class="flow">' in out
    assert '<aside class="rail">' in out
    assert CALENDAR_PLACEHOLDER in out
    assert MOVIES_PLACEHOLDER in out
    # Header section must contain only an h2 (so the mobile-collapse JS picks
    # up sibling story sections as followers); the story section must use h3.
    assert '<section class="news-header"' in out
    assert '<section class="news-story"' in out
    assert "<h2>T-us</h2>" in out
    assert "<h3>head-0</h3>" in out


def test_render_edition_html_preserves_input_section_order():
    """Section identity is per-user now; the renderer emits sections in the
    order the LLM returned them (which the prompt asks to match the user's
    configured order)."""
    sections = [_section("ai"), _section("global"), _section("us")]
    out = render_edition_html({"sections": sections, "stocks": []})
    assert out.index("T-ai") < out.index("T-global") < out.index("T-us")


def test_render_subsection_single_source_uses_anchor_shortcut():
    sec = _section("us")
    out = render_edition_html({"sections": [sec], "stocks": []})
    assert '<a class="sources" href="https://x/0"' in out
    assert '<span class="sources-popup">' not in out


def test_render_subsection_multi_sources_uses_popup():
    sec = {
        "key": "us",
        "title": "T",
        "subsections": [
            {
                "title": "h",
                "text": "b",
                "sources": [
                    {"url": "https://a", "title": "A"},
                    {"url": "https://b", "title": "B"},
                ],
            }
        ],
    }
    out = render_edition_html({"sections": [sec], "stocks": []})
    assert '<span class="sources" tabindex="0">SOURCES' in out
    assert '<span class="sources-popup">' in out
    assert 'href="https://a"' in out and 'href="https://b"' in out


def test_text_to_html_converts_markdown_dashes_to_ul():
    out = text_to_html("intro paragraph\n\n- one\n- two\n- three")
    assert "<p>intro paragraph</p>" in out
    assert out.count("<li>") == 3
    assert "<ul>" in out and "</ul>" in out


def test_text_to_html_escapes_html():
    out = text_to_html("plain <script>x</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_render_image_emits_img_with_image_id_only():
    sec = {
        "key": "us",
        "title": "T",
        "subsections": [
            {
                "title": "h",
                "text": "b",
                "sources": [{"url": "https://x", "title": "T"}],
                "images": [{"url": "https://img/orig.jpg", "alt": "alt"}],
                "image_id": 42,
            }
        ],
    }
    out = render_edition_html({"sections": [sec], "stocks": []})
    assert "/edition-image/42" in out
    # The original URL is never leaked to the page — only the DB id.
    assert "https://img/orig.jpg" not in out


def test_render_image_skipped_when_no_image_id():
    sec = {
        "key": "us",
        "title": "T",
        "subsections": [
            {
                "title": "h",
                "text": "b",
                "sources": [{"url": "https://x", "title": "T"}],
                # candidates were supplied, but server image fetch failed → no id.
                "images": [{"url": "https://img/x.jpg"}],
            }
        ],
    }
    out = render_edition_html({"sections": [sec], "stocks": []})
    assert "/edition-image/" not in out


def test_at_most_one_image_per_section_when_multiple_have_ids():
    """Defensive: if stale/multiple subsections somehow carry image_id,
    only the first such subsection renders an <img>."""
    sec = {
        "key": "us",
        "title": "T",
        "subsections": [
            {
                "title": "h0",
                "text": "b",
                "sources": [{"url": "https://x", "title": "T"}],
                "image_id": 1,
            },
            {
                "title": "h1",
                "text": "b",
                "sources": [{"url": "https://x", "title": "T"}],
                "image_id": 2,
            },
        ],
    }
    out = render_edition_html({"sections": [sec], "stocks": []})
    assert "/edition-image/1" in out
    assert "/edition-image/2" not in out


def test_image_renders_on_later_subsection_when_first_has_no_image():
    """When the first subsection produced no usable image but a later one
    did, the image should still render — just on the later subsection."""
    sec = {
        "key": "us",
        "title": "T",
        "subsections": [
            {
                "title": "h0",
                "text": "b",
                "sources": [{"url": "https://x", "title": "T"}],
                # no image_id — first subsection had no usable og:image
            },
            {
                "title": "h1",
                "text": "b",
                "sources": [{"url": "https://x", "title": "T"}],
                "image_id": 7,
            },
        ],
    }
    out = render_edition_html({"sections": [sec], "stocks": []})
    assert "/edition-image/7" in out


def test_stocks_render_with_color_and_tooltip():
    payload = {
        "sections": [],
        "stocks": [
            {
                "ticker": "GOOG",
                "price": "$182.45",
                "percent_diff": 2.3,
                "why_it_moved": {
                    "text": "Strong cloud growth.",
                    "sources": [{"url": f"https://s{i}", "title": f"S{i}"} for i in range(5)],
                },
            },
            {
                "ticker": "TSLA",
                "price": "$140.12",
                "percent_diff": -3.1,
                "why_it_moved": {
                    "text": "Margin compression.",
                    "sources": [{"url": "https://t", "title": "T"}],
                },
            },
        ],
    }
    out = render_edition_html(payload)
    # Gain → green; loss → red.
    assert "color:#0a7d1f" in out
    assert "+2.3%" in out
    assert "color:#b00020" in out
    assert "-3.1%" in out
    # Tooltip wrapper present for the popup mechanism the chrome CSS targets.
    assert '<span class="tooltip"' in out
    assert '<span class="tooltip-popup">' in out


def test_empty_stocks_omits_section():
    out = render_edition_html({"sections": [], "stocks": []})
    assert "📈 Stocks" not in out
