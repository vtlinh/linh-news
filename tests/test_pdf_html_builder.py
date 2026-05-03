from __future__ import annotations

from datetime import UTC, date, datetime

from app import pdf_html_builder

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
    formatted in Eastern time. The tz abbreviation flips between EST
    (winter) and EDT (summer)."""
    # May 2 is in DST → EDT.
    refreshed = datetime(2026, 5, 2, 19, 30, tzinfo=UTC)  # 15:30 ET
    out = pdf_html_builder.build(
        '<div class="flow"><section><h2>X</h2><p>x</p></section></div>',
        pdf_calendar_html="", pdf_movies_html="",
        today=date(2026, 5, 2),
        refreshed_at=refreshed,
    )
    assert "Refreshed at 15:00 EDT" in out
    assert 'class="refreshed"' in out


def test_builder_treats_naive_refreshed_at_as_local():
    """Naive datetimes are assumed to already be in `LOCAL_TZ`."""
    out = pdf_html_builder.build(
        '<div class="flow"><section><h2>X</h2><p>x</p></section></div>',
        pdf_calendar_html="", pdf_movies_html="",
        today=date(2026, 5, 2),
        refreshed_at=datetime(2026, 5, 2, 7, 15),
    )
    assert "Refreshed at 07:00 EDT" in out


def test_builder_uses_est_in_winter():
    """Winter-month refreshed_at picks up the EST abbreviation."""
    # Jan 15 is outside DST → EST.
    refreshed = datetime(2026, 1, 15, 17, 30, tzinfo=UTC)  # 12:30 ET
    out = pdf_html_builder.build(
        '<div class="flow"><section><h2>X</h2><p>x</p></section></div>',
        pdf_calendar_html="", pdf_movies_html="",
        today=date(2026, 1, 15),
        refreshed_at=refreshed,
    )
    assert "Refreshed at 12:00 EST" in out


def test_builder_section_separators_distinguish_stories_and_groups():
    """Two distinct rules are injected:
      * ``sep-story`` — centred 1/3-width rule between two stories of
        the same category.
      * ``sep-group`` — full-width rule between the last story of one
        category and the next category header.
    No rule is drawn above the first section, above a story that follows
    its own category header, or around back-to-back / empty headers.
    Each rule carries ``break-before: avoid`` so a column break lands
    *after* the rule rather than before it."""
    body = (
        '<div class="flow">'
        # Category 1
        '<section><h2>🌍 Top Global Political News</h2></section>'
        '<section><h3>Headline 1</h3><p>Body 1.</p></section>'
        '<section><h3>Headline 2</h3><p>Body 2.</p></section>'
        '<section><h3>Headline 3</h3><p>Body 3.</p></section>'
        # Category 2
        '<section><h2>🇺🇸 Top US Political News</h2></section>'
        '<section><h3>Headline 4</h3><p>Body 4.</p></section>'
        '<section><h3>Headline 5</h3><p>Body 5.</p></section>'
        '</div>'
    )
    out = pdf_html_builder.build(
        body,
        pdf_calendar_html="", pdf_movies_html="",
        today=date(2026, 5, 2),
    )
    # 2 story-story rules in cat 1 + 1 in cat 2 = 3 sep-story rules.
    assert out.count('class="sep-story"') == 3
    # 1 group boundary (between cat 1's last story and cat 2's header).
    assert out.count('class="sep-group"') == 1
    # Old class names are gone.
    assert 'class="sep-section"' not in out
    assert 'class="sep-article"' not in out
    # Subsection rule is 1/3 width and centred (text-align on outer block).
    assert "width:33%" in out
    assert "text-align:center" in out
    # Group rule is full-width black.
    assert "border-top:0.75pt solid #000" in out
    # Column-break behaviour: every rule glued to preceding content.
    assert "break-before:avoid" in out


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
