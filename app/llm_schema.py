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

# Section identity is per-user now and lives in ``UserSettings.sections_json``;
# there are no hardcoded section keys/titles/topic-hints in this module
# anymore. The JSON schema below accepts any string ``key`` so the LLM can
# echo whatever the user configured.

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


class Subsection(TypedDict, total=False):
    title: str
    text: str
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


class LinhNews(TypedDict, total=False):
    # Optional — only set when at least one user section is marked
    # ``can_be_headline`` and the LLM produced a top-of-front-page story.
    # Same shape as a regular ``Subsection`` (reuses ``_SUBSECTION_SCHEMA``).
    headline: Subsection
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
            "description": (
                "Stable identifier — echo back the section key from the prompt's "
                "section list verbatim."
            ),
        },
        "title": {
            "type": "string",
            "description": (
                "Display title including emoji (use the title from the section "
                "list verbatim)."
            ),
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
        "headline": {
            **_SUBSECTION_SCHEMA,
            "description": (
                "Optional. Top-of-front-page deep dive on a SINGLE story drawn "
                "from one of the headline-eligible sections listed in the "
                "prompt. Only include this field when the prompt asks for a "
                "headline. The body must be at least 2x the length of a "
                "normal subsection body and cover only that one story — no "
                "'in other news', no 'meanwhile', no roundup framing."
            ),
        },
        "sections": {
            "type": "array",
            "description": (
                "One entry per section listed in the prompt's "
                "'Required sections' block, in the same order. Echo each "
                "section's `key` and `title` verbatim from the prompt."
            ),
            "items": _SECTION_SCHEMA,
        },
        "stocks": {
            "type": "array",
            "description": (
                "One entry per watchlist symbol, in the same order. "
                "Empty array if the watchlist is empty."
            ),
            "items": _STOCK_SCHEMA,
        },
    },
    "required": ["sections", "stocks"],
    "additionalProperties": False,
}


# Schema used for the single-shot headline re-roll when the main call omits
# the optional ``headline`` field but the user's settings asked for one.
HEADLINE_REROLL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"headline": _SUBSECTION_SCHEMA},
    "required": ["headline"],
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
