"""One-shot seed: write the admin's user_settings row from news.pr-derived
defaults. Run once after the 0019 migration:

    uv run python -m scripts.seed_admin_settings

Idempotent — overwrites the row if it already exists.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.db import UserSettings, session_factory

ADMIN_EMAIL = "vtlinh87@gmail.com"
DEFAULT_SUBSECTION_COUNT = 5

# Section list mirrors the table that lived in news.pr. The richer dorch
# guidance (URLs to always check, deadline-flagging, grade-filter wording)
# is split: URLs into ``preferred_sources``, the rest into the description.
_SECTIONS = [
    {
        "key": "global",
        "title": "🌍 Top Global Political News",
        "description": (
            "Major world / international developments. Cite reputable "
            "outlets (Reuters, AP, BBC, etc.)."
        ),
        "subsection_count": DEFAULT_SUBSECTION_COUNT,
        "preferred_sources": [],
        "use_global_sources": True,
    },
    {
        "key": "us",
        "title": "🇺🇸 Top US Political News",
        "description": "Major US government / political developments.",
        "subsection_count": DEFAULT_SUBSECTION_COUNT,
        "preferred_sources": [],
        "use_global_sources": True,
    },
    {
        "key": "njny",
        "title": "🗽 Top NJ / NY News",
        "description": "Top New Jersey / New York regional news.",
        "subsection_count": DEFAULT_SUBSECTION_COUNT,
        "preferred_sources": [],
        "use_global_sources": True,
    },
    {
        "key": "dorch",
        "title": "🏫 Dorchester Elementary School News & Events",
        "description": (
            "Top Dorchester Elementary School (Woodcliff Lake, NJ) news plus "
            "upcoming events. Always check the preferred sources on every run, "
            "in addition to any other relevant sources you find. Surface anything "
            "dated within the next 4 weeks; flag deadlines (registration, "
            "sign-ups, payment due dates). Use the authoritative dates from the "
            "DORCHESTER_CALENDAR_EVENTS list verbatim — do not invent event "
            "dates. Cite the originating URL as a source on each item. "
            "Grade filter: if an item is grade-specific, only include it when "
            "it applies to one of the children's current grades for this "
            "school year, or to the next grade up for the upcoming school year "
            "(e.g. summer/fall transition info). Skip items targeted only at "
            "other grades. School-wide items (no grade tag) are always fair game."
        ),
        "subsection_count": DEFAULT_SUBSECTION_COUNT,
        "preferred_sources": [
            "https://www.wclpfa.com/WlL/index.cfm",
            "https://www.woodcliff-lake.com/",
            "https://des.woodcliff-lake.com/",
        ],
        "use_global_sources": True,
    },
    {
        "key": "finance",
        "title": "💰 Top Financial News",
        "description": (
            "Markets, deals, economic data. Exclude individual-stock earnings "
            "— those go in the Stocks section's why-it-moved."
        ),
        "subsection_count": DEFAULT_SUBSECTION_COUNT,
        "preferred_sources": [],
        "use_global_sources": True,
    },
    {
        "key": "tech",
        "title": "💻 Top Tech News",
        "description": (
            "Product launches, acquisitions, regulatory actions, platform "
            "changes, hardware releases. Exclude AI-specific stories (those "
            "go in the AI section)."
        ),
        "subsection_count": DEFAULT_SUBSECTION_COUNT,
        "preferred_sources": [],
        "use_global_sources": True,
    },
    {
        "key": "ai",
        "title": "🤖 AI News",
        "description": (
            "AI news, emphasising coding AI (Claude, Cursor, Copilot, Codex, etc.)."
        ),
        "subsection_count": DEFAULT_SUBSECTION_COUNT,
        "preferred_sources": [],
        "use_global_sources": True,
    },
    {
        "key": "ukraine",
        "title": "🇺🇦 Ukraine News",
        "description": (
            "Front-line military situation, diplomatic and peace-process news, "
            "Western aid and sanctions, significant domestic political/economic "
            "developments inside Ukraine, humanitarian stories. Cite Reuters, "
            "AP, BBC, Kyiv Independent, Ukrainska Pravda, etc."
        ),
        "subsection_count": DEFAULT_SUBSECTION_COUNT,
        "preferred_sources": [],
        "use_global_sources": True,
    },
]

# Birthdays back-computed from the previous hardcoded anchor: William was
# entering 2nd grade in academic year 2025-2026 (US norm: age 7 at start
# of 2nd grade). Elizabeth, no prior anchor in code — placeholder DOB
# matching Kindergarten in 2025-2026; edit on the Data page if wrong.
_CHILDREN = [
    {"name": "William", "birthday": "2018-09-01"},
    {"name": "Elizabeth", "birthday": "2020-09-01"},
]


def main() -> int:
    Maker = session_factory()
    with Maker() as s:
        row = s.get(UserSettings, ADMIN_EMAIL)
        if row is None:
            row = UserSettings(email=ADMIN_EMAIL)
            s.add(row)
        row.display_name = "Linh"
        row.address = "15 Hunter Ridge, Woodcliff Lake, NJ 07677"
        row.weather_coords = "41.0223,-74.0635"
        row.sections_json = _SECTIONS
        row.children_json = _CHILDREN
        row.updated_at = datetime.now(UTC)
        s.commit()
    print(
        f"Seeded user_settings for {ADMIN_EMAIL}: "
        f"{len(_SECTIONS)} sections, {len(_CHILDREN)} children."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
