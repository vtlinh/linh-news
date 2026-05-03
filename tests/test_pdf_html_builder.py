from __future__ import annotations

from datetime import date, datetime, timezone

from app import pdf_html_builder
from app.settings import LOCAL_TZ

SAMPLE_BODY = """
<!-- WEATHER_PLACEHOLDER -->
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
        weather_strip_html='<div class="weather-strip">Now 12°C 🌤 · Today H 14° / L 7°</div>',
        today=date(2026, 5, 2),
    )
    # Weather placeholder is substituted before the rest of the build.
    assert "<!-- WEATHER_PLACEHOLDER -->" not in out
    # "Now"/"Today"/"Tomorrow" labels are bolded inside the weather corner.
    assert "<strong>Now</strong>" in out
    assert "12°C" in out
    assert out.startswith("<!DOCTYPE html>")
    # Masthead + dateline
    assert "The Linh Times" in out
    assert "Saturday, May 2, 2026" in out
    # New NYT-style masthead: motto box on left, weather on right of title.
    assert 'class="motto"' in out
    assert "All the News" in out
    assert "Fit for Linh" in out
    assert 'class="weather-corner"' in out
    # VOL. line uses Roman numerals for the day-of-year (May 2, 2026 = day 122).
    assert "VOL. CXXII" in out
    # Masthead precedes the content (flow + rail), and stocks anchor as footer.
    masthead_idx = out.index('class="masthead"')
    flow_idx = out.index('class="flow"')
    stocks_idx = out.index('class="stocks-footer"')
    assert masthead_idx < flow_idx < stocks_idx
    # Old standalone weather row is gone.
    assert 'class="weather-row"' not in out
    # Old top-of-page stocks row is gone.
    assert 'class="stocks-row"' not in out
    # Chomsky @font-face is wired in for the masthead title.
    assert "@font-face" in out
    assert "Linh Times Masthead" in out
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
    # <h2> section titles preserved (needed by _drop_one_section)
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
    assert "<strong>Now</strong>" in out  # bolded label
    assert "0°C" in out
    assert 'class="weather-corner"' in out
    # No rail in input → no aside in output
    assert "<aside" not in out


def test_builder_strips_html_comments_from_body():
    """HTML comments (LLM scatters `<!-- =========== STOCKS -->` block
    dividers in the rail) must be removed so they don't surface in
    extraction tools / readers."""
    body = (
        '<!-- WEATHER_PLACEHOLDER -->'
        '<div class="flow">'
        '<section><h2>X</h2><!-- block divider --><p>hello</p></section>'
        '</div>'
    )
    out = pdf_html_builder.build(
        body, pdf_calendar_html="", pdf_movies_html="",
        today=date(2026, 5, 2),
    )
    assert "block divider" not in out
    assert "hello" in out


def test_builder_rebuilds_stocks_with_explicit_separator():
    """Stocks rail block is rebuilt from .tooltip spans into a flat list
    joined by an explicit `<span class="stock-sep">` element — bypasses
    the fragile CSS-adjacency rule the LLM kept breaking."""
    body = (
        '<!-- WEATHER_PLACEHOLDER -->'
        '<div class="flow"><section><h2>x</h2><p>x</p></section></div>'
        '<aside class="rail">'
        '<section><h2>📈 Stocks</h2>'
        '<div><span class="tooltip">GOOG $182.45 +2.3%'
        '<span class="tooltip-popup">why moved</span></span></div>'
        '<br>'
        '<div><span class="tooltip">AAPL $200.00 -1.0%</span></div>'
        '</section>'
        '</aside>'
    )
    out = pdf_html_builder.build(
        body, pdf_calendar_html="", pdf_movies_html="",
        today=date(2026, 5, 2),
    )
    # Both tickers present, popup stripped, separator element emitted.
    assert "GOOG" in out and "AAPL" in out
    assert "why moved" not in out
    assert 'class="stock-sep"' in out


def test_builder_uses_provided_refreshed_at_in_dateline():
    """When the caller passes `refreshed_at`, the dateline shows it
    formatted in Eastern time as `Refreshed at HH:00 EST`."""
    refreshed = datetime(2026, 5, 2, 19, 30, tzinfo=timezone.utc)  # 15:30 ET
    out = pdf_html_builder.build(
        '<div class="flow"><section><h2>X</h2><p>x</p></section></div>',
        pdf_calendar_html="", pdf_movies_html="",
        today=date(2026, 5, 2),
        refreshed_at=refreshed,
    )
    assert "Refreshed at 15:00 EST" in out
    assert 'class="refreshed"' in out


def test_builder_treats_naive_refreshed_at_as_local():
    """Naive datetimes are assumed to already be in `LOCAL_TZ`."""
    out = pdf_html_builder.build(
        '<div class="flow"><section><h2>X</h2><p>x</p></section></div>',
        pdf_calendar_html="", pdf_movies_html="",
        today=date(2026, 5, 2),
        refreshed_at=datetime(2026, 5, 2, 7, 15),
    )
    assert "Refreshed at 07:00 EST" in out


def test_builder_section_separator_css_present():
    """Sections after the first inside the flow get a black top rule;
    articles after the first inside a section get a short centered rule."""
    out = pdf_html_builder.build(
        '<div class="flow"><section><h2>X</h2><p>x</p></section></div>',
        pdf_calendar_html="", pdf_movies_html="",
        today=date(2026, 5, 2),
    )
    assert ".flow > section:not(:first-of-type)" in out
    assert ".flow > section > article:not(:first-of-type)::before" in out


def test_column_count_scales_with_word_count():
    base_section = '<section><h2>X</h2><p>{}</p></section>'
    short = '<div class="flow">' + base_section.format("word " * 100) + "</div>"
    medium = '<div class="flow">' + base_section.format("word " * 2000) + "</div>"
    huge = '<div class="flow">' + base_section.format("word " * 5000) + "</div>"
    today = date(2026, 5, 2)
    out_s = pdf_html_builder.build(short, pdf_calendar_html="", pdf_movies_html="", today=today)
    out_m = pdf_html_builder.build(medium, pdf_calendar_html="", pdf_movies_html="", today=today)
    out_h = pdf_html_builder.build(huge, pdf_calendar_html="", pdf_movies_html="", today=today)
    # Flow always uses 4 inner columns; rail makes 5 visible total.
    assert "column-count:4" in out_s
    assert "column-count:4" in out_m
    assert "column-count:4" in out_h
