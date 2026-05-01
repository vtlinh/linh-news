from __future__ import annotations

import argparse
import base64
import logging
import re
import sys
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import calendar_oauth, claude_client, overlays, pdf
from app.db import Edition, session_factory
from app.settings import get_settings, local_today

log = logging.getLogger(__name__)

Slot = Literal["morning", "evening", "refresh"]


def run(slot: Slot, today: date | None = None) -> date:
    """Generate today's edition and upsert into editions. Returns the date row."""
    today = today or local_today()
    settings = get_settings()
    template = settings.news_pr_path.read_text(encoding="utf-8")
    Maker = session_factory()

    with Maker() as s:
        ctx = _build_context(s, today, slot)

    response = claude_client.generate_edition(template, ctx)
    response["html"] = _strip_document_wrapper(response["html"])
    response["pdf_html"] = _ensure_lead_image(response["pdf_html"])
    pdf_bytes = pdf.html_to_pdf(response["pdf_html"])

    with Maker() as s:
        _upsert_edition(s, today, response["html"], pdf_bytes)
    log.info("Generated edition for %s (slot=%s)", today, slot)

    # Each generation cycle also refreshes the year-out movie list so the
    # admin /admin/movies page stays current. Cron runs (morning/evening) and
    # the user-triggered home Refresh both pass through here.
    try:
        from app import movies
        movies.get_or_fetch_movies(force=True)
    except Exception:  # noqa: BLE001
        log.exception("Movie cache refresh failed")
    return today


def _build_context(s: Session, today: date, slot: Slot) -> dict:
    settings = get_settings()
    horizon = today + timedelta(days=30)

    hidden_movies = overlays.active_hidden_movie_titles(s, today)
    hidden_cals = overlays.hidden_calendar_ids(s)
    important = overlays.important_events_from(s, today)
    suppressed_uids = overlays.suppressed_event_uids(s)
    suppressed_list = overlays.suppressed_events_list(s)
    watchlist = overlays.watchlist_symbols(s)

    all_calendars: list[dict] = []
    events: list[dict] = []
    try:
        all_calendars = calendar_oauth.list_calendars(s)
        hidden_ids = {c["id"] for c in hidden_cals}
        active_ids = [c["id"] for c in all_calendars if c["id"] not in hidden_ids]
        cal_names = {c["id"]: c["name"] for c in all_calendars}
        events = calendar_oauth.fetch_events(
            s, active_ids, today, horizon, calendar_names=cal_names
        )
        # Drop suppressed events outright — Claude never sees them.
        events = [e for e in events if e.get("ical_uid") not in suppressed_uids]
        # Promote auto-rule matches into important_events list shown to Claude.
        for ev in events:
            cn = cal_names.get(ev.get("calendar_id"), "")
            if calendar_oauth.is_auto_important(cn, ev.get("summary", "")):
                important.append(
                    {
                        "title": ev.get("summary"),
                        "date": (ev.get("start") or "")[:10],
                        "importance": 9,
                        "notes": f"auto-marked: {cn}",
                    }
                )
    except Exception as e:  # noqa: BLE001 — never let calendar break generation
        log.warning("Calendar unavailable: %s", e)

    return {
        "DATE": today.isoformat(),
        "KID_AGE": calendar_oauth.current_kid_age(today),
        "KID_GRADE": calendar_oauth.current_kid_grade(today),
        "ALLOWED_MOVIE_RATINGS": calendar_oauth.allowed_movie_ratings(today),
        "HIDDEN_MOVIES": hidden_movies,
        "HIDDEN_CALENDARS_JSON": hidden_cals,
        "ALL_CALENDARS_JSON": all_calendars,
        "CALENDAR_EVENTS_JSON": events,
        "IMPORTANT_EVENTS_JSON": important,
        "WEATHER_COORDS": settings.weather_coords,
        "SUPPRESSED_EVENTS_JSON": suppressed_list,
        "WATCHLIST_STOCKS": watchlist,
        "CUSTOM_TOPICS": "",
    }


_BODY_RE = re.compile(r"<body[^>]*>(.*?)</body\s*>", re.IGNORECASE | re.DOTALL)
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style\s*>", re.IGNORECASE | re.DOTALL)
_HEAD_LEAK_RE = re.compile(
    r"<\s*(?:!doctype[^>]*|/?html[^>]*|/?head[^>]*|/?body[^>]*|"
    r"meta[^>]*/?|title[^>]*/?|link[^>]*/?)>",
    re.IGNORECASE,
)
# Claude's web_search emits <cite index="..."> wrappers around quoted
# passages and inline citation references. We use our own SOURCES popup at
# the end of each item, so these wrappers just clutter the body text.
_CITE_RE = re.compile(r"<cite[^>]*>(.*?)</cite\s*>", re.IGNORECASE | re.DOTALL)


_IMG_TAG_RE = re.compile(
    r'(<img\b[^>]*\bsrc\s*=\s*)(["\'])([^"\']+)\2([^>]*>)',
    re.IGNORECASE,
)
_UA = "Linh-News/1.0 (https://github.com/vtlinh/linh-news; vtlinh87+linhnews@gmail.com)"
# Inline SVG fallback (data URI) so we never depend on the network. Renders
# as a generic newspaper graphic in the lead column when no real image is
# available.
_FALLBACK_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 320 200'>"
    "<rect width='320' height='200' fill='#f4f0e8'/>"
    "<rect x='20' y='20' width='280' height='28' fill='#1a1a1a'/>"
    "<text x='160' y='40' font-family='Times New Roman, serif' font-size='22' "
    "font-weight='bold' fill='#f4f0e8' text-anchor='middle'>HEADLINE</text>"
    "<rect x='20' y='62' width='280' height='4' fill='#1a1a1a'/>"
    "<rect x='20' y='80' width='130' height='100' fill='#cdbfa9'/>"
    "<line x1='160' y1='80' x2='300' y2='80' stroke='#444' stroke-width='1'/>"
    "<line x1='160' y1='95' x2='300' y2='95' stroke='#888' stroke-width='1'/>"
    "<line x1='160' y1='110' x2='300' y2='110' stroke='#888' stroke-width='1'/>"
    "<line x1='160' y1='125' x2='280' y2='125' stroke='#888' stroke-width='1'/>"
    "<line x1='160' y1='140' x2='300' y2='140' stroke='#888' stroke-width='1'/>"
    "<line x1='160' y1='155' x2='270' y2='155' stroke='#888' stroke-width='1'/>"
    "<line x1='160' y1='170' x2='300' y2='170' stroke='#888' stroke-width='1'/>"
    "</svg>"
)
_FALLBACK_IMG_URL = (
    "data:image/svg+xml;base64,"
    + base64.b64encode(_FALLBACK_SVG.encode("utf-8")).decode("ascii")
)


def _url_alive(url: str, timeout: float = 8.0) -> bool:
    """HEAD-check a URL with our real User-Agent. Returns True only on a
    2xx response with an image-like Content-Type."""
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": _UA}, method="HEAD"
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            ctype = (
                r.headers.get_content_type()
                if hasattr(r.headers, "get_content_type")
                else r.headers.get("Content-Type", "")
            )
            return 200 <= r.status < 300 and ctype.startswith("image/")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return False


def _ensure_lead_image(pdf_html: str) -> str:
    """Validate every <img src=...> in the print HTML and replace any that
    don't load with a stable fallback. If Claude omitted images entirely,
    inject the fallback at the very top of the document so the PDF still
    leads with an image as required."""
    found_any = False
    bad_urls: list[str] = []

    def _replace(m: re.Match[str]) -> str:
        nonlocal found_any
        found_any = True
        url = m.group(3)
        if _url_alive(url):
            return m.group(0)
        bad_urls.append(url)
        return f'{m.group(1)}{m.group(2)}{_FALLBACK_IMG_URL}{m.group(2)}{m.group(4)}'

    rewritten = _IMG_TAG_RE.sub(_replace, pdf_html)
    if bad_urls:
        log.warning(
            "Lead-image URL(s) failed preflight, swapping in fallback: %s",
            ", ".join(bad_urls),
        )
    if not found_any:
        log.warning(
            "Claude omitted the lead-story <img> entirely — injecting fallback image."
        )
        injection = (
            f'<img src="{_FALLBACK_IMG_URL}" alt="" '
            f'style="width:100%; max-height:1.6in; object-fit:cover; margin:0 0 4pt;">'
        )
        rewritten = injection + rewritten
    return rewritten


def _strip_document_wrapper(html: str) -> str:
    """If Claude emitted a full HTML document, peel off the document chrome
    (DOCTYPE, html/head/body) plus any leaked <style> blocks. Also strip
    <cite> wrappers from web_search citations (we keep their inner text)."""
    body_match = _BODY_RE.search(html)
    if body_match:
        html = body_match.group(1)
    html = _STYLE_RE.sub("", html)
    html = _HEAD_LEAK_RE.sub("", html)
    html = _CITE_RE.sub(lambda m: m.group(1), html)
    return html.strip()


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
    import time

    p = argparse.ArgumentParser()
    p.add_argument("slot", choices=["morning", "evening", "refresh"])
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from app import cache

    if args.slot == "refresh":
        cache.clear_edition_refresh_error()
    error_msg: str | None = None
    started = time.monotonic()
    try:
        run(args.slot)
    except Exception as e:  # noqa: BLE001
        error_msg = _summarize_error(e)
        log.exception("generate.run failed")
    finally:
        elapsed = time.monotonic() - started
        log.info("Slot %s finished in %.1fs (success=%s)", args.slot, elapsed, error_msg is None)
        if error_msg is None:
            # Record duration for the running average shown in the UI toast.
            cache.record_refresh_duration(elapsed)
        if args.slot == "refresh":
            if error_msg:
                cache.set_edition_refresh_error(error_msg)
            cache.end_edition_refresh()
    return 0 if error_msg is None else 1


def _summarize_error(e: BaseException) -> str:
    """Compact, human-readable error string for the UI toast."""
    name = type(e).__name__
    msg = str(e).strip()
    # Pull a useful sentence out of long Anthropic-style API errors.
    if "credit balance" in msg.lower():
        return "Anthropic credit balance too low — top up at console.anthropic.com."
    if "rate limit" in msg.lower() or "429" in msg:
        return "Anthropic rate limit hit — wait a minute and try again."
    if len(msg) > 220:
        msg = msg[:217] + "..."
    return f"{name}: {msg}" if msg else name


if __name__ == "__main__":
    sys.exit(_cli())
