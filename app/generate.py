from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import calendar_oauth, claude_client, overlays, pdf
from app.db import Edition, session_factory
from app.settings import get_settings

log = logging.getLogger(__name__)

Slot = Literal["midnight", "noon", "refresh"]


def run(slot: Slot, today: date | None = None) -> date:
    """Generate today's edition and upsert into editions. Returns the date row."""
    today = today or datetime.now(UTC).date()
    settings = get_settings()
    template = settings.news_pr_path.read_text(encoding="utf-8")
    Maker = session_factory()

    with Maker() as s:
        ctx = _build_context(s, today, slot)

    response = claude_client.generate_edition(template, ctx)
    pdf_bytes = pdf.html_to_pdf(response["pdf_html"])

    with Maker() as s:
        _upsert_edition(s, today, response["html"], pdf_bytes)
    log.info("Generated edition for %s (slot=%s)", today, slot)
    return today


def _build_context(s: Session, today: date, slot: Slot) -> dict:
    settings = get_settings()
    horizon = today + timedelta(days=30)

    hidden_movies = overlays.active_hidden_movie_titles(s, today)
    hidden_cals = overlays.hidden_calendar_ids(s)
    important = overlays.important_events_from(s, today)

    all_calendars: list[dict] = []
    events: list[dict] = []
    try:
        all_calendars = calendar_oauth.list_calendars(s)
        hidden_ids = {c["id"] for c in hidden_cals}
        active_ids = [c["id"] for c in all_calendars if c["id"] not in hidden_ids]
        events = calendar_oauth.fetch_events(s, active_ids, today, horizon)
    except RuntimeError as e:
        log.warning("Calendar unavailable: %s", e)

    return {
        "DATE": today.isoformat(),
        "SLOT": slot,
        "HIDDEN_MOVIES": hidden_movies,
        "HIDDEN_CALENDARS_JSON": hidden_cals,
        "ALL_CALENDARS_JSON": all_calendars,
        "CALENDAR_EVENTS_JSON": events,
        "IMPORTANT_EVENTS_JSON": important,
        "WEATHER_COORDS": settings.weather_coords,
        "CUSTOM_TOPICS": "",
    }


def _upsert_edition(s: Session, day: date, html: str, pdf_bytes: bytes) -> None:
    now = datetime.now(UTC)
    if s.bind.dialect.name == "postgresql":
        stmt = pg_insert(Edition).values(
            date=day, html=html, pdf=pdf_bytes, generated_at=now
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[Edition.date],
            set_={
                "html": stmt.excluded.html,
                "pdf": stmt.excluded.pdf,
                "generated_at": stmt.excluded.generated_at,
            },
        )
        s.execute(stmt)
    else:
        # Fallback path used by tests / sqlite.
        s.execute(text("DELETE FROM editions WHERE date = :d"), {"d": day})
        s.add(Edition(date=day, html=html, pdf=pdf_bytes, generated_at=now))
    s.commit()


def _cli() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("slot", choices=["midnight", "noon", "refresh"])
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(args.slot)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
