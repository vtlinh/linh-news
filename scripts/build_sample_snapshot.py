"""Build the frozen "Sample newspaper" snapshot served at /sample.

Reads the admin's edition for a fixed date (default 2026-05-26) from the DB
and bakes a set of static files under ``app/sample_data/`` that the no-auth
``/sample`` routes serve verbatim:

  * ``home.html``  — the edition body with weather + movies injected and every
    subsection image inlined as a data URI. The real calendar is replaced with
    a single "Sample calendar events" line.
  * ``sample.pdf`` — the broadsheet PDF re-rendered with the same sample
    calendar line in the rail.
  * ``movies.json`` — the movie grid payload (same shape as /admin/movies/data).
  * ``stocks.json`` — the watchlist symbols.
  * ``data.json``   — the Data-tab settings, with children emptied and the
    address blanked per the sample spec.
  * ``meta.json``   — edition date + masthead name.

The snapshot is committed to the repo and is intentionally frozen: re-run this
script only when you want to refresh what the sample pages show. Requires the
Fly Postgres proxy (localhost:15432) and WeasyPrint's native libs, exactly like
a normal generation run.

Usage:  uv run python -m scripts.build_sample_snapshot [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from datetime import date

from sqlalchemy import select

from app import main, overlays, pdf, pdf_renderer, prefs, user_settings, weather
from app import movies as movies_mod
from app.db import Edition, SubsectionImage, session_factory
from app.settings import REPO_ROOT, get_settings

SAMPLE_DIR = REPO_ROOT / "app" / "sample_data"
DEFAULT_DATE = date(2026, 5, 26)

# Calendar replacement text the sample pages show instead of real events.
SAMPLE_CALENDAR_TEXT = "Sample calendar events"
SAMPLE_CALENDAR_HTML = (
    "<section>\n<h2>\U0001f4c5 Calendar</h2>\n"
    f"<div>{SAMPLE_CALENDAR_TEXT}</div>\n</section>"
)
# PDF rail calendar block — mirrors calendar_summary.build_pdf_calendar's
# label markup so the rail fit treats it the same as a real calendar.
_PDF_CAL_LABEL_STYLE = (
    "font-size:10pt;letter-spacing:.05em;text-transform:uppercase;"
    "border-bottom:0.5pt solid #000;margin:0 0 2pt;padding-bottom:1pt;"
    "font-weight:bold"
)
SAMPLE_PDF_CALENDAR_HTML = (
    f'<div style="{_PDF_CAL_LABEL_STYLE}">Calendar</div>'
    f'<div class="cal-event" style="margin:0 0 1pt">{SAMPLE_CALENDAR_TEXT}</div>'
)


def _inline_images(html: str, s) -> str:
    """Replace ``/edition-image/{id}`` srcs with base64 data URIs so the
    snapshot doesn't depend on DB rows persisting."""

    def repl(m: re.Match) -> str:
        image_id = int(m.group(1))
        row = s.get(SubsectionImage, image_id)
        if not row or not row.bytes_:
            return m.group(0)
        b64 = base64.b64encode(row.bytes_).decode("ascii")
        mime = row.mime_type or "image/jpeg"
        return f"data:{mime};base64,{b64}"

    return re.sub(r"/edition-image/(\d+)", repl, html)


def _build_movies_payload(s, email: str, today: date) -> dict:
    """Reproduce GET /admin/movies/data for the snapshot."""
    selected = set(prefs.get_allowed_ratings(email=email))
    include_unrated = "Unrated" in selected
    unrated_certs = {"", "NR"}
    favorites = overlays.favorite_movie_titles(s, email)
    movies = movies_mod.get_movies() or []

    def matches(m: dict) -> bool:
        if m.get("title") in favorites:
            return True
        rating = (m.get("rating") or "").strip()
        if rating in selected:
            return True
        return include_unrated and rating in unrated_certs

    from datetime import timedelta

    cutoff = today - timedelta(weeks=3)

    def still_fresh(m: dict) -> bool:
        if m.get("status") != "in_theaters":
            return True
        try:
            return date.fromisoformat(m.get("release_date", "")) >= cutoff
        except Exception:  # noqa: BLE001
            return True

    movies = [m for m in movies if matches(m) and still_fresh(m)]
    hidden = {m["title"] for m in overlays.all_hidden_movies(s, email)}
    return {
        "movies": [
            {**m, "hidden": m["title"] in hidden, "favorite": m["title"] in favorites}
            for m in movies
        ],
        "default_ratings": sorted(selected),
    }


def main_build(snapshot_date: date) -> None:
    settings = get_settings()
    admin = settings.admin_email.lower()
    Maker = session_factory()
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)

    with Maker() as s:
        edition = s.execute(
            select(Edition).where(Edition.date == snapshot_date, Edition.email == admin)
        ).scalar_one_or_none()
        if edition is None:
            raise SystemExit(f"No edition found for {snapshot_date} / {admin}")

        linhnews = edition.content_json or {}
        masthead_name = (
            user_settings.get(s, admin).get("display_name") or "Daily"
        ).strip() or "Daily"

        # ── home.html ──────────────────────────────────────────────────────
        html = edition.html or ""
        html = main._inject_weather(html, s, edition)
        html = main._inject_movies(html, s, snapshot_date, admin)
        html = html.replace("<!-- CALENDAR_PLACEHOLDER -->", SAMPLE_CALENDAR_HTML, 1)
        html = _inline_images(html, s)
        (SAMPLE_DIR / "home.html").write_text(html, encoding="utf-8")
        print(f"wrote home.html ({len(html)} chars)")

        # ── movies / stocks / data ─────────────────────────────────────────
        movies_payload = _build_movies_payload(s, admin, snapshot_date)
        (SAMPLE_DIR / "movies.json").write_text(
            json.dumps(movies_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"wrote movies.json ({len(movies_payload['movies'])} movies)")

        symbols = overlays.watchlist_symbols(s, admin)
        (SAMPLE_DIR / "stocks.json").write_text(
            json.dumps({"symbols": symbols}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"wrote stocks.json ({len(symbols)} symbols)")

        settings_dict = user_settings.get(s, admin)
        settings_dict["children"] = []
        settings_dict["address"] = ""
        (SAMPLE_DIR / "data.json").write_text(
            json.dumps(settings_dict, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("wrote data.json")

        (SAMPLE_DIR / "meta.json").write_text(
            json.dumps(
                {"date": snapshot_date.isoformat(), "masthead_name": masthead_name},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        # ── sample.pdf ─────────────────────────────────────────────────────
        rail = edition.pdf_rail_json or {}
        pdf_movies_html = rail.get("movies_html", "")
        if not pdf_movies_html:
            try:
                allowed = set(prefs.get_allowed_ratings(snapshot_date, session=s))
                hidden = {m["title"] for m in overlays.all_hidden_movies(s, admin)}
                favorites = overlays.favorite_movie_titles(s, admin)
                pdf_movies_html = movies_mod.render_pdf_html(
                    movies_mod.get_movies() or [],
                    snapshot_date,
                    hidden_titles=hidden,
                    allowed_ratings=allowed,
                    favorite_titles=favorites,
                )
            except Exception as e:  # noqa: BLE001
                print(f"movies rail render failed: {e}")

        coords = settings.weather_coords
        pdf_now = weather.get_now_cached(s, coords)
        pdf_weather_strip = weather.build_weather_strip(
            pdf_now,
            edition.weather_forecast_json or {},
            edition.weather_alerts_json or [],
        )
        prose_html = (edition.weather_forecast_json or {}).get("prose_html", "") or ""

        ai_cost = float(linhnews.get("_ai_cost_usd") or 0.0)
        import math

        ai_cost_disp = math.ceil(ai_cost * 20) / 20 if ai_cost > 0 else 0.0
        first_date = s.execute(
            select(Edition.date).order_by(Edition.date.asc()).limit(1)
        ).scalar()
        vol_number = 1 if first_date is None else (snapshot_date - first_date).days + 1

        image_bytes_by_id = {
            row.id: (row.bytes_, row.mime_type or "image/jpeg")
            for row in s.execute(
                select(SubsectionImage).where(
                    SubsectionImage.edition_date == snapshot_date,
                    SubsectionImage.edition_email == admin,
                )
            )
            .scalars()
            .all()
        }

        parts = pdf_renderer.build_pdf_parts(
            linhnews,
            pdf_calendar_html=SAMPLE_PDF_CALENDAR_HTML,
            pdf_movies_html=pdf_movies_html,
            weather_strip_html=pdf_weather_strip,
            weather_prose_html=prose_html,
            today=snapshot_date,
            image_bytes_by_id=image_bytes_by_id,
            masthead_name=masthead_name,
            vol_number=vol_number,
            ai_cost_usd=ai_cost_disp,
            weather_location_label="",
        )
        try:
            pdf_bytes = pdf.html_to_pdf(parts)
            if pdf_bytes and pdf_bytes.startswith(b"%PDF"):
                (SAMPLE_DIR / "sample.pdf").write_bytes(pdf_bytes)
                print(f"wrote sample.pdf ({len(pdf_bytes)} bytes)")
            else:
                print("PDF render produced no valid bytes — sample.pdf NOT written")
        except pdf.PdfSkipped as e:
            print(f"PDF skipped: {e} — sample.pdf NOT written")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=DEFAULT_DATE.isoformat())
    args = ap.parse_args()
    main_build(date.fromisoformat(args.date))
