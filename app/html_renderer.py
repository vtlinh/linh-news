"""Render the LinhNews structured response into the screen-side HTML body.

Replaces the LLM's previous "return a full HTML body" responsibility. The
output uses the same class names and DOM shape the existing CSS in
``app/templates/base.html`` and the JS in ``app/templates/viewer.html`` rely
on, so the templates and stylesheet are unchanged:

  ``<!-- WEATHER_PLACEHOLDER -->``  — replaced server-side at view time
  ``<div class="flow">``
      ``<section><h2>title</h2></section>``      ← category header (h2-only)
      ``<section><h3>…</h3>…</section>``         ← story sibling
      ``<section><h3>…</h3>…</section>``         ← story sibling
      …
  ``<aside class="rail">``
      ``<section>… stocks tooltips …</section>``
      ``<!-- CALENDAR_PLACEHOLDER -->``
      ``<!-- MOVIES_PLACEHOLDER -->``
  ``</aside>``

The flat "h2 header followed by sibling h3 stories" pattern is required by
the mobile collapse JS in viewer.html — it tags following sibling sections
as ``.sec-collapsed-follower``.
"""

from __future__ import annotations

import html
from typing import Any

from app.llm_schema import SECTION_KEYS, SECTION_TITLES

WEATHER_PLACEHOLDER = "<!-- WEATHER_PLACEHOLDER -->"
CALENDAR_PLACEHOLDER = "<!-- CALENDAR_PLACEHOLDER -->"
MOVIES_PLACEHOLDER = "<!-- MOVIES_PLACEHOLDER -->"

# Colours for stock % moves; mirrors what news.pr previously asked the LLM
# to inline.
_GAIN_COLOR = "#0a7d1f"
_LOSS_COLOR = "#b00020"


def _esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def _format_text(text: str) -> str:
    """Convert plain-text Subsection.text into safe HTML.

    Rules:
      * HTML-escape everything.
      * Lines starting with ``- `` (dash + space) become ``<li>`` items;
        consecutive bullet lines wrap in a single ``<ul>``.
      * Blank-line-separated chunks become ``<p>`` paragraphs.
    """
    if not text:
        return ""
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    bullets: list[str] = []

    def flush_para() -> None:
        if para:
            out.append(f"<p>{_esc(' '.join(para).strip())}</p>")
            para.clear()

    def flush_bullets() -> None:
        if bullets:
            items = "".join(f"<li>{_esc(b)}</li>" for b in bullets)
            out.append(f"<ul>{items}</ul>")
            bullets.clear()

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            flush_para()
            flush_bullets()
            continue
        if line.lstrip().startswith("- "):
            flush_para()
            bullets.append(line.lstrip()[2:].strip())
            continue
        flush_bullets()
        para.append(line.strip())
    flush_para()
    flush_bullets()
    return "".join(out)


def _render_sources(sources: list[dict]) -> str:
    """Return the trailing ``SOURCES`` element for a subsection.

    Single-source items get a direct anchor; multi-source items get the
    hover popup span.
    """
    sources = [s for s in (sources or []) if s.get("url")]
    if not sources:
        return ""
    if len(sources) == 1:
        s = sources[0]
        return (
            f'<a class="sources" href="{_esc(s["url"])}" '
            f'target="_blank" rel="noopener" '
            f'title="{_esc(s.get("title", ""))}">SOURCES</a>'
        )
    links = "".join(
        f'<a href="{_esc(s["url"])}" target="_blank" rel="noopener">'
        f"{_esc(s.get('title', s['url']))}</a>"
        for s in sources
    )
    return (
        f'<span class="sources" tabindex="0">SOURCES'
        f'<span class="sources-popup">{links}</span></span>'
    )


def _render_image(subsection: dict) -> str:
    image_id = subsection.get("image_id")
    if not image_id:
        return ""
    images = subsection.get("images") or []
    alt = images[0].get("alt", "") if images else ""
    return (
        f'<img class="story-image" src="/edition-image/{int(image_id)}" '
        f'alt="{_esc(alt)}" loading="lazy" />'
    )


def _render_subsection(sub: dict, *, with_image: bool) -> str:
    title = _esc(sub.get("title", ""))
    body = _format_text(sub.get("text", ""))
    image_html = _render_image(sub) if with_image else ""
    sources_html = _render_sources(sub.get("sources", []))
    return f"<h3>{title}</h3>{image_html}{body}{(' ' + sources_html) if sources_html else ''}"


def _render_section(section: dict) -> str:
    """One news section becomes a flat run of <section> siblings: an
    h2-only header section, then one story <section> per subsection. This
    matches the pattern viewer.html's mobile-collapse JS expects."""
    key = section.get("key", "")
    title = _esc(section.get("title", "") or SECTION_TITLES.get(key, key))
    parts: list[str] = [
        f'<section class="news-header" data-key="{_esc(key)}"><h2>{title}</h2></section>'
    ]
    subs = section.get("subsections") or []
    for idx, sub in enumerate(subs):
        # Show one image per news section: the first subsection's first image.
        with_image = idx == 0
        parts.append(
            f'<section class="news-story" data-key="{_esc(key)}" '
            f'data-idx="{idx}">{_render_subsection(sub, with_image=with_image)}'
            f"</section>"
        )
    return "".join(parts)


def _format_pct(p: float) -> str:
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.1f}%"


def _render_stock(stock: dict) -> str:
    ticker = _esc(stock.get("ticker", ""))
    price = _esc(stock.get("price", ""))
    pct = float(stock.get("percent_diff") or 0)
    color = _GAIN_COLOR if pct >= 0 else _LOSS_COLOR
    pct_html = f'<span style="color:{color};">{_esc(_format_pct(pct))}</span>'
    why = stock.get("why_it_moved") or {}
    why_text = _esc(why.get("text", ""))
    sources = [s for s in (why.get("sources") or []) if s.get("url")]
    links = "".join(
        f'<a href="{_esc(s["url"])}" target="_blank" rel="noopener">'
        f"{_esc(s.get('title', s['url']))}</a>"
        for s in sources
    )
    popup_inner = f"<strong>Why it moved:</strong> {why_text}" + (
        '<hr style="margin:6px 0; border:0; border-top:1px solid #ddd;">' + links if links else ""
    )
    return (
        f'<span class="tooltip" tabindex="0">'
        f"{ticker} {price} {pct_html}"
        f'<span class="tooltip-popup">{popup_inner}</span>'
        f"</span>"
    )


def _render_stocks_section(stocks: list[dict]) -> str:
    if not stocks:
        return ""
    rows = "".join(_render_stock(s) for s in stocks)
    return f'<section class="stocks-section"><h2>📈 Stocks</h2>{rows}</section>'


def _section_by_key(sections: list[dict]) -> dict[str, dict]:
    return {s.get("key"): s for s in (sections or []) if s.get("key")}


def render_edition_html(linhnews: dict[str, Any]) -> str:
    """Return the full screen-side body HTML for an edition.

    Sections are emitted in the canonical ``SECTION_KEYS`` order. Sections
    the model omitted are simply skipped (the generator's missing-section
    re-roll fills them in before this is called).
    """
    by_key = _section_by_key(linhnews.get("sections") or [])
    flow_inner = "".join(_render_section(by_key[k]) for k in SECTION_KEYS if k in by_key)
    stocks_html = _render_stocks_section(linhnews.get("stocks") or [])

    rail_parts: list[str] = []
    if stocks_html:
        rail_parts.append(stocks_html)
    rail_parts.append(CALENDAR_PLACEHOLDER)
    rail_parts.append(MOVIES_PLACEHOLDER)

    return (
        f"{WEATHER_PLACEHOLDER}"
        f'<div class="flow">{flow_inner}</div>'
        f'<aside class="rail">{"".join(rail_parts)}</aside>'
    )


# ── Helpers reused by the PDF renderer ───────────────────────────────────


# Re-exported so app.pdf_renderer can share the markdown-bullet conversion
# without depending on the screen-only sources/tooltip helpers.
def text_to_html(text: str) -> str:  # pragma: no cover - thin wrapper
    return _format_text(text)


def percent_color(pct: float) -> str:
    return _GAIN_COLOR if pct >= 0 else _LOSS_COLOR


def format_percent(pct: float) -> str:
    return _format_pct(pct)


# Internal exports for tests.
__all__ = [
    "render_edition_html",
    "text_to_html",
    "percent_color",
    "format_percent",
    "WEATHER_PLACEHOLDER",
    "CALENDAR_PLACEHOLDER",
    "MOVIES_PLACEHOLDER",
]
