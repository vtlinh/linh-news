from __future__ import annotations

from datetime import date

from app.pdf_renderer import (
    AssemblyLayout,
    _pick_split_index,
    _section_word_count,
    assemble_final_html,
    build_pdf_parts,
)


def _section(key: str, n_subs: int = 2, words_per_sub: int = 3) -> dict:
    body = " ".join(["word"] * words_per_sub)
    return {
        "key": key,
        "title": f"T-{key}",
        "subsections": [
            {
                "title": f"h{i}",
                "text": f"{body} {i}",
                "sources": [{"url": "https://x", "title": "T"}],
            }
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


def test_build_pdf_parts_yields_two_bands_for_two_sections():
    parts = build_pdf_parts(
        _full_payload(),
        pdf_calendar_html="<section>cal</section>",
        pdf_movies_html="<section>mov</section>",
        weather_strip_html='<div class="weather-strip">Now 12°C ⛅</div>',
        today=date(2026, 5, 3),
    )
    assert parts.section_count == 2
    assert len(parts.news_bands) == 2
    upper_html, upper_words = parts.news_bands[0]
    lower_html, lower_words = parts.news_bands[1]
    assert upper_words > 0 and lower_words > 0
    # Each band's flow markup uses the section/header pattern.
    assert 'class="news-header"' in upper_html
    assert 'class="news-header"' in lower_html
    # Top region contains the masthead title + dateline.
    assert "The Linh Times" in parts.top_inner_html
    assert "Sunday, May 3, 2026" in parts.top_inner_html
    # Stocks ribbon has the GOOG + TSLA + bullet separator.
    assert "GOOG" in parts.stocks_inner_html
    assert '<span class="stock-sep">•</span>' in parts.stocks_inner_html
    # Rail wraps calendar + movies in rail-block divs.
    assert "cal" in parts.rail_inner_html and "mov" in parts.rail_inner_html
    # No source links anywhere — body stays print-only.
    for blob in (
        (parts.top_inner_html,)
        + tuple(h for h, _ in parts.news_bands)
        + (
            parts.rail_inner_html,
            parts.stocks_inner_html,
        )
    ):
        assert "https://x" not in blob


def test_separator_rules_within_bands():
    """A section with 2 stories produces 1 sep-story; band boundaries are
    not crossed by sep-group rules (each band is independent)."""
    parts = build_pdf_parts(
        _full_payload(),
        pdf_calendar_html="",
        pdf_movies_html="",
        weather_strip_html="",
        today=date(2026, 5, 3),
    )
    # The "global" section (2 stories) is its own band with 1 sep-story rule.
    # The "us" section (1 story) lands in the lower band with 0 sep-story
    # rules. No sep-group between bands.
    all_band_html = "".join(h for h, _ in parts.news_bands)
    # Two stories in the "global" band → one sep-story rule.
    assert all_band_html.count('class="sep-story"') == 1
    # Bands are split — no sep-group emitted across the boundary.
    assert all_band_html.count('class="sep-group"') == 0


def test_no_section_yields_zero_bands():
    parts = build_pdf_parts(
        {"sections": [], "stocks": []},
        pdf_calendar_html="",
        pdf_movies_html="",
        weather_strip_html="",
        today=date(2026, 5, 3),
    )
    assert parts.section_count == 0
    assert parts.news_bands == []
    assert parts.stocks_inner_html == ""


def test_one_section_yields_single_band_caller_decides():
    parts = build_pdf_parts(
        {"sections": [_section("global", 2)], "stocks": []},
        pdf_calendar_html="",
        pdf_movies_html="",
        weather_strip_html="",
        today=date(2026, 5, 3),
    )
    assert parts.section_count == 1
    # Single band: 1 entry. (The pdf.html_to_pdf_ex pipeline raises
    # PdfSkipped on this — separately tested.)
    assert len(parts.news_bands) == 1


def test_section_word_count_sums_titles_and_text():
    sec = {
        "key": "x",
        "title": "Two Words",
        "subsections": [
            {"title": "A", "text": "one two three"},
            {"title": "B C", "text": "four five"},
        ],
    }
    # title (2) + sub0 title (1) + sub0 text (3) + sub1 title (2) + sub1 text (2)
    assert _section_word_count(sec) == 10


def test_pick_split_index_lands_close_to_70_percent():
    # Section word counts: 70, 30 → split at k=1 yields upper ratio 0.70.
    sections = [
        _section("a", n_subs=1, words_per_sub=68),  # ~70 words incl. title weight
        _section("b", n_subs=1, words_per_sub=28),  # ~30 words
    ]
    k = _pick_split_index(sections)
    assert k == 1


def test_pick_split_index_three_sections_chooses_70_30_boundary():
    sections = [
        _section("a", n_subs=1, words_per_sub=40),
        _section("b", n_subs=1, words_per_sub=30),
        _section("c", n_subs=1, words_per_sub=30),
    ]
    # Cumulative ratios: k=1 → ~40%, k=2 → ~70%. Best is k=2.
    k = _pick_split_index(sections)
    assert k == 2


def test_pick_split_index_returns_none_for_one_section():
    assert _pick_split_index([_section("a")]) is None
    assert _pick_split_index([]) is None


def test_assemble_final_html_contains_all_region_classes_and_distinct_fonts():
    parts = build_pdf_parts(
        _full_payload(),
        pdf_calendar_html="<section>cal</section>",
        pdf_movies_html="<section>mov</section>",
        weather_strip_html="",
        today=date(2026, 5, 3),
    )
    layout = AssemblyLayout(
        page_w_in=15.296,
        page_h_in=27.193,
        margin_in=0.4,
        top_h_in=1.6,
        stocks_h_in=0.5,
        rail_w_in=2.4,
        content_gap_in=14 / 72,
        upper_h_in=17.0,
        lower_h_in=7.3,
        upper_font_pt=14.0,
        lower_font_pt=12.0,
        rail_font_pt=10.5,
        top_font_pt=11.0,
        stocks_font_pt=10.0,
    )
    out = assemble_final_html(parts, layout)
    assert "<!DOCTYPE html>" in out
    for cls in ("top", "news-u", "news-l", "rail", "stocks-footer"):
        assert cls in out, cls
    # Four distinct font-size declarations for the four sized regions.
    assert "14.00pt" in out
    assert "12.00pt" in out
    assert "10.50pt" in out
    assert "11.00pt" in out or "11pt" in out


def test_headline_box_height_subtracts_vertical_chrome():
    """Regression guard: WeasyPrint flex treats the declared ``height``
    as content-box. The headline-box CSS must subtract the 13pt of vertical
    chrome (1pt borders × 2 + 8pt top padding + 3pt bottom padding) from
    the declared height, otherwise the headline-box renders ~13pt taller
    than its budget and the sibling .upper-bottom (flex: 1 1 auto) shrinks
    to compensate — clipping its pre-fit content."""
    parts = build_pdf_parts(
        _full_payload(),
        pdf_calendar_html="",
        pdf_movies_html="",
        weather_strip_html="",
        today=date(2026, 5, 3),
    )
    layout = AssemblyLayout(
        page_w_in=15.296,
        page_h_in=27.193,
        margin_in=0.4,
        top_h_in=1.6,
        stocks_h_in=0.5,
        rail_w_in=2.4,
        content_gap_in=14 / 72,
        upper_h_in=17.0,
        lower_h_in=7.3,
        upper_font_pt=14.0,
        lower_font_pt=12.0,
        rail_font_pt=10.5,
        top_font_pt=11.0,
        stocks_font_pt=10.0,
        # Headline fields — drive the headline-box render.
        headline_h_in=8.0,
        headline_box_w_in=8.88,
        headline_col_span=3,
        headline_image_w_in=0.0,
        headline_image_h_in=0.0,
        headline_image_data_uri="",
        headline_title="Title",
        headline_title_font_pt=18.0,
        headline_body_html="<p>body</p>",
        headline_body_font_pt=12.0,
        headline_body_col_h_in=4.0,
    )
    parts.has_headline = True
    out = assemble_final_html(parts, layout)
    # 8.0in - 13/72in = 7.819in. Confirm the chrome is subtracted (i.e. NOT
    # the raw 8.000in declared value).
    assert "height: 7.819in" in out
    # And the un-subtracted form must NOT appear in the .headline-box rule.
    assert "height: 8.000in" not in out


def test_stocks_color_rules_round_trip_through_parts():
    parts = build_pdf_parts(
        _full_payload(),
        pdf_calendar_html="",
        pdf_movies_html="",
        weather_strip_html="",
        today=date(2026, 5, 3),
    )
    assert "#0a7d1f" in parts.stocks_inner_html  # GOOG +2.3%
    assert "#b00020" in parts.stocks_inner_html  # TSLA -3.1%
