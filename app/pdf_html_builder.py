"""Server-side construction of the print-styled HTML that goes to WeasyPrint.

Replaces the LLM's previous ``pdf_html`` output: we lift the structural
pieces from the body HTML the LLM produced (weather strip, flow sections,
rail's stocks/calendar/movies blocks), strip interactive cruft, substitute
the server-rendered calendar + movies blocks, and assemble a deterministic
12 x 22 in NYT-broadsheet document.

Typography is governed by ``app/pdf.py:_make_css``, so this builder mainly
controls structure and column count.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

log = logging.getLogger(__name__)


_MASTHEAD_FONT_PATH = Path(__file__).resolve().parent / "fonts" / "Chomsky.otf"
_MASTHEAD_FONT_FAMILY = "Linh Times Masthead"

_CAL_PLACEHOLDER = "<!-- CALENDAR_PLACEHOLDER -->"
_MOV_PLACEHOLDER = "<!-- MOVIES_PLACEHOLDER -->"
_WEATHER_PLACEHOLDER = "<!-- WEATHER_PLACEHOLDER -->"

_INTERACTIVE_TAGS = ("button", "script", "form", "input", "select", "textarea", "img")
_STRIP_CLASSES = (
    # NOTE: do NOT strip "tooltip" — the visible ticker text on stock rows
    # lives directly inside <span class="tooltip">; only the inner
    # ".tooltip-popup" should be removed.
    "sources", "sources-popup", "tooltip-popup",
    "hide-btn", "hide-button",
)
_INTERACTIVE_ATTRS = ("onclick", "onfocus", "onblur", "onmouseover", "onload")


def _format_date(d: date) -> str:
    """Cross-platform 'Saturday, May 2, 2026'."""
    # %-d / %#d aren't portable; build manually.
    return d.strftime("%A, %B ") + str(d.day) + d.strftime(", %Y")


def _roman(n: int) -> str:
    """Convert a positive int to Roman numerals (no Unicode overlines)."""
    pairs = [
        (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
        (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
        (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
    ]
    out: list[str] = []
    for v, sym in pairs:
        while n >= v:
            out.append(sym)
            n -= v
    return "".join(out) or "N"


def _day_of_year(d: date) -> int:
    """1 on Jan 1, 365/366 on Dec 31."""
    return (d - date(d.year, 1, 1)).days + 1


def _format_weather_for_pdf(inner: str) -> str:
    """Reshape the weather strip's joined text for the PDF masthead corner.

    Rules:
      * Drop any pollen segment ('· Pollen: …').
      * Keep the wind reading on the same line as 'Now' (don't break it).
      * Render Now / Today / Tomorrow / alerts on separate lines.
      * Each line is wrapped in a nowrap span so it never visually wraps
        inside the narrow masthead corner.
    """
    if not inner:
        return ""
    # Drop pollen entirely (handles '· Pollen: ...' anywhere in the strip).
    inner = re.sub(r"\s*·\s*Pollen[^·]*", "", inner, flags=re.IGNORECASE)
    inner = re.sub(r"^\s*Pollen[^·]*·\s*", "", inner, flags=re.IGNORECASE)
    # Split on the top-level " · " separator the strip builder uses.
    raw_parts = [p.strip() for p in inner.split(" · ") if p.strip()]
    # Re-merge "Wind …" segments back into the preceding line (Now).
    merged: list[str] = []
    for p in raw_parts:
        if p.lower().startswith("wind") and merged:
            merged[-1] = f"{merged[-1]}, {p}"
        else:
            merged.append(p)

    def _bold_label(line: str) -> str:
        # Bold the leading "Now"/"Today"/"Tomorrow" label of each line.
        m = re.match(r"^(Now|Today|Tomorrow)\b", line)
        if not m:
            return line
        label = m.group(1)
        rest = line[m.end():]
        return f"<strong>{label}</strong>{rest}"

    # Single flowing paragraph with " · " separators between Now / Today /
    # Tomorrow / alerts; the box around the weather corner lets the text
    # wrap onto multiple lines naturally instead of being forced to <br>.
    return " · ".join(_bold_label(m) for m in merged)


def _decompose_all(soup: BeautifulSoup, selector: str) -> int:
    n = 0
    for el in soup.select(selector):
        el.decompose()
        n += 1
    return n


def _strip_interactive(soup: BeautifulSoup) -> None:
    for tag_name in _INTERACTIVE_TAGS:
        for el in soup.find_all(tag_name):
            el.decompose()
    for cls in _STRIP_CLASSES:
        for el in soup.select(f".{cls}"):
            el.decompose()
    for el in soup.find_all(True):
        for attr in _INTERACTIVE_ATTRS:
            if attr in el.attrs:
                del el.attrs[attr]


def _inner_html(tag: Tag | None) -> str:
    if tag is None:
        return ""
    return tag.decode_contents()


def _word_count(html: str) -> int:
    return len(re.sub(r"<[^>]+>", " ", html or "").split())


def _column_count(flow_html: str) -> int:
    """Always 4 inner columns of free-flowing copy. The right rail
    (calendar + movies) is a fixed sidebar *outside* this multi-column
    region, so the total visible columns on the page = 5.
    """
    return 4


def build(
    body_html: str,
    *,
    pdf_calendar_html: str,
    pdf_movies_html: str,
    today: date,
    weather_strip_html: str = "",
) -> str:
    """Build the print-styled HTML document for WeasyPrint.

    ``body_html`` is the LLM's screen-HTML body (post-``_strip_document_wrapper``).
    ``pdf_calendar_html``, ``pdf_movies_html`` and ``weather_strip_html`` are
    server-rendered blocks that replace the literal placeholder comments
    inside the body.
    """
    # 1. Substitute placeholders before parsing (they live in HTML comments).
    body_html = body_html.replace(_CAL_PLACEHOLDER, pdf_calendar_html or "")
    body_html = body_html.replace(_MOV_PLACEHOLDER, pdf_movies_html or "")
    body_html = body_html.replace(_WEATHER_PLACEHOLDER, weather_strip_html or "")

    soup = BeautifulSoup(body_html, "html.parser")
    _strip_interactive(soup)

    weather_inner = _format_weather_for_pdf(
        _inner_html(soup.select_one("div.weather-strip"))
    )

    # The rail's first <section> is stocks. Lift it verbatim so the green/red
    # spans the LLM produced for the screen carry over to the PDF.
    rail = soup.select_one("aside.rail")
    stocks_inner = ""
    sidebar_inner = ""
    if rail is not None:
        rail_sections = rail.find_all("section", recursive=False)
        if rail_sections:
            # Drop <br> tags inside the stocks block so the
            # ".tooltip + .tooltip" CSS selector fires (CSS sees <br> as
            # an intervening sibling and breaks the adjacency rule). Also
            # makes the "* " separator + 16pt padding actually render.
            stocks_block = rail_sections[0]
            for br in stocks_block.find_all("br"):
                br.decompose()
            stocks_inner = _inner_html(stocks_block)
        # Anything else in the rail (calendar substitution + movies
        # substitution + any extra sections) becomes the sidebar.
        # We rebuild from rail children minus the first <section>.
        first_section_seen = False
        sidebar_parts: list[str] = []
        for child in rail.children:
            if isinstance(child, NavigableString):
                sidebar_parts.append(str(child))
                continue
            if isinstance(child, Tag) and child.name == "section" and not first_section_seen:
                first_section_seen = True
                continue
            sidebar_parts.append(str(child))
        sidebar_inner = "".join(sidebar_parts).strip()

    flow_sections = soup.select("div.flow > section")
    flow_html = "".join(str(s) for s in flow_sections)
    n_cols = _column_count(flow_html)

    dateline = _format_date(today)
    vol_roman = _roman(_day_of_year(today))
    font_url = _MASTHEAD_FONT_PATH.as_uri()

    style_block = f"""
    @font-face {{ font-family: "{_MASTHEAD_FONT_FAMILY}";
                  src: url("{font_url}"); }}
    /* All three (motto, title, weather) live on a single row. Title is
       elastic in the middle; motto and weather stay sized to their
       content so they're never blocked. */
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
                 font-family:"Times New Roman", Georgia, serif; }}
    /* Weather block: padded, narrow enough to wrap into 2-3 lines on the
       right of the masthead. No outline. */
    .masthead .weather-corner {{ text-align:center;
                 font-family:"Times New Roman", Georgia, serif;
                 max-width:2.6in; min-width:1.6in;
                 padding:6pt 8pt;
                 line-height:1.3; }}
    .masthead .weather-corner .weather-title {{
                 display:block; font-weight:bold;
                 letter-spacing:.05em; text-transform:uppercase;
                 font-size:10pt; margin:0 0 4pt; text-align:center; }}
    .masthead .title {{ flex:1 1 auto; text-align:center; margin:0;
                 font-family:"{_MASTHEAD_FONT_FAMILY}", "Times New Roman",
                              Georgia, serif;
                 font-weight:normal; letter-spacing:0;
                 white-space:nowrap; overflow:visible; }}
    .dateline {{ display:flex; justify-content:space-between; align-items:baseline;
                 font-size:8pt; padding:2pt 0;
                 border-bottom:0.5pt solid #000;
                 letter-spacing:.05em; text-transform:uppercase; }}
    .dateline .vol, .dateline .vol-spacer {{ flex:0 0 22%; }}
    .dateline .vol {{ text-align:left; }}
    .dateline .vol-spacer {{ text-align:right; }}
    .dateline .date {{ flex:1 1 auto; text-align:center; }}
    .content {{ display:flex; gap:14pt; margin-top:6pt;
                align-items:stretch; }}
    .flow {{ flex:1 1 auto; column-count:{n_cols}; column-gap:14pt;
             column-rule:0.5pt solid #999; }}
    .flow > section, .flow > section > article {{
             /* allow free wrapping from one column to the next */ }}
    aside.rail {{ flex:0 0 2.4in; padding-left:8pt;
                  border-left:0.5pt solid #999;
                  font-size:9pt; line-height:1.2; }}
    aside.rail > * + * {{ margin-top:8pt; }}
    .stocks-footer {{ margin-top:8pt; padding:4pt 0 0;
                      border-top:1pt solid #000;
                      font-size:10pt; line-height:1.4;
                      text-align:center; white-space:normal; }}
    /* Drop the "Stocks" heading entirely — the footer is identifiable
       by its border + ticker formatting. */
    .stocks-footer h2 {{ display:none !important; }}
    .stocks-footer > div {{ display:inline; }}
    .stocks-footer .tooltip {{ display:inline;
                 white-space:nowrap; }}
    /* The LLM emits explicit <br> between tickers — hide them so the
       footer reads as a single line. */
    .stocks-footer br {{ display:none; }}
    /* Add an asterisk separator between adjacent ticker spans, with
       8pt of horizontal whitespace on each side (16pt total padding
       between consecutive stocks). */
    .stocks-footer .tooltip + .tooltip::before {{
                 content: " * "; padding:0 8pt;
                 color:#000; font-weight:bold; }}
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
            if weather_inner.strip() else ""
        )
        + '</div>',
        '</header>',
        '<div class="dateline">'
        f'<span class="vol">VOL. {vol_roman}</span>'
        f'<span class="date">{dateline}</span>'
        '<span class="vol-spacer"></span>'
        '</div>',
    ]
    # Main content area: multi-column flow on the left, fixed-width rail
    # (calendar + movies) glued to the right so it always lives in the last
    # visual column rather than spilling to the bottom of the page.
    if flow_html.strip() or sidebar_inner.strip():
        parts.append('<div class="content">')
        if flow_html.strip():
            parts.append(f'<div class="flow">{flow_html}</div>')
        if sidebar_inner.strip():
            parts.append(f'<aside class="rail">{sidebar_inner}</aside>')
        parts.append('</div>')
    # Stocks always anchor the page as a footer ribbon.
    if stocks_inner.strip():
        parts.append(f'<footer class="stocks-footer">{stocks_inner}</footer>')
    parts.append("</body></html>")
    return "".join(parts)
