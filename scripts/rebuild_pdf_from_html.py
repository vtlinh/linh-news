"""Rebuild today's PDF from the stored Edition.content_json — no LLM call.

Reads ``editions.content_json`` for ``today``, re-fetches calendar / movies /
weather server-side, runs ``app.pdf_renderer.build_pdf_html``, and updates
``Edition.pdf`` + ``Edition.pdf_html`` in place. Useful when you've changed
the renderer and want to re-paginate without spending an LLM credit.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import (
    calendar_oauth,
    calendar_summary,
    overlays,
    pdf,
    pdf_renderer,
    prefs,
    weather,
)
from app import movies as movies_mod
from app.db import Edition, session_factory
from app.settings import get_settings, local_today

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("rebuild_pdf")


def main(target: date) -> int:
    Maker = session_factory()
    with Maker() as s:
        e = s.get(Edition, target)
        if not e or not e.content_json:
            log.error("No structured content_json for %s — re-run /refresh first", target)
            return 1
        log.info("Loaded structured content for %s", target)

        # Calendar (Google API, no LLM)
        hidden_cals = overlays.hidden_calendar_ids(s)
        suppressed = overlays.suppressed_event_uids(s)
        important = overlays.important_events_from(s, target)
        important_uids = {x["ical_uid"] for x in important if x.get("ical_uid")}
        cals = calendar_oauth.list_calendars(s)
        hidden_ids = {c["id"] for c in hidden_cals}
        active = [c["id"] for c in cals if c["id"] not in hidden_ids]
        cal_names = {c["id"]: c["name"] for c in cals}
        events = calendar_oauth.fetch_events(
            s,
            active,
            target,
            target + timedelta(days=30),
            calendar_names=cal_names,
        )
        events = [ev for ev in events if ev.get("ical_uid") not in suppressed]
        events = calendar_oauth.dedupe_events(events)
        for ev in events:
            cn = cal_names.get(ev.get("calendar_id"), "")
            if calendar_oauth.is_auto_important(cn, ev.get("summary", "")):
                important_uids.add(ev.get("ical_uid", ""))
        pdf_cal = calendar_summary.build_pdf_calendar(events, target, important_uids)
        log.info("Calendar block: %d bytes", len(pdf_cal))

        # Movies (cached, no LLM)
        cached = movies_mod.get_movies()
        hidden = set(overlays.active_hidden_movie_titles(s, target))
        allowed = set(prefs.get_allowed_ratings(target))
        pdf_mov = movies_mod.render_pdf_html(
            cached,
            target,
            hidden_titles=hidden,
            allowed_ratings=allowed,
        )
        log.info("Movies block: %d bytes", len(pdf_mov))

        # Weather strip — fetched here so re-runs don't need stale persistence.
        coords = get_settings().weather_coords
        try:
            now_text = weather.get_now_cached(s, coords)
            forecast = weather.fetch_forecast(coords)
            alerts = weather.fetch_alerts(coords)
            weather_strip_html = weather.build_weather_strip(
                now_text,
                forecast,
                alerts,
            )
        except Exception:  # noqa: BLE001
            log.exception("Could not build weather strip; rendering without it")
            weather_strip_html = ""
        log.info("Weather strip: %d bytes", len(weather_strip_html))

        pdf_html = pdf_renderer.build_pdf_html(
            e.content_json,
            pdf_calendar_html=pdf_cal,
            pdf_movies_html=pdf_mov,
            weather_strip_html=weather_strip_html,
            today=target,
        )
        log.info("Print HTML: %d bytes", len(pdf_html))

        pdf_bytes = pdf.html_to_pdf(pdf_html)
        log.info("PDF rendered: %d bytes (header=%r)", len(pdf_bytes), pdf_bytes[:8])

        e.pdf = pdf_bytes
        e.pdf_html = pdf_html
        s.commit()
        log.info("Upserted edition %s", target)
    return 0


if __name__ == "__main__":
    target = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else local_today()
    sys.exit(main(target))
