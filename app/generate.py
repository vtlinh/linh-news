from __future__ import annotations

import argparse
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

from app import (
    calendar_oauth,
    calendar_summary,
    claude_client,
    overlays,
    pdf,
    pdf_html_builder,
    prefs,
    weather,
)
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
    pdf_calendar_html = ctx.pop("_pdf_calendar_html", "")
    weather_forecast = ctx.pop("_weather_forecast", {})
    weather_alerts = ctx.pop("_weather_alerts", [])

    with _step("claude_generate_edition"):
        response = claude_client.generate_edition(template, ctx)
    with _step("strip_document_wrapper"):
        response["html"] = _strip_document_wrapper(response["html"])
    with _step("backfill_missing_sections"):
        response["html"] = _backfill_missing_sections(response["html"], today)
    # Build the weather strip used in the PDF (baked at generation time;
    # the screen page substitutes its own at view time so 'Now' stays
    # within the 1-hour cache window).
    with _step("build_pdf_weather_strip"), Maker() as s:
        pdf_now = weather.get_now_cached(s, settings.weather_coords)
    pdf_weather_strip = weather.build_weather_strip(
        pdf_now, weather_forecast, weather_alerts,
    )
    with _step("build_pdf_html"):
        pdf_html = pdf_html_builder.build(
            response["html"],
            pdf_calendar_html=pdf_calendar_html,
            pdf_movies_html=pdf_movies_html,
            weather_strip_html=pdf_weather_strip,
            today=today,
        )
    # Snapshot the print HTML *exactly* as it goes into WeasyPrint, so we
    # can inspect missing-image and overflow problems after the fact.
    try:
        from pathlib import Path
        snap_dir = Path(__file__).resolve().parent.parent / "logs"
        snap_dir.mkdir(exist_ok=True)
        snap = snap_dir / f"pdf-html-{slot}-{int(datetime.now(UTC).timestamp())}.html"
        snap.write_text(pdf_html, encoding="utf-8")
        log.info("Saved pre-WeasyPrint pdf_html: %s (%d bytes, %d <img> tags)",
                 snap, len(pdf_html), pdf_html.lower().count("<img"))
    except Exception:
        log.exception("Could not snapshot pdf_html")

    with _step("html_to_pdf"):
        pdf_bytes = pdf.html_to_pdf(pdf_html)
    try:
        from pathlib import Path
        snap_dir = Path(__file__).resolve().parent.parent / "logs"
        snap_dir.mkdir(exist_ok=True)
        ts = int(datetime.now(UTC).timestamp())
        pdf_snap = snap_dir / f"pdf-{slot}-{ts}.pdf"
        pdf_snap.write_bytes(pdf_bytes)
        log.info("Saved generated PDF: %s (%d bytes)", pdf_snap, len(pdf_bytes))
    except Exception:
        log.exception("Could not snapshot pdf bytes")

    with _step("upsert_edition"), Maker() as s:
        _upsert_edition(
            s, today, response["html"], pdf_bytes, pdf_html,
            weather_forecast=weather_forecast,
            weather_alerts=weather_alerts,
        )
    log.info("Generated edition for %s (slot=%s)", today, slot)

    log.info("⏱  ── refresh pipeline end (total %.2fs) ──",
             time.monotonic() - overall_t0)
    return today


def _build_context(s: Session, today: date, slot: Slot) -> dict:
    settings = get_settings()
    horizon = today + timedelta(days=30)

    with _step("overlays (hidden movies + watchlist)"):
        hidden_movies = overlays.active_hidden_movie_titles(s, today)
        favorite_movies = overlays.favorite_movie_titles(s)
        watchlist = overlays.watchlist_symbols(s)

    # Refresh the cached movie list (long-horizon, kid-appropriate) once per
    # generation cycle and render its PDF block server-side. The HTML page
    # block is rendered at view-time (see ``main._inject_movies``) so admin
    # hides apply immediately without waiting for the next refresh.
    with _step("prefs.get_allowed_ratings"):
        allowed_ratings = set(prefs.get_allowed_ratings(today, session=s))
    hidden_set = set(hidden_movies)
    pdf_movies_html = ""
    try:
        with _step("movies.get_movies(refresh_if_stale=True)"):
            cached_movies = movies_mod.get_movies(refresh_if_stale=True)
        with _step("movies.render_pdf_html"):
            pdf_movies_html = movies_mod.render_pdf_html(
                cached_movies, today,
                hidden_titles=hidden_set,
                allowed_ratings=allowed_ratings,
                favorite_titles=favorite_movies,
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
            # Collapse duplicates that span multiple subscribed calendars
            # (e.g. a shared "Racquetball weekly" appearing on two cals
            # with different iCalUIDs).
            events = calendar_oauth.dedupe_events(events)

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
        # Cron runs (morning/evening) take the cheaper Batch-API emoji path
        # at the cost of ~5+ min of extra latency. User /refresh runs take
        # the single-call path so the spinner stays under ~10 s for emojis.
        emoji_use_batch = slot != "refresh"
        with _step(f"calendar_persist ({len(events_by_day)} days, "
                   f"emoji_batch={emoji_use_batch})"):
            calendar_summary.persist_events_for_days(
                s, events_by_day, use_batch=emoji_use_batch,
            )

    except Exception as e:  # noqa: BLE001 — never let calendar break generation
        log.warning("Calendar unavailable: %s", e)

    # PDF calendar: today+tomorrow timed events + important all-day, pure Python.
    pdf_calendar_html = calendar_summary.build_pdf_calendar(events, today, important_uids)

    # Dorchester Parent Calendar — the only calendar that's allowed into the
    # LLM's user message, so Claude can fold concrete upcoming school events
    # into the Dorchester news section without inventing dates.
    dorchester_text = calendar_summary.build_dorchester_event_list(events, cal_names)

    # NWS forecast + active alerts — fetched at generation time and persisted
    # on the Edition row so view-time injection doesn't re-query NWS for the
    # slowly-changing parts. The 'Now' observation has its own 1-hour cache.
    with _step("NWS fetch_forecast"):
        weather_forecast = weather.fetch_forecast(settings.weather_coords)
    with _step("NWS fetch_alerts"):
        weather_alerts = weather.fetch_alerts(settings.weather_coords)

    return {
        "DATE": today.isoformat(),
        "KID_AGE": calendar_oauth.current_kid_age(today),
        "KID_GRADE": calendar_oauth.current_kid_grade(today),
        "WATCHLIST_STOCKS": watchlist,
        "DORCHESTER_CALENDAR_EVENTS": dorchester_text,
        "CUSTOM_TOPICS": "",
        # Private context keys (leading underscore). Popped before the LLM
        # call in run() so they never leak into the user message.
        "_pdf_movies_html": pdf_movies_html,
        "_pdf_calendar_html": pdf_calendar_html,
        "_weather_forecast": weather_forecast,
        "_weather_alerts": weather_alerts,
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


# ── Required news sections (in flow order) ───────────────────────────────
# Each entry: (key, human-friendly name, h2-keyword pattern used to detect
# whether the section is already present, topic spec for re-roll prompt).
_REQUIRED_NEWS_SECTIONS: list[tuple[str, str, re.Pattern, str]] = [
    ("global", "🌍 Top Global Political News",
     re.compile(r"global|world|international", re.IGNORECASE),
     "Top global political news — major world / international developments. "
     "Cite reputable outlets (Reuters, AP, BBC, etc.)."),
    ("us", "🇺🇸 Top US Political News",
     re.compile(r"\bus\b|u\.s\.|united states|us political", re.IGNORECASE),
     "Top US political news — major US government / political developments."),
    ("njny", "🗽 Top NJ / NY News",
     re.compile(r"\bnj\b|new jersey|new york|\bny\b", re.IGNORECASE),
     "Top New Jersey / New York regional news."),
    ("dorch", "🏫 Dorchester Elementary School News & Events",
     re.compile(r"dorchester|elementary", re.IGNORECASE),
     "Top Dorchester Elementary School (Woodcliff Lake, NJ) news plus upcoming "
     "events. Always check https://www.wclpfa.com/WlL/index.cfm."),
    ("finance", "💰 Top Financial News",
     re.compile(r"financ", re.IGNORECASE),
     "Top financial news — markets, deals, economic data."),
    ("tech", "💻 Top Tech News",
     re.compile(r"\btech\b|technology", re.IGNORECASE),
     "Top tech news — product launches, acquisitions, regulatory actions, "
     "platform changes, hardware releases. Exclude AI-specific stories."),
    ("ai", "🤖 AI News",
     re.compile(r"\bai\b|artificial intelligence", re.IGNORECASE),
     "AI news, emphasizing coding AI (Claude, Cursor, Copilot, Codex, etc.)."),
    ("ukraine", "🇺🇦 Ukraine News",
     re.compile(r"ukrain", re.IGNORECASE),
     "Ukraine news — front-line military situation, diplomatic and "
     "peace-process news, Western aid and sanctions, significant domestic "
     "political/economic developments inside Ukraine, humanitarian stories. "
     "Cite reputable outlets (Reuters, AP, BBC, Kyiv Independent, "
     "Ukrainska Pravda, etc.)."),
]

_H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2\s*>", re.IGNORECASE | re.DOTALL)
_TAG_STRIP = re.compile(r"<[^>]+>")
_SECTION_BLOCK_RE = re.compile(
    r"<section(?:\s[^>]*)?>.*?</section\s*>", re.IGNORECASE | re.DOTALL,
)


def _detect_missing_sections(html: str) -> list[tuple[str, str, re.Pattern, str]]:
    h2_titles = [
        _TAG_STRIP.sub("", m.group(1)).strip()
        for m in _H2_RE.finditer(html)
    ]
    missing = []
    for entry in _REQUIRED_NEWS_SECTIONS:
        _, _, pat, _ = entry
        if not any(pat.search(t) for t in h2_titles):
            missing.append(entry)
    return missing


def _regenerate_section(
    name: str, topic: str, today: date,
) -> str | None:
    """Re-roll a single missing news section. Returns the validated
    ``<section>…</section>`` HTML, or ``None`` if the model didn't produce
    something usable."""
    system = (
        "You are filling in ONE missing news section for Linh's daily "
        "newspaper edition. Use the web_search tool aggressively to find "
        "fresh items dated within 1–2 days of the target date. "
        "Return STRICT JSON {\"html\": \"<section>...</section>\"} containing "
        "exactly one <section> element with: an <h2> title (use the exact "
        "title given below, including its emoji), and AT LEAST 3 <article> "
        "children, each with an <h3> headline, a one-paragraph blurb, and a "
        "single Sources element at the end. The Sources element is either "
        "<a class=\"sources\" href=\"…\" target=\"_blank\" rel=\"noopener\">"
        "SOURCES</a> for a single source, or a tooltip popup span for "
        "multiple. Do NOT wrap in <html>/<head>/<body>/<style>; do NOT use "
        "<cite> tags; do NOT print citation numbers like [1]."
    )
    user = (
        f"Target date: {today.isoformat()}.\n"
        f"Section title (use verbatim as the <h2>): {name}\n\n"
        f"Topic: {topic}\n\n"
        "Run multiple web_search queries until you have at least 3 fresh, "
        "distinct articles. Return only the single <section> element."
    )
    try:
        result = claude_client.call_with_schema(
            system=system,
            user=user,
            schema=claude_client.EDITION_SCHEMA,
            schema_name="return_section",
            schema_description=(
                "Return the requested news section as one <section> element."
            ),
            extra_tools=[claude_client.WEB_SEARCH_TOOL],
            max_tokens=8000,
        )
    except Exception:  # noqa: BLE001
        log.exception("Re-roll for missing section %r failed", name)
        return None

    raw = (result or {}).get("html", "")
    if not isinstance(raw, str) or not raw.strip():
        log.warning("Re-roll for %r returned empty html", name)
        return None
    cleaned = _strip_document_wrapper(raw)
    m = _SECTION_BLOCK_RE.search(cleaned)
    if not m:
        log.warning("Re-roll for %r had no <section> block", name)
        return None
    section_html = m.group(0)
    if not _H2_RE.search(section_html):
        log.warning("Re-roll for %r had no <h2>", name)
        return None
    n_articles = len(re.findall(r"<article(?:\s[^>]*)?>", section_html, re.I))
    if n_articles < 1:
        log.warning("Re-roll for %r had no <article> children", name)
        return None
    log.info("Re-roll for %r succeeded (%d articles)", name, n_articles)
    return section_html


def _splice_section(html: str, key: str, section_html: str) -> str:
    """Insert ``section_html`` into ``html`` at the correct position relative
    to the other required news sections."""
    order = [k for k, _, _, _ in _REQUIRED_NEWS_SECTIONS]
    target_idx = order.index(key)

    # Walk the existing sections in document order; record where to splice.
    insert_after_end: int | None = None
    insert_before_start: int | None = None
    for sec in _SECTION_BLOCK_RE.finditer(html):
        sec_html = sec.group()
        m = _H2_RE.search(sec_html)
        if not m:
            continue
        title = _TAG_STRIP.sub("", m.group(1)).strip()
        for k, _, pat, _ in _REQUIRED_NEWS_SECTIONS:
            if pat.search(title):
                kidx = order.index(k)
                if kidx < target_idx:
                    insert_after_end = sec.end()
                elif kidx > target_idx and insert_before_start is None:
                    insert_before_start = sec.start()
                break

    if insert_before_start is not None:
        return (
            html[:insert_before_start]
            + section_html + "\n"
            + html[insert_before_start:]
        )
    if insert_after_end is not None:
        return (
            html[:insert_after_end]
            + "\n" + section_html
            + html[insert_after_end:]
        )
    # Fallback: append just before </div></aside transition (end of .flow).
    flow_close = re.search(r"</div\s*>\s*<aside", html, re.IGNORECASE)
    if flow_close:
        return (
            html[:flow_close.start()]
            + section_html + "\n"
            + html[flow_close.start():]
        )
    return html + "\n" + section_html


def _backfill_missing_sections(html: str, today: date) -> str:
    """Detect required news sections that the LLM omitted, re-roll each in a
    focused call, and splice valid responses into ``html`` at the right
    position. If a re-roll fails or returns garbage, leave the HTML alone for
    that section — never insert an empty placeholder shell."""
    missing = _detect_missing_sections(html)
    if not missing:
        return html
    log.warning(
        "Edition missing %d required section(s): %s — attempting re-roll",
        len(missing), [k for k, _, _, _ in missing],
    )
    for key, name, _pat, topic in missing:
        with _step(f"reroll_section:{key}"):
            section_html = _regenerate_section(name, topic, today)
        if section_html:
            html = _splice_section(html, key, section_html)
            log.info("Backfilled missing section %s inline", key)
        else:
            log.warning(
                "Could not backfill section %s — storing edition without it",
                key,
            )
    return html


def _upsert_edition(
    s: Session, day: date, html: str, pdf_bytes: bytes, pdf_html: str | None = None,
    *, weather_forecast: dict | None = None, weather_alerts: list | None = None,
) -> None:
    now = datetime.now(UTC)
    if s.bind.dialect.name == "postgresql":
        stmt = pg_insert(Edition).values(
            date=day, html=html, pdf_html=pdf_html, pdf=pdf_bytes, generated_at=now,
            weather_forecast_json=weather_forecast or None,
            weather_alerts_json=weather_alerts or None,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[Edition.date],
            set_={
                "html": stmt.excluded.html,
                "pdf_html": stmt.excluded.pdf_html,
                "pdf": stmt.excluded.pdf,
                "generated_at": stmt.excluded.generated_at,
                "weather_forecast_json": stmt.excluded.weather_forecast_json,
                "weather_alerts_json": stmt.excluded.weather_alerts_json,
            },
        )
        s.execute(stmt)
    else:
        # Fallback path used by tests / sqlite.
        s.execute(text("DELETE FROM editions WHERE date = :d"), {"d": day})
        s.add(Edition(
            date=day, html=html, pdf_html=pdf_html, pdf=pdf_bytes, generated_at=now,
            weather_forecast_json=weather_forecast or None,
            weather_alerts_json=weather_alerts or None,
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
        else:
            # Persist the failure for any slot (not just refresh) so cron
            # failures surface in the UI's freshness endpoint instead of
            # disappearing into the worker's tmpfs log.
            cache.set_edition_refresh_error(error_msg)
        if args.slot == "refresh":
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
