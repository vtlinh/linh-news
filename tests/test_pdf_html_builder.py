from __future__ import annotations

from datetime import date

from app import pdf_html_builder

SAMPLE_BODY = """
<div class="weather-strip">Now 12°C 🌤 · Today H 14° / L 7°</div>
<div class="flow">
  <section><h2>🇺🇸 US Politics</h2>
    <article>Story one with a <span class="sources" tabindex="0">SOURCES</span> popup.</article>
    <article>Story two <button onclick="hide()">Hide</button>.</article>
  </section>
  <section><h2>🤖 AI</h2>
    <article>AI story.</article>
  </section>
</div>
<aside class="rail">
  <section><h2>📈 Stocks</h2>
    GOOG $182.45 <span style="color:#0a7d1f;">+2.3%</span>
  </section>
  <!-- CALENDAR_PLACEHOLDER -->
  <!-- MOVIES_PLACEHOLDER -->
</aside>
"""


def test_builder_assembles_print_document():
    out = pdf_html_builder.build(
        SAMPLE_BODY,
        pdf_calendar_html='<section><h2>📅 Calendar</h2><p>Today</p></section>',
        pdf_movies_html='<section><h2>🎬 Movies</h2><p>A film</p></section>',
        today=date(2026, 5, 2),
    )
    assert out.startswith("<!DOCTYPE html>")
    # Masthead + dateline
    assert "The Linh Times" in out
    assert "Saturday, May 2, 2026" in out
    # Weather row 1, stocks row 2
    weather_idx = out.index('class="weather-row"')
    stocks_idx = out.index('class="stocks-row"')
    flow_idx = out.index('class="flow"')
    assert weather_idx < stocks_idx < flow_idx
    # Stocks colour span survives lift
    assert "#0a7d1f" in out
    assert "+2.3%" in out
    # Sources stripped
    assert "SOURCES" not in out
    assert 'class="sources"' not in out
    # Buttons stripped
    assert "<button" not in out
    assert "Hide" not in out
    # Calendar + movies substituted into sidebar
    assert "📅 Calendar" in out
    assert "🎬 Movies" in out
    # <h2> section titles preserved (needed by _inject_lead_image and _drop_one_section)
    assert "🇺🇸 US Politics" in out
    assert "<section>" in out


def test_builder_handles_empty_optional_blocks():
    body = (
        '<div class="weather-strip">Now 0°C</div>'
        '<div class="flow"><section><h2>Title</h2><p>x</p></section></div>'
    )
    out = pdf_html_builder.build(
        body,
        pdf_calendar_html="",
        pdf_movies_html="",
        today=date(2026, 5, 2),
    )
    assert "The Linh Times" in out
    assert "Now 0°C" in out
    # No rail in input → no aside in output
    assert "<aside" not in out


def test_column_count_scales_with_word_count():
    base_section = '<section><h2>X</h2><p>{}</p></section>'
    short = '<div class="flow">' + base_section.format("word " * 100) + "</div>"
    medium = '<div class="flow">' + base_section.format("word " * 2000) + "</div>"
    huge = '<div class="flow">' + base_section.format("word " * 5000) + "</div>"
    today = date(2026, 5, 2)
    out_s = pdf_html_builder.build(short, pdf_calendar_html="", pdf_movies_html="", today=today)
    out_m = pdf_html_builder.build(medium, pdf_calendar_html="", pdf_movies_html="", today=today)
    out_h = pdf_html_builder.build(huge, pdf_calendar_html="", pdf_movies_html="", today=today)
    assert "column-count:4" in out_s
    assert "column-count:5" in out_m
    assert "column-count:6" in out_h
