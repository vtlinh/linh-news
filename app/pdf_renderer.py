"""Render the print-styled HTML for WeasyPrint directly from the structured
LinhNews response.

The PDF is composed in five fixed regions:

  +-------------------------------------------------------+
  |                MASTHEAD + DATELINE                    |  ← top
  +-------------------------------------------------------+
  |                                            |          |
  |             UPPER NEWS (4 cols, 70%)       |          |
  +--------------------------------------------+   RAIL   |
  |             LOWER NEWS (4 cols, 30%)       |          |
  +--------------------------------------------+----------+
  |                STOCKS FOOTER                          |  ← stocks
  +-------------------------------------------------------+

This module is responsible for two things:

* ``build_pdf_parts(linhnews, …)`` — slice the structured response into the
  HTML fragments that fill each region, plus the shared CSS @font-face / class
  rules. Sections are assigned to upper/lower bands by word count
  (split-point that lands the upper-band ratio closest to 70%, preserving
  LLM section order).
* ``assemble_final_html(parts, fonts=…, regions=…)`` — stitch the fitted
  fragments back into a single one-page WeasyPrint document with per-region
  font sizes and explicit region dimensions.

PDF rules vs the screen renderer:
  * No source links anywhere in the body (PDF is print-only).
  * Stocks become a flat ``•``-separated footer ribbon — no tooltip popups.
  * Each news section emits a story-divider rule between consecutive stories
    and a heavier group-divider rule between the last story of one section
    and the next section's header.
"""

from __future__ import annotations

import base64
import html as html_mod
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.html_renderer import format_percent, percent_color, text_to_html

log = logging.getLogger(__name__)

_MASTHEAD_FONT_PATH = Path(__file__).resolve().parent / "fonts" / "Chomsky.otf"
_MASTHEAD_FONT_FAMILY = "Linh Times Masthead"

_FLOW_COLUMNS = 4

# Bump whenever the PDF side rail's render logic changes (calendar/movies
# layout, font sizing, backdrop sizing, etc.). The generate pipeline keys
# its rail-HTML cache on this — re-runs for the same date rebuild the
# rail iff the stored version differs from the current one.
# v3: 5-region pipeline corrected after the 10.18in measurement bug —
#     previous v2 caches captured rails that had been over-trimmed inside
#     a falsely-tiny rail box, so they now contain far fewer items than
#     the corrected rail box can comfortably hold.
# v4: layout chrome changed (band-divider added between upper and lower
#     news bands) which reshuffles the rail's available height slightly;
#     bump to force a fresh rail fit on the next refresh.
# v5: rail fit now subtracts border-left + padding-left from the fit width
#     to match the assembled aside.rail content area (was clipping the
#     right edge of every line); refit needed.
PDF_RAIL_VERSION = 5

# Upper band's target share of the news area (the rest goes to the lower
# band). Used to pick the section-split point.
UPPER_BAND_RATIO = 0.70


# ── Public types ──────────────────────────────────────────────────────────


@dataclass
class PdfParts:
    """The five region HTML fragments + shared CSS head used by the
    per-region fit primitive and the final assembler.

    ``news_bands`` is a list of ``(inner_html, word_count)`` tuples:
      * 0 entries → no news (e.g. empty payload); chrome-only PDF.
      * 1 entry   → single news region (sections were 0/1 or split would
        have emptied a band — caller can decide to skip PDF entirely).
      * 2 entries → upper then lower, sliced by word count.

    ``font_face_css`` is the @font-face declarations (Chomsky masthead +
    LinhEmoji emoji-only fallback) — shared between fit passes and final
    assembly so every region sees the same custom fonts.

    ``masthead_title_pt`` is the absolute pt size for the Chomsky title
    (computed from the masthead name length so it never overflows its
    flex slot).
    """

    top_inner_html: str
    news_bands: list[tuple[str, int]]
    rail_inner_html: str
    stocks_inner_html: str
    font_face_css: str
    masthead_title_pt: int
    section_count: int = 0

    # ── Optional headline (front-page hero). Set only when the LLM
    # response carries ``LinhNews.headline``. When set, the upper band
    # in the assembled PDF is restructured into an L-shape:
    #   upper-top (height = headline_h): [headline-box, news-u-right]
    #   news-u-bottom (block, column-count: 4) below the upper-top.
    has_headline: bool = False
    headline_title: str = ""
    headline_body_html: str = ""
    headline_word_count: int = 0
    headline_image_bytes: bytes | None = None
    headline_image_mime: str | None = None
    headline_image_aspect: float | None = None
    upper_sections: list[dict] | None = None
    lower_sections: list[dict] | None = None
    image_bytes_by_id: dict[int, tuple[bytes, str]] | None = None


# ── small utilities ───────────────────────────────────────────────────────


def _esc(s: str) -> str:
    return html_mod.escape(s or "", quote=True)


def _format_date(d: date) -> str:
    """Cross-platform 'Saturday, May 2, 2026'."""
    return d.strftime("%A, %B ") + str(d.day) + d.strftime(", %Y")


def _roman(n: int) -> str:
    pairs = [
        (1000, "M"),
        (900, "CM"),
        (500, "D"),
        (400, "CD"),
        (100, "C"),
        (90, "XC"),
        (50, "L"),
        (40, "XL"),
        (10, "X"),
        (9, "IX"),
        (5, "V"),
        (4, "IV"),
        (1, "I"),
    ]
    out: list[str] = []
    for v, sym in pairs:
        while n >= v:
            out.append(sym)
            n -= v
    return "".join(out) or "N"


def _day_of_year(d: date) -> int:
    return (d - date(d.year, 1, 1)).days + 1


def _format_weather_for_pdf(strip_html: str, prose_html: str = "") -> str:
    """Reshape the weather data for the PDF masthead corner.

    When ``prose_html`` (the hand-written paragraph baked at generation
    time) is supplied, return its inner HTML directly — the bold
    Today/Tomorrow keywords and any alert suffixes are already in place.
    The PDF intentionally drops the live 'Now' temperature, which is only
    meaningful in the auto-refreshing HTML viewer.

    When no prose is available (older editions), fall back to extracting
    the inner text of ``<div class="weather-strip">…</div>`` and applying
    the legacy layout.
    """
    if prose_html:
        m_p = re.search(
            r'<div\s+class="weather-prose"[^>]*>(.*?)</div\s*>',
            prose_html,
            re.IGNORECASE | re.DOTALL,
        )
        return m_p.group(1) if m_p else prose_html
    if not strip_html:
        return ""
    m = re.search(
        r'<div\s+class="weather-strip"[^>]*>(.*?)</div\s*>',
        strip_html,
        re.IGNORECASE | re.DOTALL,
    )
    inner = m.group(1) if m else strip_html
    inner = re.sub(
        r'<span\s+class="weather-refreshed"[^>]*>.*?</span\s*>',
        "",
        inner,
        flags=re.IGNORECASE | re.DOTALL,
    )
    inner = re.sub(
        r'<span\s+class="weather-main"[^>]*>(.*?)</span\s*>',
        r"\1",
        inner,
        flags=re.IGNORECASE | re.DOTALL,
    )
    inner = re.sub(r"\s*·\s*Pollen[^·]*", "", inner, flags=re.IGNORECASE)
    inner = re.sub(r"^\s*Pollen[^·]*·\s*", "", inner, flags=re.IGNORECASE)
    raw_parts = [p.strip() for p in inner.split(" · ") if p.strip()]
    merged: list[str] = []
    for p in raw_parts:
        if p.lower().startswith("wind") and merged:
            merged[-1] = f"{merged[-1]}, {p}"
        else:
            merged.append(p)

    def _bold_label(line: str) -> str:
        m = re.match(r"^(Now|Today|Tomorrow)\b", line)
        if not m:
            return line
        label = m.group(1)
        rest = line[m.end() :]
        return f"<strong>{label}</strong>{rest}"

    return " · ".join(_bold_label(p) for p in merged)


# ── Per-section HTML builders ─────────────────────────────────────────────


def _render_story_html(
    sub: dict,
    *,
    image_bytes_by_id: dict[int, tuple[bytes, str]] | None,
    with_image: bool,
) -> str:
    title = _esc(sub.get("title", ""))
    body = text_to_html(sub.get("text", ""))
    image_html = ""
    if with_image and image_bytes_by_id:
        image_id = sub.get("image_id")
        entry = image_bytes_by_id.get(int(image_id)) if image_id else None
        if entry is not None:
            from app import images as _images

            resized = _images.resize_for_pdf(entry[0])
            data, mime = resized if resized is not None else entry
            src = _images.to_data_uri(data, mime)
            image_html = f'<img class="story-image" src="{src}" alt="" />'
    return f"{image_html}<h3>{title}</h3>{body}"


def _render_section_for_pdf(
    section: dict,
    *,
    image_bytes_by_id: dict[int, tuple[bytes, str]] | None,
) -> tuple[str, list[str]]:
    """Return (header_html, [story_html, …]) for a single news section."""
    key = section.get("key", "")
    title = _esc(section.get("title", "") or key)
    header = f'<section class="news-header"><h2>{title}</h2></section>'
    stories: list[str] = []
    image_used = False
    for sub in section.get("subsections") or []:
        with_image = (not image_used) and bool(sub.get("image_id"))
        story = _render_story_html(
            sub,
            image_bytes_by_id=image_bytes_by_id,
            with_image=with_image,
        )
        if with_image:
            image_used = True
        stories.append(f'<section class="news-story">{story}</section>')
    return header, stories


def _section_word_count(section: dict) -> int:
    """Count words across every subsection's title + text. Used to assign
    sections to the 70%/30% bands without involving heading / image area."""
    total = 0
    for sub in section.get("subsections") or []:
        title = sub.get("title", "") or ""
        text = sub.get("text", "") or ""
        total += len(title.split()) + len(text.split())
    # Section title itself is a heading; count it too so a section with a
    # long title but few subsections still gets some weight.
    total += len((section.get("title", "") or "").split())
    return total


def _pick_split_index(sections: list[dict]) -> int | None:
    """Return the split index ``k`` such that ``sections[:k]`` go to the
    upper band and ``sections[k:]`` go to the lower band. We choose the
    split that lands the upper-band word ratio closest to
    ``UPPER_BAND_RATIO``, preserving the LLM-given section order.

    Returns ``None`` when there are fewer than 2 sections (no meaningful
    split possible) — callers fall back to a single-band layout.
    """
    if len(sections) < 2:
        return None
    counts = [_section_word_count(s) for s in sections]
    total = sum(counts)
    if total == 0:
        # All sections empty — split right down the middle by count.
        return len(sections) // 2 or 1
    best_k = 1
    best_diff = float("inf")
    for k in range(1, len(sections)):
        upper_ratio = sum(counts[:k]) / total
        diff = abs(upper_ratio - UPPER_BAND_RATIO)
        if diff < best_diff:
            best_diff = diff
            best_k = k
    return best_k


def _render_news_band(
    sections_slice: list[dict],
    *,
    image_bytes_by_id: dict[int, tuple[bytes, str]] | None,
) -> str:
    """Stitch a slice of sections into the 4-column flow markup with
    sep-story / sep-group rules between adjacent stories and sections.

    Identical separator semantics to the old single-flow renderer, just
    constrained to a contiguous slice rather than the entire section list.
    """
    parts: list[str] = []
    for i, sec in enumerate(sections_slice):
        if not sec.get("key"):
            continue
        header, stories = _render_section_for_pdf(sec, image_bytes_by_id=image_bytes_by_id)
        if i > 0 and parts:
            parts.append('<div class="sep-group"></div>')
        parts.append(header)
        for j, story in enumerate(stories):
            if j > 0:
                parts.append('<div class="sep-story"></div>')
            parts.append(story)
    return "".join(parts)


def _render_stocks_inner(stocks: list[dict]) -> str:
    """Inner HTML of the stocks ribbon (no outer ``<footer>`` — that's
    emitted by the assembler so we can pin its dimensions)."""
    if not stocks:
        return ""
    items: list[str] = []
    for s in stocks:
        ticker = _esc(s.get("ticker", ""))
        price = _esc(s.get("price", ""))
        pct = float(s.get("percent_diff") or 0)
        color = percent_color(pct)
        items.append(
            f'<span class="tooltip">{ticker} {price} '
            f'<span style="color:{color};">{_esc(format_percent(pct))}</span>'
            f"</span>"
        )
    sep = ' <span class="stock-sep">•</span> '
    return sep.join(items)


def _render_top_inner(
    *,
    today: date,
    weather_inner: str,
    masthead_name: str,
    title_pt: int,
    vol_number: int,
    ai_cost_usd: float,
) -> str:
    """Masthead + dateline inner HTML.

    ``vol_number`` is the days-since-first-edition count (1-indexed).
    ``ai_cost_usd`` is the total Anthropic API cost for the current
    edition; rendered in the dateline's right slot."""
    title_text = f"The {masthead_name} Times"
    dateline = _format_date(today)
    motto_quote = f'"All the News<br>That\'s Fit for {masthead_name}"'
    weather_block = (
        f'<span class="weather-title">The Weather</span>{weather_inner}'
        if weather_inner.strip()
        else ""
    )
    price_block = (
        f'<span class="ai-cost-label">Prices vary by AI</span>'
        f'<span class="ai-cost-value">${ai_cost_usd:.2f}</span>'
    )
    return (
        '<header class="masthead">'
        f'<div class="motto"><span>{motto_quote}</span></div>'
        f'<h1 class="title" style="font-size:{title_pt}pt">{_esc(title_text)}</h1>'
        f'<div class="weather-corner">{weather_block}</div>'
        "</header>"
        '<div class="dateline">'
        f'<span class="vol">VOL. {_roman(vol_number)}</span>'
        f'<span class="date">{dateline}</span>'
        f'<span class="ai-cost">{price_block}</span>'
        "</div>"
    )


def _render_rail_inner(pdf_calendar_html: str, pdf_movies_html: str) -> str:
    """Inner HTML of the side rail. Calendar block and movies block are
    each wrapped in a ``.rail-block`` div so the ``.rail-block + .rail-block
    { margin-top }`` rule only fires between the major blocks, not between
    every event row or movie card.
    """
    parts: list[str] = []
    if pdf_calendar_html:
        parts.append(f'<div class="rail-block">{pdf_calendar_html}</div>')
    if pdf_movies_html:
        parts.append(f'<div class="rail-block">{pdf_movies_html}</div>')
    return "".join(parts)


# ── @font-face declarations shared across regions ─────────────────────────


_EMOJI_FAMILY = '"LinhEmoji"'
_EMOJI_FACE = (
    '@font-face { font-family: "LinhEmoji";'
    ' src: local("Noto Color Emoji"),'
    '      local("Segoe UI Emoji"),'
    '      local("Apple Color Emoji");'
    " unicode-range: U+1F000-1FFFF, U+2600-27BF, U+1F1E6-1F1FF,"
    "                U+2300-23FF, U+2B00-2BFF, U+2900-297F,"
    "                U+1F900-1F9FF, U+1FA70-1FAFF; }"
)


def _build_font_face_css() -> str:
    """Return the @font-face CSS block that every region needs: the
    LinhEmoji unicode-range fallback (so emoji codepoints render in color
    via Noto Color Emoji) and the Chomsky masthead font embedded as a
    data URI (bypasses WeasyPrint's url_fetcher, which mis-handles file://
    paths for OTF fonts)."""
    font_b64 = base64.b64encode(_MASTHEAD_FONT_PATH.read_bytes()).decode("ascii")
    font_url = f"data:font/otf;base64,{font_b64}"
    return (
        _EMOJI_FACE
        + " "
        + (
            f'@font-face {{ font-family: "{_MASTHEAD_FONT_FAMILY}";'
            f' src: url("{font_url}") format("opentype");'
            " font-weight: normal; font-style: normal; }"
        )
    )


# ── Region-scoped CSS used by both fit pass and final assembly ────────────


def news_region_css() -> str:
    """CSS that styles the news flow inside a ``.region`` container.
    Headings scale with the region's font-size via ``em`` so each band can
    pick its own font without parallel rule blocks.
    """
    return f"""
    .region {{
        column-count: {_FLOW_COLUMNS};
        column-gap: 14pt;
        column-rule: 0.5pt solid #999;
    }}
    .region section.news-header h2 {{
        font-size: 1.375em; font-weight: bold !important;
        margin: 4pt 0 2pt;
        font-family: "Times New Roman", Georgia, serif, {_EMOJI_FAMILY};
        overflow-wrap: break-word;
        word-wrap: break-word;
    }}
    .region section.news-story h3 {{
        font-size: 1.125em; font-weight: bold !important;
        margin: 3pt 0 1pt;
        font-family: "Times New Roman", Georgia, serif, {_EMOJI_FAMILY};
        overflow-wrap: break-word;
        word-wrap: break-word;
    }}
    .region section.news-story p,
    .region section.news-story ul,
    .region section.news-story li {{
        margin: 0 0 3pt; font-size: 1em; line-height: 1.15;
        font-family: "Times New Roman", Georgia, serif, {_EMOJI_FAMILY};
        text-align: justify;
        hyphens: auto;
        -webkit-hyphens: auto;
        /* Allow long unbreakable words (acronyms, URLs, "U.S.A.E.") to
           break mid-word as a last resort so they don't extend past the
           column and get chopped by overflow: hidden. */
        overflow-wrap: break-word;
        word-wrap: break-word;
    }}
    .region section, .region article, .region header, .region footer {{
        margin: 0 0 3pt;
    }}
    .region section.news-story img.story-image {{
        display: block; width: auto; max-width: 100%;
        height: auto; margin: 0 0 4pt;
    }}
    .region div.sep-story {{
        display: block; text-align: center;
        margin: 6pt 0 4pt; padding: 0; height: 0;
        break-inside: avoid;
    }}
    .region div.sep-story::before {{
        content: ""; display: inline-block;
        width: 33%; height: 0;
        border-top: 0.4pt solid #999;
        vertical-align: middle;
    }}
    .region div.sep-story + section {{ padding-top: 6pt; }}
    .region div.sep-group {{
        display: block; border: 0; border-top: 0.75pt solid #000;
        margin: 8pt 0 4pt; padding: 0; height: 0;
        break-inside: avoid;
    }}
    """


def rail_region_css() -> str:
    """CSS for the rail when rendered as a single-column region."""
    return f"""
    .region {{ column-count: 1; line-height: 1.2; }}
    .region .rail-block + .rail-block {{ margin-top: 8pt; }}
    .region .cal-date, .region .movie-title {{ font-size: 1.1em; }}
    .region .cal-event, .region .movie-desc {{ font-size: 1em; }}
    .region * {{
        font-family: "Times New Roman", Georgia, serif, {_EMOJI_FAMILY};
    }}
    """


def top_region_css() -> str:
    """CSS for the masthead + dateline row when rendered alone."""
    return f"""
    .region {{ column-count: 1; }}
    /* Reset user-agent h1 default margin (0.67em). At the 72pt masthead
       title size that margin would balloon to 1.3in top + 1.3in bottom,
       which alone makes the measured top region 3in too tall. */
    .region h1 {{ margin: 0 0 2pt; }}
    .region h2 {{ margin: 0; }}
    .masthead {{
        display: flex; align-items: center; justify-content: space-between;
        gap: 12pt; padding: 4pt 0 2pt;
        border-bottom: 1pt solid #000;
    }}
    .masthead .motto, .masthead .weather-corner {{
        flex: 0 0 auto; font-size: 11pt; line-height: 1.25;
    }}
    .masthead .motto {{ text-align: center; }}
    .masthead .motto span {{
        display: inline-block;
        border: 0.5pt solid #000; padding: 8pt 20pt;
        text-align: center; font-style: italic;
        font-family: "Times New Roman", Georgia, serif, {_EMOJI_FAMILY};
    }}
    .masthead .weather-corner {{
        text-align: center;
        font-family: "Times New Roman", Georgia, serif, {_EMOJI_FAMILY};
        max-width: 2.6in; min-width: 1.6in;
        padding: 6pt 8pt; line-height: 1.3;
    }}
    .masthead .weather-corner .weather-title {{
        display: block; font-weight: bold !important;
        letter-spacing: .05em; text-transform: uppercase;
        font-size: 10pt; margin: 0 0 4pt; text-align: center;
    }}
    .masthead .title {{
        flex: 1 1 auto; text-align: center; margin: 0;
        font-family: "{_MASTHEAD_FONT_FAMILY}", "Times New Roman",
                     Georgia, serif;
        font-weight: normal; letter-spacing: 0;
        white-space: nowrap; overflow: visible;
    }}
    .dateline {{
        display: flex; justify-content: space-between; align-items: baseline;
        font-size: 8pt; padding: 2pt 0;
        border-bottom: 0.5pt solid #000;
        letter-spacing: .05em; text-transform: uppercase;
    }}
    .dateline .vol, .dateline .ai-cost {{ flex: 0 0 22%; }}
    .dateline .vol {{ text-align: left; }}
    .dateline .ai-cost {{ text-align: right; text-transform: none; letter-spacing: 0; }}
    .dateline .ai-cost .ai-cost-label {{ font-size: 6pt; color: #555; margin-right: 18pt; }}
    .dateline .ai-cost .ai-cost-value {{ font-size: 11pt; }}
    .dateline .refreshed {{ text-align: right; }}
    .dateline .date {{ flex: 1 1 auto; text-align: center; }}
    """


def headline_title_region_css(title_font_pt: float) -> str:
    """CSS for a one-off measurement pass that renders just the headline's
    h3 title at its target font size. Used to size the title's vertical
    reservation in the box height calc without over-allocating space."""
    return f"""
    .region h3 {{
        margin: 0 0 4pt;
        font-weight: bold;
        font-size: {title_font_pt:.2f}pt;
        line-height: 1.15;
        font-family: "Times New Roman", Georgia, serif;
        overflow-wrap: break-word;
        word-wrap: break-word;
    }}
    """


def headline_body_region_css(col_count: int) -> str:
    """CSS used during the headline-fit pass + natural-height measurement.

    ``column-fill: balance`` (instead of auto) lets us measure the actual
    balanced multicolumn height with a single render — no need to render
    a 1-col version and divide by N. The body's height is determined by
    its content (no ``height: 100%``); WeasyPrint balances the columns
    to that natural height."""
    return f"""
    .region {{
        column-count: {col_count};
        column-gap: 14pt;
        column-rule: none;
        column-fill: balance;
        text-align: justify;
        hyphens: auto;
        -webkit-hyphens: auto;
    }}
    .region p, .region ul, .region li {{
        margin: 0 0 3pt; font-size: 1em; line-height: 1.25;
        font-family: "Times New Roman", Georgia, serif, {_EMOJI_FAMILY};
    }}
    """


def stocks_region_css() -> str:
    return f"""
    .region {{ column-count: 1;
              text-align: center;
              white-space: normal;
              line-height: 1.4;
              border-top: 1pt solid #000;
              padding-top: 4pt; }}
    .region .tooltip {{ display: inline; white-space: nowrap;
        font-family: "Times New Roman", Georgia, serif, {_EMOJI_FAMILY}; }}
    .region .stock-sep {{ display: inline-block;
        padding: 0 8pt; color: #000; font-weight: bold; }}
    """


# ── Top-level builder ─────────────────────────────────────────────────────


def build_pdf_parts(
    linhnews: dict,
    *,
    pdf_calendar_html: str,
    pdf_movies_html: str,
    weather_strip_html: str,
    today: date,
    image_bytes_by_id: dict[int, tuple[bytes, str]] | None = None,
    weather_prose_html: str = "",
    masthead_name: str = "Linh",
    vol_number: int = 1,
    ai_cost_usd: float = 0.0,
) -> PdfParts:
    """Slice ``linhnews`` into the five region fragments the per-region
    fit pass will operate on.

    The returned ``news_bands`` is:
      * ``[]``                    when no sections were supplied,
      * ``[(html, words)]``       for 0 or 1 section (caller decides what
                                  to do with a 1-section payload — the new
                                  pipeline skips PDF generation entirely
                                  per user spec),
      * ``[(upper, w_u), (lower, w_l)]`` when ≥ 2 sections.
    """
    weather_inner = _format_weather_for_pdf(weather_strip_html, weather_prose_html)
    title_text = f"The {masthead_name} Times"
    title_pt = max(36, min(72, 900 // max(len(title_text), 1)))

    top_inner = _render_top_inner(
        today=today,
        weather_inner=weather_inner,
        masthead_name=masthead_name,
        title_pt=title_pt,
        vol_number=vol_number,
        ai_cost_usd=ai_cost_usd,
    )

    sections = [s for s in (linhnews.get("sections") or []) if s.get("key")]
    section_count = len(sections)

    upper_slice: list[dict] = []
    lower_slice: list[dict] = []
    if section_count >= 2:
        k = _pick_split_index(sections) or 1
        upper_slice = sections[:k]
        lower_slice = sections[k:]
    elif section_count == 1:
        upper_slice = sections

    # Headline detection. When present, ``app.pdf`` handles the upper-band
    # render itself (after fitting headline geometry), so we pass it raw
    # section slices rather than pre-rendered HTML for the upper band.
    headline = linhnews.get("headline") or {}
    has_headline = bool(headline.get("text"))
    headline_image_bytes: bytes | None = None
    headline_image_mime: str | None = None
    headline_image_aspect: float | None = None
    headline_body_html = ""
    headline_title = ""
    headline_word_count = 0
    if has_headline:
        headline_title = headline.get("title", "") or ""
        headline_body_html = text_to_html(headline.get("text", "") or "")
        headline_word_count = (
            len((headline_title or "").split())
            + len((headline.get("text") or "").split())
        )
        image_id = headline.get("image_id")
        if image_id and image_bytes_by_id is not None:
            entry = image_bytes_by_id.get(int(image_id))
            if entry is not None:
                headline_image_bytes, headline_image_mime = entry
                try:
                    import io as _io

                    from PIL import Image
                    with Image.open(_io.BytesIO(headline_image_bytes)) as _img:
                        if _img.height > 0:
                            headline_image_aspect = _img.width / _img.height
                except Exception:  # noqa: BLE001
                    log.exception("Could not decode headline image for aspect ratio")
                    headline_image_aspect = None

    bands: list[tuple[str, int]] = []
    if has_headline:
        # Lower band rendered as usual; upper band ALSO pre-rendered so
        # we have a fallback if the headline fit fails in ``app.pdf``.
        # When fit succeeds, ``app.pdf`` ignores news_bands[0] and renders
        # the L-layout from upper_sections instead.
        upper_html = _render_news_band(upper_slice, image_bytes_by_id=image_bytes_by_id)
        upper_words = sum(_section_word_count(s) for s in upper_slice)
        if lower_slice:
            lower_html = _render_news_band(lower_slice, image_bytes_by_id=image_bytes_by_id)
            lower_words = sum(_section_word_count(s) for s in lower_slice)
            bands = [(upper_html, upper_words), (lower_html, lower_words)]
        elif upper_slice:
            bands = [(upper_html, upper_words)]
    else:
        if section_count >= 2:
            upper_html = _render_news_band(upper_slice, image_bytes_by_id=image_bytes_by_id)
            lower_html = _render_news_band(lower_slice, image_bytes_by_id=image_bytes_by_id)
            upper_words = sum(_section_word_count(s) for s in upper_slice)
            lower_words = sum(_section_word_count(s) for s in lower_slice)
            bands = [(upper_html, upper_words), (lower_html, lower_words)]
        elif section_count == 1:
            single_html = _render_news_band(sections, image_bytes_by_id=image_bytes_by_id)
            bands = [(single_html, _section_word_count(sections[0]))]
        else:
            bands = []

    rail_inner = _render_rail_inner(pdf_calendar_html, pdf_movies_html)
    stocks_inner = _render_stocks_inner(linhnews.get("stocks") or [])

    return PdfParts(
        top_inner_html=top_inner,
        news_bands=bands,
        rail_inner_html=rail_inner,
        stocks_inner_html=stocks_inner,
        font_face_css=_build_font_face_css(),
        masthead_title_pt=title_pt,
        section_count=section_count,
        has_headline=has_headline,
        headline_title=headline_title,
        headline_body_html=headline_body_html,
        headline_word_count=headline_word_count,
        headline_image_bytes=headline_image_bytes,
        headline_image_mime=headline_image_mime,
        headline_image_aspect=headline_image_aspect,
        upper_sections=upper_slice if has_headline else None,
        lower_sections=lower_slice if has_headline else None,
        image_bytes_by_id=image_bytes_by_id if has_headline else None,
    )


# ── Final assembly (consumed by app.pdf.html_to_pdf_ex) ──────────────────


@dataclass
class RegionDims:
    """Fixed-size box, in inches, for one region of the final layout."""

    width_in: float
    height_in: float


@dataclass
class AssemblyLayout:
    """Geometry + font sizes for the final assembled PDF, decided by the
    per-region fit pass in ``app.pdf``.

    All widths/heights in inches; font sizes in pt. The lower band may
    be ``None`` when the news payload is a single band (single section
    case is skipped before reaching the assembler, but we keep this
    optional for an eventual 0-section chrome-only PDF).
    """

    page_w_in: float
    page_h_in: float
    margin_in: float
    top_h_in: float
    stocks_h_in: float
    rail_w_in: float
    content_gap_in: float
    upper_h_in: float
    lower_h_in: float | None
    upper_font_pt: float
    lower_font_pt: float | None
    rail_font_pt: float
    top_font_pt: float = 11.0
    stocks_font_pt: float = 10.0
    masthead_name: str = "Linh"
    masthead_title_pt: int = 72

    # ── Headline L-layout (only used when PdfParts.has_headline). ──────
    # When headline_h_in > 0, the upper band region is replaced by:
    #   .upper-top (row flex, height = headline_h_in)
    #     ├── .headline-box  (column-count = headline_col_span)
    #     └── .news-u-right  (column-count = 4 − headline_col_span)
    #   .news-u-bottom (block, column-count: 4, height = upper_h - headline_h)
    # In other words, .upper-top + .news-u-bottom replace .news-u as
    # direct children of .news-stack — same depth of flex nesting as
    # the existing news-u / news-l pair.
    headline_h_in: float = 0.0
    headline_box_w_in: float = 0.0
    headline_col_span: int = 0
    headline_image_w_in: float = 0.0
    headline_image_h_in: float = 0.0
    headline_image_data_uri: str = ""
    headline_title: str = ""
    headline_body_html: str = ""
    headline_body_font_pt: float = 0.0
    headline_title_font_pt: float = 0.0
    headline_body_col_h_in: float = 0.0
    upper_right_html: str = ""
    upper_right_font_pt: float = 0.0
    upper_bottom_html: str = ""
    upper_bottom_font_pt: float = 0.0

    @property
    def inner_w_in(self) -> float:
        return self.page_w_in - 2 * self.margin_in

    @property
    def inner_h_in(self) -> float:
        return self.page_h_in - 2 * self.margin_in

    @property
    def mid_h_in(self) -> float:
        return self.inner_h_in - self.top_h_in - self.stocks_h_in

    @property
    def news_w_in(self) -> float:
        return self.inner_w_in - self.rail_w_in - self.content_gap_in

    @property
    def news_u_right_w_in(self) -> float:
        """Width of the right pane (column 4 of the 4-col newspaper grid,
        full upper-band height).

        The 12pt safety margin absorbs WeasyPrint's tendency to render
        text spans a few pt past the declared flex-item width (which
        ``overflow: hidden`` then clips mid-letter), plus accounts for
        the 7pt padding-left + 0.5pt border-left of the pane itself."""
        safety_pt = 12.0
        return (
            self.news_w_in
            - self.headline_box_w_in
            - self.content_gap_in
            - safety_pt / 72.0
        )


def _news_u_flex_rule(layout: AssemblyLayout, use_headline_layout: bool) -> str:
    """When a headline is present, .news-u is a row flex container
    holding .upper-left + .upper-right side by side. Without a headline
    it's a plain block holding the legacy multicolumn upper-band flow."""
    if not use_headline_layout:
        return ""
    gap_pt = layout.content_gap_in * 72
    return (
        f"display: flex; flex-direction: row; gap: {gap_pt:.1f}pt; "
        "align-items: stretch;"
    )


def assemble_final_html(parts: PdfParts, layout: AssemblyLayout) -> str:
    """Compose the final one-page document. Each region carries its
    pre-fitted font size; WeasyPrint just lays them out at their reserved
    positions (no further fitting)."""

    upper_inner = parts.news_bands[0][0] if parts.news_bands else ""
    lower_inner = parts.news_bands[1][0] if len(parts.news_bands) > 1 else ""
    use_headline_layout = parts.has_headline and layout.headline_h_in > 0

    common = f"""
    {parts.font_face_css}
    @page {{ size: {layout.page_w_in:.3f}in {layout.page_h_in:.3f}in;
             margin: {layout.margin_in:.3f}in; }}
    html, body {{ margin: 0; padding: 0;
                  font-family: "Times New Roman", Georgia, serif; }}
    body {{ line-height: 1.15; }}
    hr {{ display: none; }}
    img {{ max-width: 100%; }}
    """

    # We compose region CSS by replacing the ``.region`` selector with the
    # band-specific class so the same rules can be reused unchanged.
    def _scope(css: str, class_name: str) -> str:
        return css.replace(".region", f".{class_name}")

    top_css = _scope(top_region_css(), "top")
    upper_css = _scope(news_region_css(), "news-u")
    lower_css = _scope(news_region_css(), "news-l")
    rail_css = _scope(rail_region_css(), "rail")
    stocks_css = _scope(stocks_region_css(), "stocks-footer")
    # L-layout right pane (column-count = 4 − headline_col_span) and
    # below-headline pane (column-count = 4). Both reuse news_region_css
    # with a substituted column-count.
    upper_right_css = ""
    upper_bottom_css = ""
    if use_headline_layout:
        # Right pane is a tall single-column flow (column 4 of the
        # newspaper grid), full upper-band height. ``column-count: 1``
        # keeps news_region_css's typography rules but drops the
        # multicolumn flow.
        upper_right_css = _scope(
            news_region_css().replace(
                f"column-count: {_FLOW_COLUMNS};",
                "column-count: 1; column-fill: auto;",
            ),
            "upper-right",
        )
        # Below-headline pane uses the same column count as the headline
        # (3) so news flowing beneath the box lines up visually.
        upper_bottom_css = _scope(
            news_region_css().replace(
                f"column-count: {_FLOW_COLUMNS}",
                f"column-count: {layout.headline_col_span}",
            ),
            "upper-bottom",
        )

    # Per-region absolute placement. The middle row uses flexbox: the news
    # stack flexes wide, the rail is fixed width. Within the news stack
    # the two bands are stacked vertically with explicit heights.
    # Layout uses explicit ``height`` (NOT just flex-basis) on every
    # fixed-height region, because WeasyPrint's flex implementation lets
    # flex items grow to their content size when only flex-basis is set —
    # which would push a region past its allocation and overflow the
    # one-page contract. ``box-sizing: border-box`` is set on regions
    # that carry borders/padding so the chosen height includes that chrome.
    layout_css = f"""
    .top {{ height: {layout.top_h_in:.3f}in;
            max-height: {layout.top_h_in:.3f}in;
            overflow: hidden;
            box-sizing: border-box; }}
    .content {{ display: flex; gap: {layout.content_gap_in * 72:.1f}pt;
                align-items: stretch;
                height: {layout.mid_h_in:.3f}in;
                max-height: {layout.mid_h_in:.3f}in;
                overflow: hidden;
                box-sizing: border-box; }}
    .news-stack {{ flex: 0 0 {layout.news_w_in:.3f}in;
                   display: flex; flex-direction: column;
                   min-width: 0;
                   width: {layout.news_w_in:.3f}in;
                   height: {layout.mid_h_in:.3f}in;
                   max-height: {layout.mid_h_in:.3f}in;
                   overflow: hidden;
                   box-sizing: border-box; }}
    .news-u {{ flex: 0 0 {layout.upper_h_in:.3f}in;
               height: {layout.upper_h_in:.3f}in;
               max-height: {layout.upper_h_in:.3f}in;
               font-size: {layout.upper_font_pt:.2f}pt;
               line-height: 1.15;
               overflow: hidden;
               box-sizing: border-box;
               {_news_u_flex_rule(layout, use_headline_layout)} }}
    .news-l {{ flex: 0 0 {(layout.lower_h_in or 0):.3f}in;
               height: {(layout.lower_h_in or 0):.3f}in;
               max-height: {(layout.lower_h_in or 0):.3f}in;
               font-size: {(layout.lower_font_pt or layout.upper_font_pt):.2f}pt;
               line-height: 1.15;
               overflow: hidden;
               box-sizing: border-box; }}
    /* ── Headline side-by-side layout (when headline is present) ──
       The .news-u flex row contains .upper-left (col-flex with headline
       + below-headline news flow) and .upper-right (full-height single
       column with a vertical separator on its left edge).
       Same 4-ingredient recipe (flex: 0 0 X; explicit width/height;
       min-width: 0; overflow: hidden) applied throughout. */
    .upper-left {{ flex: 0 0 {layout.headline_box_w_in:.3f}in;
                   width: {layout.headline_box_w_in:.3f}in;
                   height: {layout.upper_h_in:.3f}in;
                   max-height: {layout.upper_h_in:.3f}in;
                   display: flex; flex-direction: column;
                   min-width: 0;
                   overflow: hidden;
                   box-sizing: border-box; }}
    .headline-box {{ flex: 0 0 {layout.headline_h_in:.3f}in;
                     width: {layout.headline_box_w_in:.3f}in;
                     height: {layout.headline_h_in:.3f}in;
                     min-width: 0;
                     border: 1pt solid #000;
                     /* Asymmetric: lighter padding at bottom so the body's
                        last column doesn't leave a ragged gap above the
                        border. */
                     padding: 8pt 8pt 3pt 8pt;
                     margin-bottom: 6pt;
                     box-sizing: border-box;
                     overflow: hidden;
                     font-size: {layout.headline_body_font_pt:.2f}pt;
                     line-height: 1.25; }}
    .headline-box .hero-title {{ margin: 0 0 4pt;
                                 font-weight: bold;
                                 font-size: {layout.headline_title_font_pt:.2f}pt;
                                 line-height: 1.15;
                                 font-family: "Times New Roman", Georgia, serif; }}
    .headline-box .hero-image {{ display: block; margin: 0 auto 6pt;
                                 /* w/h pinned inline below — no CSS scaling */ }}
    .headline-box .hero-body {{ column-gap: 14pt;
                                column-rule: none;
                                column-fill: balance;
                                text-align: justify;
                                hyphens: auto;
                                -webkit-hyphens: auto;
                                font-family: "Times New Roman", Georgia, serif; }}
    .headline-box .hero-body p,
    .headline-box .hero-body ul,
    .headline-box .hero-body li {{ margin: 0 0 3pt; }}
    .upper-bottom {{ flex: 1 1 auto;
                     width: {layout.headline_box_w_in:.3f}in;
                     height: {(layout.upper_h_in - layout.headline_h_in - 6/72):.3f}in;
                     max-height: {(layout.upper_h_in - layout.headline_h_in - 6/72):.3f}in;
                     min-width: 0;
                     font-size: {layout.upper_bottom_font_pt:.2f}pt;
                     line-height: 1.15;
                     overflow: hidden;
                     box-sizing: border-box; }}
    .upper-right {{ flex: 0 0 {layout.news_u_right_w_in:.3f}in;
                    width: {layout.news_u_right_w_in:.3f}in;
                    height: {layout.upper_h_in:.3f}in;
                    max-height: {layout.upper_h_in:.3f}in;
                    min-width: 0;
                    /* Vertical separator between left/right panes. */
                    border-left: 0.5pt solid #999;
                    padding-left: 7pt;
                    font-size: {layout.upper_right_font_pt:.2f}pt;
                    line-height: 1.15;
                    overflow: hidden;
                    box-sizing: border-box; }}
    /* Double horizontal-rule separator between the upper and lower bands.
       Top line is twice as thick as the bottom (1.5pt vs 0.75pt), with a
       small gap drawn by the element's padding. The whole element is a
       zero-content-height block whose total stacked height is
       1.5 + 1.5 + 0.75 = 3.75pt, plus 4pt of margin-bottom giving the
       lower band a breathing space before its content begins. */
    .band-divider {{ height: 0;
                     border-top: 1.5pt solid #000;
                     border-bottom: 0.75pt solid #000;
                     padding: 1.5pt 0 0;
                     margin: 0 0 4pt;
                     box-sizing: content-box;
                     flex: 0 0 auto; }}
    aside.rail {{ flex: 0 0 {layout.rail_w_in:.3f}in;
                  width: {layout.rail_w_in:.3f}in;
                  height: {layout.mid_h_in:.3f}in;
                  max-height: {layout.mid_h_in:.3f}in;
                  padding-left: 8pt;
                  border-left: 0.5pt solid #999;
                  font-size: {layout.rail_font_pt:.2f}pt;
                  line-height: 1.2;
                  overflow: hidden;
                  box-sizing: border-box; }}
    .stocks-footer {{ height: {layout.stocks_h_in:.3f}in;
                      max-height: {layout.stocks_h_in:.3f}in;
                      font-size: {layout.stocks_font_pt:.2f}pt;
                      overflow: hidden;
                      box-sizing: border-box; }}
    """

    parts_html: list[str] = [
        "<!DOCTYPE html>",
        '<html><head><meta charset="utf-8">',
        "<style>",
        common,
        top_css,
        upper_css,
        lower_css,
        rail_css,
        stocks_css,
        upper_right_css,
        upper_bottom_css,
        layout_css,
        "</style></head><body>",
        f'<div class="top">{parts.top_inner_html}</div>',
    ]

    has_news = (
        bool(upper_inner.strip() or lower_inner.strip())
        or use_headline_layout
    )
    has_rail = bool(parts.rail_inner_html.strip())
    if has_news or has_rail:
        parts_html.append('<div class="content">')
        if has_news:
            parts_html.append('<div class="news-stack">')
            if use_headline_layout:
                # Side-by-side: .news-u is a flex row holding .upper-left
                # (col flex with headline + below) and .upper-right (tall
                # single column with vertical separator).
                img_html = ""
                if layout.headline_image_data_uri and layout.headline_image_w_in > 0:
                    img_html = (
                        f'<img class="hero-image" '
                        f'src="{layout.headline_image_data_uri}" '
                        f'style="width:{layout.headline_image_w_in:.3f}in;'
                        f'height:{layout.headline_image_h_in:.3f}in;" '
                        f'alt="" />'
                    )
                body_h_style = (
                    f"height:{layout.headline_body_col_h_in:.3f}in;"
                    if layout.headline_body_col_h_in > 0
                    else ""
                )
                hero_inner = (
                    f"{img_html}"
                    f'<h3 class="hero-title">{_esc(layout.headline_title)}</h3>'
                    f'<div class="hero-body" '
                    f'style="column-count:{layout.headline_col_span};{body_h_style}">'
                    f"{layout.headline_body_html}"
                    f"</div>"
                )
                parts_html.append(
                    f'<div class="news-u">'
                    f'<div class="upper-left">'
                    f'<div class="headline-box">{hero_inner}</div>'
                    f'<div class="upper-bottom">{layout.upper_bottom_html}</div>'
                    f"</div>"
                    f'<div class="upper-right">{layout.upper_right_html}</div>'
                    f"</div>"
                )
            else:
                parts_html.append(f'<div class="news-u">{upper_inner}</div>')
            if lower_inner.strip() or layout.lower_h_in:
                parts_html.append('<div class="band-divider"></div>')
                parts_html.append(f'<div class="news-l">{lower_inner}</div>')
            parts_html.append("</div>")
        if has_rail:
            parts_html.append(f'<aside class="rail">{parts.rail_inner_html}</aside>')
        parts_html.append("</div>")

    if parts.stocks_inner_html.strip():
        parts_html.append(f'<footer class="stocks-footer">{parts.stocks_inner_html}</footer>')

    parts_html.append("</body></html>")
    return "".join(parts_html)


# ── Single-region wrapper used by the fit primitive in app.pdf ────────────


def build_single_region_html(
    inner_html: str,
    *,
    width_in: float,
    height_in: float,
    font_pt: float,
    region_css: str,
    font_face_css: str,
) -> str:
    """Wrap a region's inner HTML in a minimal page sized exactly to the
    region (margin: 0), so the fit primitive can measure overflow against
    the same width/height the region will occupy in the final document.

    ``region_css`` is one of ``news_region_css`` / ``rail_region_css`` /
    ``top_region_css`` / ``stocks_region_css`` (the selector is always
    ``.region`` inside the helper, so we don't need to rescope here).
    """
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
        + font_face_css
        + f" @page {{ size: {width_in:.3f}in {height_in:.3f}in; margin: 0; }}"
        + " html, body { margin: 0; padding: 0;"
        + ' font-family: "Times New Roman", Georgia, serif; }'
        + f" body {{ font-size: {font_pt:.2f}pt; line-height: 1.15; }}"
        + " img { max-width: 100%; }"
        + " hr { display: none; }"
        + region_css
        + "</style></head><body>"
        + f'<div class="region">{inner_html}</div>'
        + "</body></html>"
    )


def build_measure_only_html(
    inner_html: str,
    *,
    width_in: float,
    font_pt: float,
    region_css: str,
    font_face_css: str,
    measure_h_in: float = 30.0,
) -> str:
    """Wrap a region in a very tall page (``measure_h_in`` inches) so the
    content lays out without vertical overflow. The fit primitive then
    measures the deepest box position and converts to the natural height
    the region wants. Used for the masthead/dateline + stocks pre-pass
    where heights are not chosen — they are measured."""
    return build_single_region_html(
        inner_html,
        width_in=width_in,
        height_in=measure_h_in,
        font_pt=font_pt,
        region_css=region_css,
        font_face_css=font_face_css,
    )
