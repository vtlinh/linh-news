from __future__ import annotations

import argparse
import base64
import contextlib
import logging
import re
import sys
import time
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import calendar_oauth, calendar_summary, claude_client, overlays, pdf, weather
from app import movies as movies_mod
from app.db import Edition, session_factory
from app.settings import get_settings, local_today

log = logging.getLogger(__name__)

Slot = Literal["morning", "evening", "refresh"]


@contextlib.contextmanager
def _step(name: str):
    """Log how long a named step took. Logs even on exception."""
    t0 = time.monotonic()
    log.info("⏱  START   %s", name)
    try:
        yield
    finally:
        dt = time.monotonic() - t0
        log.info("⏱  DONE    %s (%.2fs)", name, dt)


def run(slot: Slot, today: date | None = None) -> date:
    """Generate today's edition and upsert into editions. Returns the date row."""
    today = today or local_today()
    settings = get_settings()
    template = settings.news_pr_path.read_text(encoding="utf-8")
    Maker = session_factory()

    overall_t0 = time.monotonic()
    log.info("⏱  ── refresh pipeline begin (slot=%s, date=%s) ──", slot, today)

    with _step("build_context"), Maker() as s:
        ctx = _build_context(s, today, slot)
    pdf_movies_html = ctx.pop("_pdf_movies_html", "")

    with _step("claude_generate_edition"):
        response = claude_client.generate_edition(template, ctx)
    with _step("strip_document_wrapper"):
        response["html"] = _strip_document_wrapper(response["html"])
    with _step("inject_pdf_movies"):
        response["pdf_html"] = _inject_pdf_movies(
            response["pdf_html"], pdf_movies_html
        )
    with _step("inject_lead_image"):
        response["pdf_html"] = _inject_lead_image(response["pdf_html"], today)
    with _step("ensure_lead_image"):
        response["pdf_html"] = _ensure_lead_image(response["pdf_html"])
    # Snapshot the print HTML *exactly* as it goes into WeasyPrint, so we
    # can inspect missing-image and overflow problems after the fact.
    try:
        from pathlib import Path
        snap_dir = Path(__file__).resolve().parent.parent / "logs"
        snap_dir.mkdir(exist_ok=True)
        snap = snap_dir / f"pdf-html-{slot}-{int(datetime.now(UTC).timestamp())}.html"
        snap.write_text(response["pdf_html"], encoding="utf-8")
        log.info("Saved pre-WeasyPrint pdf_html: %s (%d bytes, %d <img> tags)",
                 snap, len(response["pdf_html"]),
                 response["pdf_html"].lower().count("<img"))
    except Exception:
        log.exception("Could not snapshot pdf_html")

    with _step("html_to_pdf"):
        pdf_bytes = pdf.html_to_pdf(response["pdf_html"])

    with _step("upsert_edition"), Maker() as s:
        _upsert_edition(s, today, response["html"], pdf_bytes, response["pdf_html"])
    log.info("Generated edition for %s (slot=%s)", today, slot)

    log.info("⏱  ── refresh pipeline end (total %.2fs) ──",
             time.monotonic() - overall_t0)
    return today


def _build_context(s: Session, today: date, slot: Slot) -> dict:
    settings = get_settings()
    horizon = today + timedelta(days=30)

    with _step("overlays (hidden movies + watchlist)"):
        hidden_movies = overlays.active_hidden_movie_titles(s, today)
        watchlist = overlays.watchlist_symbols(s)

    # Refresh the cached movie list (long-horizon, kid-appropriate) once per
    # generation cycle and render its PDF block server-side. The HTML page
    # block is rendered at view-time (see ``main._inject_movies``) so admin
    # hides apply immediately without waiting for the next refresh.
    allowed_ratings = set(calendar_oauth.allowed_movie_ratings(today))
    hidden_set = set(hidden_movies)
    pdf_movies_html = ""
    try:
        with _step("movies.get_or_fetch_movies(force=True)"):
            cached_movies = movies_mod.get_or_fetch_movies(force=True)
        with _step("movies.render_pdf_html"):
            pdf_movies_html = movies_mod.render_pdf_html(
                cached_movies, today,
                hidden_titles=hidden_set, allowed_ratings=allowed_ratings,
            )
    except Exception:  # noqa: BLE001
        log.exception("Movie cache refresh / render failed")

    # ── Calendar pipeline ────────────────────────────────────────────────
    events: list[dict] = []
    important_uids: set[str] = set()
    try:
        with _step("overlays (calendar/event lookups)"):
            hidden_cals = overlays.hidden_calendar_ids(s)
            suppressed_uids = overlays.suppressed_event_uids(s)
            important = overlays.important_events_from(s, today)
            important_uids = {
                e["ical_uid"] for e in important if e.get("ical_uid")
            }

        with _step("google list_calendars"):
            all_calendars = calendar_oauth.list_calendars(s)
            hidden_ids = {c["id"] for c in hidden_cals}
            active_ids = [c["id"] for c in all_calendars if c["id"] not in hidden_ids]
            cal_names = {c["id"]: c["name"] for c in all_calendars}

        with _step("google fetch_events (today..+30d)"):
            events = calendar_oauth.fetch_events(
                s, active_ids, today, horizon, calendar_names=cal_names
            )
            events = [e for e in events if e.get("ical_uid") not in suppressed_uids]

        # Auto-mark important events and collect their uids for PDF calendar.
        for ev in events:
            cn = cal_names.get(ev.get("calendar_id"), "")
            if calendar_oauth.is_auto_important(cn, ev.get("summary", "")):
                important_uids.add(ev.get("ical_uid", ""))

        # Group by day and generate/retrieve per-day HTML summaries.
        events_by_day: dict[date, list[dict]] = {}
        for ev in events:
            day_str = (ev.get("start") or "")[:10]
            try:
                d = date.fromisoformat(day_str)
                events_by_day.setdefault(d, []).append(ev)
            except ValueError:
                pass
        with _step(f"calendar_summary ({len(events_by_day)} days)"):
            calendar_summary.get_or_generate_summaries(s, events_by_day)

    except Exception as e:  # noqa: BLE001 — never let calendar break generation
        log.warning("Calendar unavailable: %s", e)

    # PDF calendar: today+tomorrow timed events + important all-day, pure Python.
    pdf_calendar_html = calendar_summary.build_pdf_calendar(events, today, important_uids)

    # Current "Now" observation from NWS (empty string → Claude falls back to web_search).
    with _step("NWS fetch_current_now"):
        now_weather = weather.fetch_current_now(settings.weather_coords)

    return {
        "DATE": today.isoformat(),
        "KID_AGE": calendar_oauth.current_kid_age(today),
        "KID_GRADE": calendar_oauth.current_kid_grade(today),
        "WATCHLIST_STOCKS": watchlist,
        "WEATHER_COORDS": settings.weather_coords,
        "NOW_WEATHER": now_weather,
        "PDF_CALENDAR_HTML": pdf_calendar_html,
        "CUSTOM_TOPICS": "",
        # The PDF movie block is server-rendered and substituted into
        # ``pdf_html`` after Claude returns (see ``_inject_pdf_movies``).
        # Held as a private context key so it doesn't leak into the user
        # message sent to the LLM.
        "_pdf_movies_html": pdf_movies_html,
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
    """Verify a URL serves an image. Uses a tiny ranged GET instead of HEAD
    because Wikimedia's thumbnail server frequently returns 404 on HEAD for
    not-yet-cached thumbnails (HEAD is technically supported but stale-cache
    behaviour differs from GET). One-byte range keeps bandwidth minimal."""
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": _UA, "Range": "bytes=0-0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            ctype = (
                r.headers.get_content_type()
                if hasattr(r.headers, "get_content_type")
                else r.headers.get("Content-Type", "")
            )
            # Range requests succeed with 206; servers that ignore Range
            # return 200. Both are fine here.
            ok_status = r.status in (200, 206)
            return ok_status and (ctype or "").startswith("image/")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
        log.info("URL preflight failed (%s): %s", e, url[:200])
        return False


_H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")

_LEAD_IMAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "image_url": {
            "type": "string",
            "description": (
                "Direct URL to a real, publicly accessible image for the headline. "
                "Prefer Wikimedia Commons thumbnails: "
                "https://upload.wikimedia.org/wikipedia/commons/thumb/.../NNNpx-file.jpg. "
                "Must be a working image URL — verify via web_search before returning."
            ),
        },
        "alt_text": {"type": "string", "description": "Brief alt text for the image."},
    },
    "required": ["image_url", "alt_text"],
    "additionalProperties": False,
}


def _extract_lead_headline(pdf_html: str) -> str | None:
    """Return the text of the first <h2> in the PDF HTML (the lead story)."""
    m = _H2_RE.search(pdf_html)
    if not m:
        return None
    return _TAG_RE.sub("", m.group(1)).strip() or None


def _inject_lead_image(pdf_html: str, today: date) -> str:
    """Ask Claude (with web_search) for a real image matching the lead headline,
    then replace/inject an <img> at the top of the PDF HTML."""
    headline = _extract_lead_headline(pdf_html)
    if not headline:
        log.warning("Could not extract lead headline — skipping image lookup")
        return pdf_html

    log.info("Fetching lead image for headline: %r", headline[:120])
    try:
        result = claude_client.call_with_schema(
            system=(
                "You are finding a single real image for a newspaper PDF. "
                "Use web_search to locate a publicly accessible, directly embeddable "
                "image URL. Prefer Wikimedia Commons thumbnails. "
                "Never invent URLs."
            ),
            user=(
                f"Date: {today.isoformat()}\n"
                f"Lead headline: {headline}\n\n"
                "Search for the most appropriate real image for this story. "
                "Return a working direct image URL (not a page URL, not a search URL). "
                "Verify the URL is reachable via web_search before calling return_image."
            ),
            schema=_LEAD_IMAGE_SCHEMA,
            schema_name="return_image",
            schema_description="Return the verified image URL for the lead story.",
            extra_tools=[claude_client.WEB_SEARCH_TOOL],
            max_tokens=4000,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Lead-image lookup failed: %s", e)
        return pdf_html

    image_url = (result.get("image_url") or "").strip()
    alt_text = (result.get("alt_text") or headline[:80]).strip()

    if not image_url:
        log.warning("Claude returned no image_url for headline %r", headline[:80])
        return pdf_html

    log.info("Lead image URL: %s", image_url)

    img_tag = (
        f'<img src="{image_url}" alt="{alt_text}" '
        f'style="width:100%; max-height:1.6in; object-fit:cover; margin:0 0 4pt;">'
    )
    # Replace any existing <img> tag(s) that appear before the first <h2>,
    # then inject our verified image immediately before the lead headline.
    before_h2 = _H2_RE.split(pdf_html)[0]
    stripped = _IMG_TAG_RE.sub("", before_h2)  # remove old lead images
    return stripped + img_tag + pdf_html[len(before_h2):]


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


_PDF_MOVIES_PLACEHOLDER = "<!-- PDF_MOVIES_PLACEHOLDER -->"


def _inject_pdf_movies(pdf_html: str, movies_html: str) -> str:
    """Replace ``<!-- PDF_MOVIES_PLACEHOLDER -->`` in the LLM-rendered PDF
    HTML with the server-rendered movies block. If the LLM omitted the
    placeholder there's nothing to do — the PDF simply ships without
    movies (which is acceptable per the priority list)."""
    if _PDF_MOVIES_PLACEHOLDER not in pdf_html:
        if movies_html:
            log.warning(
                "PDF movies placeholder missing — server-rendered movies block "
                "will not appear in this PDF.",
            )
        return pdf_html
    return pdf_html.replace(_PDF_MOVIES_PLACEHOLDER, movies_html, 1)


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


def _upsert_edition(
    s: Session, day: date, html: str, pdf_bytes: bytes, pdf_html: str | None = None
) -> None:
    now = datetime.now(UTC)
    if s.bind.dialect.name == "postgresql":
        stmt = pg_insert(Edition).values(
            date=day, html=html, pdf_html=pdf_html, pdf=pdf_bytes, generated_at=now
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[Edition.date],
            set_={
                "html": stmt.excluded.html,
                "pdf_html": stmt.excluded.pdf_html,
                "pdf": stmt.excluded.pdf,
                "generated_at": stmt.excluded.generated_at,
            },
        )
        s.execute(stmt)
    else:
        # Fallback path used by tests / sqlite.
        s.execute(text("DELETE FROM editions WHERE date = :d"), {"d": day})
        s.add(Edition(
            date=day, html=html, pdf_html=pdf_html, pdf=pdf_bytes, generated_at=now,
        ))
    s.commit()


def _cli() -> int:
    import time
    from pathlib import Path

    p = argparse.ArgumentParser()
    p.add_argument("slot", choices=["morning", "evening", "refresh"])
    p.add_argument("--date", default=None, help="Override date (YYYY-MM-DD)")
    args = p.parse_args()

    # Logs go to BOTH the inherited stderr (so they appear in the dev terminal
    # running uvicorn when triggered via /refresh) AND a per-run log file
    # under logs/refresh-*.log so you can tail them later.
    log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"refresh-{args.slot}-{int(time.time())}.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    stream_h = logging.StreamHandler(sys.stderr)
    stream_h.setFormatter(fmt)
    file_h = logging.FileHandler(log_path, encoding="utf-8")
    file_h.setFormatter(fmt)
    # Replace any handlers basicConfig may have set so we don't get duplicates.
    root.handlers[:] = [stream_h, file_h]
    log.info("Refresh log file: %s", log_path)

    from app import cache

    target_date: date | None = None
    if args.date:
        try:
            target_date = date.fromisoformat(args.date)
        except ValueError:
            log.error("Invalid --date value: %s", args.date)
            return 1

    if args.slot == "refresh":
        cache.clear_edition_refresh_error()
    error_msg: str | None = None
    started = time.monotonic()
    try:
        run(args.slot, today=target_date)
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
