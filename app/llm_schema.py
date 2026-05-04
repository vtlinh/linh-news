"""Structured-output contract between the LLM and the renderer.

The LLM returns a single ``LinhNews`` JSON object via the ``return_edition``
tool. The server then renders both the HTML page and the PDF from that
structure — the LLM never produces HTML.

Design notes
------------
* ``Section.key`` lets the server detect missing required sections and re-roll
  them in a focused follow-up call.
* ``Subsection.text`` is plain text. Lines beginning with ``- `` (dash + space)
  become ``<li>`` bullets at render time. No HTML is permitted in ``text``.
* ``Subsection.images`` is 0..N candidate URLs from web_search (landscape
  preferred). The server picks one at random, downloads, resizes to ≤400px wide,
  and persists the bytes in ``subsection_images``.
* ``Stock.percent_diff`` is a signed float (e.g. ``-1.4``). The renderer applies
  the green/red colour rules (``#0a7d1f`` for ≥0, ``#b00020`` for <0).
"""

from __future__ import annotations

from typing import Any, TypedDict

# ── Required section keys (in flow order). The LLM must return one Section
# per key; missing keys trigger a single-section re-roll. ────────────────────

SECTION_KEYS: list[str] = [
    "global",
    "us",
    "njny",
    "dorch",
    "finance",
    "tech",
    "ai",
    "ukraine",
]

SECTION_TITLES: dict[str, str] = {
    "global": "🌍 Top Global Political News",
    "us": "🇺🇸 Top US Political News",
    "njny": "🗽 Top NJ / NY News",
    "dorch": "🏫 Dorchester Elementary School News & Events",
    "finance": "💰 Top Financial News",
    "tech": "💻 Top Tech News",
    "ai": "🤖 AI News",
    "ukraine": "🇺🇦 Ukraine News",
}

SECTION_TOPIC_HINTS: dict[str, str] = {
    "global": "Major world / international developments. Cite reputable outlets "
    "(Reuters, AP, BBC, etc.).",
    "us": "Major US government / political developments.",
    "njny": "Top New Jersey / New York regional news.",
    "dorch": "Top Dorchester Elementary School (Woodcliff Lake, NJ) news plus "
    "upcoming events. Always check https://www.wclpfa.com/WlL/index.cfm. "
    "Use the authoritative DORCHESTER_CALENDAR_EVENTS dates verbatim — do "
    "not invent dates.",
    "finance": "Markets, deals, economic data. Exclude individual-stock earnings — "
    "those go in the Stocks section's why-it-moved.",
    "tech": "Product launches, acquisitions, regulatory actions, platform "
    "changes, hardware releases. Exclude AI-specific stories.",
    "ai": "AI news, emphasising coding AI (Claude, Cursor, Copilot, Codex, etc.).",
    "ukraine": "Front-line military situation, diplomatic and peace-process news, "
    "Western aid and sanctions, significant domestic political/economic "
    "developments inside Ukraine, humanitarian stories. Cite Reuters, AP, "
    "BBC, Kyiv Independent, Ukrainska Pravda, etc.",
}

# Minimum subsections per news section. The model is asked for ≥3; the server
# accepts whatever it returns (even 1) but logs a warning below this threshold.
MIN_SUBSECTIONS = 3

# Minimum sources required per stock's ``why_it_moved`` block.
MIN_STOCK_SOURCES = 5


# ── TypedDicts for IDE-time clarity. The LLM contract is the JSON schema
# below; these aliases just make Python call sites readable. ────────────────


class Source(TypedDict):
    url: str
    title: str


class ImageRef(TypedDict, total=False):
    url: str
    alt: str


class Subsection(TypedDict, total=False):
    title: str
    text: str
    images: list[ImageRef]
    sources: list[Source]
    # Set by app.images after download/resize, not by the LLM.
    image_id: int | None


class Section(TypedDict):
    key: str
    title: str
    subsections: list[Subsection]


class WhyItMoved(TypedDict):
    text: str
    sources: list[Source]


class Stock(TypedDict):
    ticker: str
    price: str
    percent_diff: float
    why_it_moved: WhyItMoved


class LinhNews(TypedDict):
    sections: list[Section]
    stocks: list[Stock]


# ── JSON Schema enforced by Anthropic structured-output tool ────────────────

_SOURCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "Direct article URL."},
        "title": {
            "type": "string",
            "description": "Outlet + headline, e.g. 'Reuters: Fed holds rates'.",
        },
    },
    "required": ["url", "title"],
    "additionalProperties": False,
}

_IMAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "Direct image URL (jpg/png/webp). Landscape preferred.",
        },
        "alt": {"type": "string", "description": "Short caption / alt text."},
    },
    "required": ["url"],
    "additionalProperties": False,
}

_SUBSECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "Plain text headline. No HTML.",
        },
        "text": {
            "type": "string",
            "description": (
                "Plain-text body (no HTML). To render bullet points, write each "
                "bullet on its own line prefixed with '- ' (dash space). "
                "Paragraphs are separated by a blank line."
            ),
        },
        "images": {
            "type": "array",
            "description": (
                "0..N candidate image URLs surfaced by web_search. The server "
                "picks one at random (landscape preferred) and downloads it."
            ),
            "items": _IMAGE_SCHEMA,
        },
        "sources": {
            "type": "array",
            "description": "≥1 sources cited for this subsection.",
            "items": _SOURCE_SCHEMA,
            "minItems": 1,
        },
    },
    "required": ["title", "text", "sources"],
    "additionalProperties": False,
}

_SECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "key": {
            "type": "string",
            "enum": SECTION_KEYS,
            "description": "Stable identifier; one of the eight required sections.",
        },
        "title": {
            "type": "string",
            "description": ("Display title including emoji (use the canonical title for the key)."),
        },
        "subsections": {
            "type": "array",
            "description": (
                f"Distinct news items for this section. Aim for ≥{MIN_SUBSECTIONS}; "
                "each item must be dated within 1–2 days of DATE."
            ),
            "items": _SUBSECTION_SCHEMA,
            "minItems": 1,
        },
    },
    "required": ["key", "title", "subsections"],
    "additionalProperties": False,
}

_STOCK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ticker": {"type": "string", "description": "Symbol, e.g. 'GOOG'."},
        "price": {"type": "string", "description": "Rendered verbatim, e.g. '$182.45'."},
        "percent_diff": {
            "type": "number",
            "description": "Signed daily % change, e.g. -1.4 or 2.3.",
        },
        "why_it_moved": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": (
                        "Plain-text explanation. Depth scales with |percent_diff|: "
                        "<2% → one sentence; 2–5% → 2–3 sentences with the catalyst; "
                        ">5% → fuller multi-source rundown."
                    ),
                },
                "sources": {
                    "type": "array",
                    "description": (
                        f"≥{MIN_STOCK_SOURCES} sources, sorted most-trusted first "
                        "(Tier 1: Reuters/Bloomberg/WSJ/FT/AP/SEC; Tier 2: CNBC/"
                        "Barron's/MarketWatch/Economist/Forbes-staff; Tier 3: Yahoo "
                        "Finance/Investopedia/Seeking-Alpha-staff/TechCrunch; "
                        "Tier 4: anything else)."
                    ),
                    "items": _SOURCE_SCHEMA,
                    "minItems": 1,
                },
            },
            "required": ["text", "sources"],
            "additionalProperties": False,
        },
    },
    "required": ["ticker", "price", "percent_diff", "why_it_moved"],
    "additionalProperties": False,
}

EDITION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "description": (
                "Eight required news sections, in this order: "
                + ", ".join(f"{k} ({SECTION_TITLES[k]})" for k in SECTION_KEYS)
                + "."
            ),
            "items": _SECTION_SCHEMA,
        },
        "stocks": {
            "type": "array",
            "description": (
                "One entry per WATCHLIST_STOCKS symbol, in the same order. "
                "Empty array if the watchlist is empty."
            ),
            "items": _STOCK_SCHEMA,
        },
    },
    "required": ["sections", "stocks"],
    "additionalProperties": False,
}


# Schema used for the single-section re-roll (one missing key at a time). The
# LLM returns just the subsection list for that key; the server re-attaches
# the canonical key + title.
SECTION_REROLL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "subsections": {
            "type": "array",
            "items": _SUBSECTION_SCHEMA,
            "minItems": 1,
        },
    },
    "required": ["subsections"],
    "additionalProperties": False,
}
