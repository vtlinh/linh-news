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

from bs4 import BeautifulSoup, NavigableString, Tag

log = logging.getLogger(__name__)


_CAL_PLACEHOLDER = "<!-- CALENDAR_PLACEHOLDER -->"
_MOV_PLACEHOLDER = "<!-- MOVIES_PLACEHOLDER -->"

_INTERACTIVE_TAGS = ("button", "script", "form", "input", "select", "textarea")
_STRIP_CLASSES = (
    "sources", "sources-popup", "tooltip-popup", "tooltip",
    "hide-btn", "hide-button",
)
_INTERACTIVE_ATTRS = ("onclick", "onfocus", "onblur", "onmouseover", "onload")


def _format_date(d: date) -> str:
    """Cross-platform 'Saturday, May 2, 2026'."""
    # %-d / %#d aren't portable; build manually.
    return d.strftime("%A, %B ") + str(d.day) + d.strftime(", %Y")


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
    """4-6 columns per the broadsheet rule, sized to flow density."""
    w = _word_count(flow_html)
    if w < 1200:
        return 4
    if w > 3500:
        return 6
    return 5


def build(
    body_html: str,
    *,
    pdf_calendar_html: str,
    pdf_movies_html: str,
    today: date,
) -> str:
    """Build the print-styled HTML document for WeasyPrint.

    ``body_html`` is the LLM's screen-HTML body (post-``_strip_document_wrapper``).
    ``pdf_calendar_html`` and ``pdf_movies_html`` are server-rendered blocks
    that replace the literal placeholder comments inside the body.
    """
    # 1. Substitute placeholders before parsing (they live in HTML comments).
    body_html = body_html.replace(_CAL_PLACEHOLDER, pdf_calendar_html or "")
    body_html = body_html.replace(_MOV_PLACEHOLDER, pdf_movies_html or "")

    soup = BeautifulSoup(body_html, "html.parser")
    _strip_interactive(soup)

    weather_inner = _inner_html(soup.select_one("div.weather-strip"))

    # The rail's first <section> is stocks. Lift it verbatim so the green/red
    # spans the LLM produced for the screen carry over to the PDF.
    rail = soup.select_one("aside.rail")
    stocks_inner = ""
    sidebar_inner = ""
    if rail is not None:
        rail_sections = rail.find_all("section", recursive=False)
        if rail_sections:
            stocks_inner = _inner_html(rail_sections[0])
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

    style_block = f"""
    .masthead {{ text-align:center; font-weight:bold;
                 font-family:"Times New Roman", Georgia, serif; }}
    .dateline {{ text-align:center; font-size:8pt; padding:2pt 0;
                 border-bottom:0.5pt solid #000;
                 letter-spacing:.05em; text-transform:uppercase; }}
    .weather-row {{ font-size:8pt; padding:2pt 0;
                    border-bottom:0.5pt solid #999;
                    display:block; width:100%; }}
    .stocks-row {{ font-size:7pt; padding:2pt 0;
                   border-bottom:0.5pt solid #999;
                   display:block; width:100%; }}
    .stocks-row h2 {{ display:none; }}
    .flow {{ column-count:{n_cols}; column-gap:14pt;
             column-rule:0.5pt solid #999; margin-top:6pt; }}
    .flow > section {{ break-inside: avoid; }}
    aside.rail {{ margin-top:6pt; }}
    aside.rail > section {{ break-inside: avoid; margin-bottom:4pt; }}
    """

    parts = [
        "<!DOCTYPE html>",
        '<html><head><meta charset="utf-8">',
        f"<style>{style_block}</style>",
        "</head><body>",
        '<h1 class="masthead">The Linh Times</h1>',
        f'<div class="dateline">{dateline}</div>',
    ]
    if weather_inner.strip():
        parts.append(f'<div class="weather-row">{weather_inner}</div>')
    if stocks_inner.strip():
        parts.append(f'<div class="stocks-row">{stocks_inner}</div>')
    if flow_html.strip():
        parts.append(f'<div class="flow">{flow_html}</div>')
    if sidebar_inner.strip():
        parts.append(f'<aside class="rail">{sidebar_inner}</aside>')
    parts.append("</body></html>")
    return "".join(parts)
