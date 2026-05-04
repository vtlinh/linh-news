"""Render the print-styled HTML for WeasyPrint directly from the structured
LinhNews response.

Replaces ``app/pdf_html_builder.py`` — there is no longer any HTML to parse.
We emit a deterministic 4-column flow with sep-story / sep-group rules,
server-rendered weather / calendar / movies blocks, and a stocks footer
ribbon.

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
from datetime import date, datetime
from pathlib import Path

from app.html_renderer import format_percent, percent_color, text_to_html
from app.llm_schema import SECTION_KEYS, SECTION_TITLES
from app.settings import LOCAL_TZ, local_now

log = logging.getLogger(__name__)

_MASTHEAD_FONT_PATH = Path(__file__).resolve().parent / "fonts" / "Chomsky.otf"
_MASTHEAD_FONT_FAMILY = "Linh Times Masthead"

_FLOW_COLUMNS = 4


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


def _format_weather_for_pdf(strip_html: str) -> str:
    """Reshape the weather strip's joined text for the PDF masthead corner.

    The strip arrives as ``<div class="weather-strip">…</div>``; we extract
    the inner text and apply the same layout rules the old builder used:
    drop pollen, keep "Wind" merged into the preceding line, bold the
    Now / Today / Tomorrow labels, join with " · ".
    """
    if not strip_html:
        return ""
    m = re.search(
        r'<div\s+class="weather-strip"[^>]*>(.*?)</div\s*>',
        strip_html,
        re.IGNORECASE | re.DOTALL,
    )
    inner = m.group(1) if m else strip_html
    # The screen weather strip wraps its segments in <span class="weather-main">…
    # </span> and tacks on a <span class="weather-refreshed">…</span> badge for
    # the page header. Drop the refreshed badge entirely (the PDF dateline shows
    # its own refreshed timestamp) and unwrap the weather-main container so the
    # subsequent " · "-split sees clean text rather than a half-open <span>.
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


# ── Body builders ──────────────────────────────────────────────────────────


def _render_story_html(
    sub: dict,
    *,
    image_bytes_by_id: dict[int, tuple[bytes, str]] | None,
    with_image: bool,
) -> str:
    title = _esc(sub.get("title", ""))
    body = text_to_html(sub.get("text", ""))  # safe: HTML-escaped + bullets only
    image_html = ""
    if with_image and image_bytes_by_id:
        image_id = sub.get("image_id")
        entry = image_bytes_by_id.get(int(image_id)) if image_id else None
        if entry is not None:
            data, mime = entry
            b64 = base64.b64encode(data).decode("ascii")
            alt_src = (sub.get("images") or [{}])[0].get("alt", "") if sub.get("images") else ""
            image_html = (
                f'<img class="story-image" src="data:{mime};base64,{b64}" alt="{_esc(alt_src)}" />'
            )
    return f"<h3>{title}</h3>{image_html}{body}"


def _render_section_for_pdf(
    section: dict,
    *,
    image_bytes_by_id: dict[int, tuple[bytes, str]] | None,
) -> tuple[str, list[str]]:
    """Return (header_html, [story_html, …]) for a single news section.

    Header is the h2-only wrapper; stories are individual <section> blocks
    with the h3 + body. The caller stitches them together with sep-story /
    sep-group rules between the right pairs. The first story carries the
    section's image (if its first subsection had a successful image fetch).
    """
    key = section.get("key", "")
    title = _esc(section.get("title", "") or SECTION_TITLES.get(key, key))
    header = f'<section class="news-header"><h2>{title}</h2></section>'
    stories: list[str] = []
    for idx, sub in enumerate(section.get("subsections") or []):
        with_image = idx == 0
        story = _render_story_html(
            sub,
            image_bytes_by_id=image_bytes_by_id,
            with_image=with_image,
        )
        stories.append(f'<section class="news-story">{story}</section>')
    return header, stories


def _render_flow(
    sections: list[dict],
    *,
    image_bytes_by_id: dict[int, tuple[bytes, str]] | None,
) -> str:
    """Stitch all sections into the multi-column flow with separators.

    No rule above the first section. Within a section, ``sep-story`` between
    consecutive stories. Between sections, ``sep-group`` before the next
    header.
    """
    by_key = {s.get("key"): s for s in sections if s.get("key")}
    ordered = [by_key[k] for k in SECTION_KEYS if k in by_key]
    parts: list[str] = []
    for i, sec in enumerate(ordered):
        header, stories = _render_section_for_pdf(sec, image_bytes_by_id=image_bytes_by_id)
        if i > 0:
            parts.append('<div class="sep-group"></div>')
        parts.append(header)
        for j, story in enumerate(stories):
            if j > 0:
                parts.append('<div class="sep-story"></div>')
            parts.append(story)
    return "".join(parts)


def _render_stocks_footer(stocks: list[dict]) -> str:
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
    return f'<footer class="stocks-footer">{sep.join(items)}</footer>'


# ── Top-level builder ──────────────────────────────────────────────────────


def build_pdf_html(
    linhnews: dict,
    *,
    pdf_calendar_html: str,
    pdf_movies_html: str,
    weather_strip_html: str,
    today: date,
    refreshed_at: datetime | None = None,
    image_bytes_by_id: dict[int, tuple[bytes, str]] | None = None,
) -> str:
    """Build the print-styled HTML document for WeasyPrint.

    ``image_bytes_by_id`` maps ``subsection_images.id`` → (bytes, mime).
    For each section's first subsection that has an ``image_id`` and a
    matching entry, the renderer embeds the image inline as a data URI.
    """
    weather_inner = _format_weather_for_pdf(weather_strip_html)
    flow_html = _render_flow(
        linhnews.get("sections") or [],
        image_bytes_by_id=image_bytes_by_id,
    )
    stocks_html = _render_stocks_footer(linhnews.get("stocks") or [])

    sidebar_parts: list[str] = []
    if pdf_calendar_html:
        sidebar_parts.append(pdf_calendar_html)
    if pdf_movies_html:
        sidebar_parts.append(pdf_movies_html)
    sidebar_inner = "".join(sidebar_parts)

    if refreshed_at is None:
        refreshed_at = local_now()
    elif refreshed_at.tzinfo is None:
        refreshed_at = refreshed_at.replace(tzinfo=LOCAL_TZ)
    else:
        refreshed_at = refreshed_at.astimezone(LOCAL_TZ)
    tz_abbrev = refreshed_at.tzname() or "EST"
    refreshed_label = f"Refreshed at {refreshed_at.hour:02d}:00 {tz_abbrev}"
    dateline = _format_date(today)
    vol_roman = _roman(_day_of_year(today))
    font_url = _MASTHEAD_FONT_PATH.as_uri()

    # Color-emoji fallback chain: Noto Color Emoji ships in the production
    # Docker image; Segoe UI Emoji and Apple Color Emoji cover Windows/macOS
    # local dev so glyphs like 🇺🇸 / 🌍 / 💰 don't fall through to a
    # monochrome DejaVu fallback.
    _EMOJI_FAMILIES = '"Noto Color Emoji", "Segoe UI Emoji", "Apple Color Emoji", emoji'
    style_block = f"""
    @font-face {{ font-family: "{_MASTHEAD_FONT_FAMILY}";
                  src: url("{font_url}") format("opentype"); }}
    /* News-flow titles + body. !important so the WeasyPrint stylesheet's
       generic h2/h3/p rules in app/pdf.py compose correctly without losing
       weight when font-family changes. */
    .flow section.news-header h2 {{
                 font-weight:bold !important;
                 font-family:"Times New Roman", Georgia, serif, {_EMOJI_FAMILIES}; }}
    .flow section.news-story h3 {{
                 font-weight:bold !important;
                 font-family:"Times New Roman", Georgia, serif, {_EMOJI_FAMILIES}; }}
    .flow section.news-story p,
    .flow section.news-story ul,
    .flow section.news-story li {{
                 font-family:"Times New Roman", Georgia, serif, {_EMOJI_FAMILIES}; }}
    /* Subsection image: cap at one flow-column width. The page geometry
       (15.296in × 0.4in margins, 2.4in rail, 14pt content gap, 14pt
       column-gap × 3, 4 columns) gives ~2.83in per column; 2.6in keeps
       the image safely inside its column even when WeasyPrint resolves
       width:100% against the multi-column container instead of the
       column. */
    .flow section.news-story img.story-image {{
                 display:block;
                 width:100%; max-width:2.6in;
                 height:auto;
                 margin:0 0 4pt; }}
    .masthead {{ display:flex; align-items:center; justify-content:space-between;
                 gap:12pt; padding:4pt 0 2pt;
                 border-bottom:1pt solid #000; }}
    .masthead .motto, .masthead .weather-corner {{
                 flex:0 0 auto; font-size:11pt; line-height:1.25; }}
    .masthead .motto {{ text-align:center; }}
    .masthead .motto span {{ display:inline-block;
                 border:0.5pt solid #000; padding:8pt 20pt;
                 text-align:center;
                 font-style:italic;
                 font-family:"Times New Roman", Georgia, serif,
                              "Noto Color Emoji", "Segoe UI Emoji",
                              "Apple Color Emoji", emoji; }}
    .masthead .weather-corner {{ text-align:center;
                 font-family:"Times New Roman", Georgia, serif,
                              "Noto Color Emoji", "Segoe UI Emoji",
                              "Apple Color Emoji", emoji;
                 max-width:2.6in; min-width:1.6in;
                 padding:6pt 8pt;
                 line-height:1.3; }}
    .masthead .weather-corner .weather-title {{
                 display:block; font-weight:bold;
                 letter-spacing:.05em; text-transform:uppercase;
                 font-size:10pt; margin:0 0 4pt; text-align:center; }}
    .masthead .title {{ flex:1 1 auto; text-align:center; margin:0;
                 font-family:"{_MASTHEAD_FONT_FAMILY}", "Times New Roman",
                              Georgia, serif, "Noto Color Emoji";
                 font-weight:normal; letter-spacing:0;
                 white-space:nowrap; overflow:visible; }}
    .dateline {{ display:flex; justify-content:space-between; align-items:baseline;
                 font-size:8pt; padding:2pt 0;
                 border-bottom:0.5pt solid #000;
                 letter-spacing:.05em; text-transform:uppercase; }}
    .dateline .vol, .dateline .refreshed {{ flex:0 0 22%; }}
    .dateline .vol {{ text-align:left; }}
    .dateline .refreshed {{ text-align:right; }}
    .dateline .date {{ flex:1 1 auto; text-align:center; }}
    .content {{ display:flex; gap:14pt; margin-top:6pt; align-items:stretch; }}
    .flow {{ flex:1 1 auto; column-count:{_FLOW_COLUMNS}; column-gap:14pt;
             column-rule:0.5pt solid #999; }}
    .flow div.sep-story {{
             display:block; text-align:center !important;
             margin-left:0 !important; margin-right:0 !important;
             margin-top:6pt !important; margin-bottom:4pt !important;
             padding:0; height:0;
             break-before:avoid; page-break-before:avoid;
             break-inside:avoid; break-after:auto; }}
    .flow div.sep-story::before {{
             content:""; display:inline-block;
             width:33%; height:0;
             border-top:0.4pt solid #999;
             vertical-align:middle; }}
    .flow div.sep-story + section {{ padding-top:6pt !important; }}
    .flow div.sep-group {{
             display:block; box-sizing:content-box;
             border:0; border-top:0.75pt solid #000;
             width:auto; margin-left:0 !important; margin-right:0 !important;
             margin-top:8pt !important; margin-bottom:4pt !important;
             padding:0; height:0;
             break-before:avoid; page-break-before:avoid;
             break-inside:avoid; break-after:auto; }}
    aside.rail {{ flex:0 0 2.4in; padding-left:8pt;
                  border-left:0.5pt solid #999;
                  font-size:9pt; line-height:1.2; }}
    aside.rail > * + * {{ margin-top:8pt; }}
    .stocks-footer {{ margin-top:8pt; padding:4pt 0 0;
                      border-top:1pt solid #000;
                      font-size:10pt; line-height:1.4;
                      text-align:center; white-space:normal; }}
    .stocks-footer .tooltip {{ display:inline; white-space:nowrap; }}
    .stocks-footer .stock-sep {{ display:inline-block;
                 padding:0 8pt; color:#000; font-weight:bold; }}
    """

    parts = [
        "<!DOCTYPE html>",
        '<html><head><meta charset="utf-8">',
        f"<style>{style_block}</style>",
        "</head><body>",
        '<header class="masthead">',
        '<div class="motto"><span>"All the News<br>That\'s Fit for Linh"</span></div>',
        '<h1 class="title">The Linh Times</h1>',
        '<div class="weather-corner">'
        + (
            f'<span class="weather-title">The Weather</span>{weather_inner}'
            if weather_inner.strip()
            else ""
        )
        + "</div>",
        "</header>",
        '<div class="dateline">'
        f'<span class="vol">VOL. {vol_roman}</span>'
        f'<span class="date">{dateline}</span>'
        f'<span class="refreshed">{refreshed_label}</span>'
        "</div>",
    ]
    if flow_html.strip() or sidebar_inner.strip():
        parts.append('<div class="content">')
        if flow_html.strip():
            parts.append(f'<div class="flow">{flow_html}</div>')
        if sidebar_inner.strip():
            parts.append(f'<aside class="rail">{sidebar_inner}</aside>')
        parts.append("</div>")
    if stocks_html:
        parts.append(stocks_html)
    parts.append("</body></html>")
    return "".join(parts)
