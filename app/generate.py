from __future__ import annotations

import argparse
import contextlib
import hashlib
import logging
import re
import sys
import time
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import (
    calendar_oauth,
    calendar_summary,
    claude_client,
    html_renderer,
    images,
    og_image,
    overlays,
    pdf,
    pdf_renderer,
    prefs,
    weather,
)
from app import movies as movies_mod
from app.db import Edition, SubsectionImage, session_factory
from app.llm_schema import (
    MIN_STOCK_SOURCES,
    MIN_SUBSECTIONS,
    SECTION_KEYS,
    SECTION_REROLL_SCHEMA,
    SECTION_TITLES,
    SECTION_TOPIC_HINTS,
)
from app.pdf import _PLACEHOLDER_PDF
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
        linhnews = claude_client.generate_edition(template, ctx)
    _validate_and_log(linhnews)

    with _step("backfill_missing_sections"):
        linhnews = _backfill_missing_sections(linhnews, today)

    with _step("fetch_subsection_images"), Maker() as s:
        image_bytes_by_id = _fetch_and_persist_images(s, today, linhnews)

    with _step("html_renderer.render_edition_html"):
        html_body = html_renderer.render_edition_html(linhnews)

    # Build the weather strip used in the PDF (baked at generation time;
    # the screen page substitutes its own at view time so 'Now' stays
    # within the 1-hour cache window).
    with _step("build_pdf_weather_strip"), Maker() as s:
        pdf_now = weather.get_now_cached(s, settings.weather_coords)
    pdf_weather_strip = weather.build_weather_strip(
        pdf_now,
        weather_forecast,
        weather_alerts,
    )
    with _step("pdf_renderer.build_pdf_html"):
        pdf_html = pdf_renderer.build_pdf_html(
            linhnews,
            pdf_calendar_html=pdf_calendar_html,
            pdf_movies_html=pdf_movies_html,
            weather_strip_html=pdf_weather_strip,
            today=today,
            image_bytes_by_id=image_bytes_by_id,
        )
    # Snapshot the print HTML *exactly* as it goes into WeasyPrint, so we
    # can inspect missing-image and overflow problems after the fact.
    try:
        from pathlib import Path

        snap_dir = Path(__file__).resolve().parent.parent / "logs"
        snap_dir.mkdir(exist_ok=True)
        snap = snap_dir / f"pdf-html-{slot}-{int(datetime.now(UTC).timestamp())}.html"
        snap.write_text(pdf_html, encoding="utf-8")
        log.info(
            "Saved pre-WeasyPrint pdf_html: %s (%d bytes, %d <img> tags)",
            snap,
            len(pdf_html),
            pdf_html.lower().count("<img"),
        )
    except Exception:
        log.exception("Could not snapshot pdf_html")

    with _step("html_to_pdf"):
        pdf_bytes = pdf.html_to_pdf(pdf_html)
    try:
        from pathlib import Path

        snap_dir = Path(__file__).resolve().parent.parent / "logs"
        snap_dir.mkdir(exist_ok=True)
        ts = int(datetime.now(UTC).timestamp())
        digest = hashlib.sha256(pdf_bytes).hexdigest()[:10]
        pdf_snap = snap_dir / f"pdf-{slot}-{ts}-{digest}.pdf"
        pdf_snap.write_bytes(pdf_bytes)
        log.info("Saved generated PDF: %s (%d bytes)", pdf_snap, len(pdf_bytes))
    except Exception:
        log.exception("Could not snapshot pdf bytes")

    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
        raise RuntimeError(
            f"Refusing to upsert edition for {today}: PDF render produced "
            f"{len(pdf_bytes)} bytes, not a valid PDF"
        )
    if pdf_bytes == _PLACEHOLDER_PDF:
        raise RuntimeError(
            f"Refusing to upsert edition for {today}: PDF render returned the "
            "placeholder (WeasyPrint native libs missing or content overflowed "
            "even after dropping every droppable section)"
        )

    with _step("upsert_edition"), Maker() as s:
        _upsert_edition(
            s,
            today,
            html_body,
            pdf_bytes,
            pdf_html,
            content_json=linhnews,
            weather_forecast=weather_forecast,
            weather_alerts=weather_alerts,
        )
    log.info("Generated edition for %s (slot=%s)", today, slot)

    log.info("⏱  ── refresh pipeline end (total %.2fs) ──", time.monotonic() - overall_t0)
    return today


def _build_context(s: Session, today: date, slot: Slot) -> dict:
    settings = get_settings()
    horizon = today + timedelta(days=30)

    with _step("overlays (hidden movies + watchlist)"):
        hidden_movies = overlays.active_hidden_movie_titles(s, today)
        favorite_movies = overlays.favorite_movie_titles(s)
        watchlist = overlays.watchlist_symbols(s)

    with _step("prefs.get_allowed_ratings"):
        allowed_ratings = set(prefs.get_allowed_ratings(today, session=s))
    hidden_set = set(hidden_movies)
    pdf_movies_html = ""
    try:
        with _step("movies.get_movies(refresh_if_stale=True)"):
            cached_movies = movies_mod.get_movies(refresh_if_stale=True)
        with _step("movies.render_pdf_html"):
            pdf_movies_html = movies_mod.render_pdf_html(
                cached_movies,
                today,
                hidden_titles=hidden_set,
                allowed_ratings=allowed_ratings,
                favorite_titles=favorite_movies,
            )
    except Exception:  # noqa: BLE001
        log.exception("Movie cache refresh / render failed")

    # ── Calendar pipeline ────────────────────────────────────────────────
    events: list[dict] = []
    important_uids: set[str] = set()
    cal_names: dict[str, str] = {}
    try:
        with _step("overlays (calendar/event lookups)"):
            hidden_cals = overlays.hidden_calendar_ids(s)
            suppressed_uids = overlays.suppressed_event_uids(s)
            important = overlays.important_events_from(s, today)
            important_uids = {e["ical_uid"] for e in important if e.get("ical_uid")}

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
            events = calendar_oauth.dedupe_events(events)

        for ev in events:
            cn = cal_names.get(ev.get("calendar_id"), "")
            if calendar_oauth.is_auto_important(cn, ev.get("summary", "")):
                important_uids.add(ev.get("ical_uid", ""))

        events_by_day: dict[date, list[dict]] = {}
        for ev in events:
            day_str = (ev.get("start") or "")[:10]
            try:
                d = date.fromisoformat(day_str)
                events_by_day.setdefault(d, []).append(ev)
            except ValueError:
                pass
        emoji_use_batch = slot != "refresh"
        with _step(f"calendar_persist ({len(events_by_day)} days, emoji_batch={emoji_use_batch})"):
            calendar_summary.persist_events_for_days(
                s,
                events_by_day,
                use_batch=emoji_use_batch,
            )

    except Exception as e:  # noqa: BLE001 — never let calendar break generation
        log.warning("Calendar unavailable: %s", e)

    pdf_calendar_html = calendar_summary.build_pdf_calendar(events, today, important_uids)
    dorchester_text = calendar_summary.build_dorchester_event_list(events, cal_names)

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


# ── Validation / re-roll on the structured response ─────────────────────


def _validate_and_log(linhnews: dict) -> None:
    """Surface visible warnings for any structural shortfall — missing keys,
    short subsection counts, stocks below the source threshold. Never raises.
    """
    sections = linhnews.get("sections") or []
    keys_present = {s.get("key") for s in sections}
    missing = [k for k in SECTION_KEYS if k not in keys_present]
    if missing:
        log.warning("LLM omitted required sections: %s", missing)
    for sec in sections:
        n = len(sec.get("subsections") or [])
        if n < MIN_SUBSECTIONS:
            log.warning(
                "Section %r returned only %d subsection(s) (target ≥%d)",
                sec.get("key"),
                n,
                MIN_SUBSECTIONS,
            )
    for st in linhnews.get("stocks") or []:
        nsrc = len((st.get("why_it_moved") or {}).get("sources") or [])
        if nsrc < MIN_STOCK_SOURCES:
            log.warning(
                "Stock %s why_it_moved has only %d source(s) (target ≥%d)",
                st.get("ticker"),
                nsrc,
                MIN_STOCK_SOURCES,
            )


def _detect_missing_section_keys(linhnews: dict) -> list[str]:
    keys_present = {s.get("key") for s in (linhnews.get("sections") or [])}
    return [k for k in SECTION_KEYS if k not in keys_present]


def _regenerate_section(key: str, today: date) -> dict | None:
    """Re-roll one missing section and return a Section dict, or None."""
    title = SECTION_TITLES[key]
    topic = SECTION_TOPIC_HINTS[key]
    system = (
        "You are filling in ONE missing news section for Linh's daily "
        "newspaper edition. Use web_search aggressively to find fresh items "
        "dated within 1–2 days of the target date. Return STRICTLY the "
        "subsections list for this section — no HTML, no extra fields. "
        "Each subsection has plain-text title + text (use '- ' prefixes for "
        "bullet lines), 0..N image URL candidates, and ≥1 source."
    )
    user = (
        f"Target date: {today.isoformat()}.\n"
        f"Section key: {key}\n"
        f"Section title (informational only): {title}\n\n"
        f"Topic: {topic}\n\n"
        f"Run multiple web_search queries until you have at least "
        f"{MIN_SUBSECTIONS} fresh, distinct items."
    )
    try:
        result = claude_client.call_with_schema(
            system=system,
            user=user,
            schema=SECTION_REROLL_SCHEMA,
            schema_name="return_section",
            schema_description=("Return the subsections list for this single news section."),
            extra_tools=[claude_client.WEB_SEARCH_TOOL],
            max_tokens=8000,
        )
    except Exception:  # noqa: BLE001
        log.exception("Re-roll for missing section %r failed", key)
        return None
    subs = (result or {}).get("subsections") or []
    if not subs:
        log.warning("Re-roll for %r returned no subsections", key)
        return None
    log.info("Re-roll for %r succeeded (%d subsections)", key, len(subs))
    return {"key": key, "title": title, "subsections": subs}


def _backfill_missing_sections(linhnews: dict, today: date) -> dict:
    missing = _detect_missing_section_keys(linhnews)
    if not missing:
        return linhnews
    log.warning(
        "Edition missing %d required section(s): %s — attempting re-roll",
        len(missing),
        missing,
    )
    sections = list(linhnews.get("sections") or [])
    for key in missing:
        with _step(f"reroll_section:{key}"):
            section = _regenerate_section(key, today)
        if section:
            sections.append(section)
        else:
            log.warning(
                "Could not backfill section %s — storing edition without it",
                key,
            )
    linhnews["sections"] = sections
    return linhnews


# ── Image fetch ─────────────────────────────────────────────────────────


def _ensure_edition_stub(s: Session, day: date) -> None:
    """Insert an empty ``editions`` row for ``day`` if one doesn't already
    exist, so the ``subsection_images.edition_date`` FK has a parent. The
    real content is filled in later by ``_upsert_edition``."""
    if s.get(Edition, day) is not None:
        return
    s.add(
        Edition(
            date=day,
            html="",
            pdf=b"",
            pdf_html="",
            generated_at=datetime.now(UTC),
        )
    )
    s.commit()


_HEADLINE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'-]+")
_HEADLINE_STOPWORDS = {
    "about", "after", "again", "against", "amid", "around", "before", "behind",
    "below", "between", "could", "during", "every", "from", "have", "into",
    "more", "much", "must", "near", "next", "off", "once", "only", "other",
    "over", "same", "should", "since", "some", "still", "such", "than",
    "that", "their", "them", "then", "there", "these", "they", "this",
    "those", "through", "today", "tonight", "under", "until", "very", "what",
    "when", "where", "which", "while", "with", "within", "without", "would",
    "your", "says", "said", "just", "also", "been", "were", "will",
}


def _headline_keywords(text_value: str) -> set[str]:
    """Significant tokens from a headline / og title / alt — lowercase,
    length ≥4, alphabetic-leading, stopwords removed. Used to score whether
    an og:image's metadata refers to the same story as the subsection."""
    out: set[str] = set()
    for tok in _HEADLINE_TOKEN_RE.findall(text_value or ""):
        low = tok.lower()
        if len(low) >= 4 and low not in _HEADLINE_STOPWORDS:
            out.add(low)
    return out


def _is_image_relevant(headline: str, og: og_image.OgImage) -> bool:
    """True if the og:image's page title or alt text shares at least one
    significant keyword with the subsection headline. If the page exposed
    no title/alt at all, we trust the homepage filter and accept — this
    avoids false rejections on minimal sites."""
    headline_kw = _headline_keywords(headline)
    if not headline_kw:
        return True
    candidate_kw = _headline_keywords(og.page_title) | _headline_keywords(og.alt)
    if not candidate_kw:
        return True
    return bool(headline_kw & candidate_kw)


def _candidate_image_urls(sub: dict) -> list[str]:
    """Scrape og:image from each source URL, in order. Deduped, and
    filtered to images whose page metadata mentions at least one keyword
    from the subsection headline.

    The LLM no longer supplies image URLs — it hallucinated 100% 404s.
    We only trust og:image meta tags from the article pages themselves,
    and we reject anything that looks unrelated (a generic site banner on
    a homepage URL, an article about something else, etc.).
    """
    urls: list[str] = []
    seen: set[str] = set()
    headline = sub.get("title", "") or ""
    for src in sub.get("sources") or []:
        if not isinstance(src, dict):
            continue
        article_url = src.get("url")
        if not article_url:
            continue
        og = og_image.fetch_og_image(article_url)
        if og is None or og.url in seen:
            continue
        if not _is_image_relevant(headline, og):
            log.info(
                "og:image rejected — no headline overlap (headline=%r url=%s)",
                headline,
                og.url,
            )
            continue
        seen.add(og.url)
        urls.append(og.url)
    return urls


def _fetch_and_persist_images(
    s: Session,
    day: date,
    linhnews: dict,
) -> dict[int, tuple[bytes, str]]:
    """For each subsection, scrape og:image from its sources, download one
    that decodes, persist the bytes, and stamp the row id back onto the
    in-memory structure as ``subsection["image_id"]``.

    Returns a ``{image_id: (bytes, mime_type)}`` mapping the PDF renderer
    embeds inline as data URIs (avoiding the round-trip through HTTP).

    Wipes any pre-existing rows for ``day`` so re-runs don't accumulate.
    """
    _ensure_edition_stub(s, day)
    s.execute(delete(SubsectionImage).where(SubsectionImage.edition_date == day))
    s.commit()

    # Hashes seen on any *other* edition's images — used to drop generic
    # site banners that recur day after day. Today's rows were just deleted
    # above, so equality with ``day`` is a no-op; using != is conservative
    # in case a parallel run interleaves.
    reject_hashes: set[str] = set(
        h
        for h in s.execute(
            select(SubsectionImage.image_hash).where(
                SubsectionImage.image_hash.is_not(None),
                SubsectionImage.edition_date != day,
            )
        ).scalars()
        if h
    )

    image_bytes_by_id: dict[int, tuple[bytes, str]] = {}
    for section in linhnews.get("sections") or []:
        key = section.get("key", "")
        # Only the first subsection of each section is rendered with an
        # image (per pdf_renderer._render_section_for_pdf), so don't waste
        # network on the rest.
        subs = section.get("subsections") or []
        if not subs:
            continue
        idx, sub = 0, subs[0]
        urls = _candidate_image_urls(sub)
        if not urls:
            continue
        try:
            fetched = images.fetch_one(urls, reject_hashes=reject_hashes)
        except Exception:  # noqa: BLE001
            log.exception("Image fetch raised for %s/%d", key, idx)
            continue
        if fetched is None:
            log.info("No usable image for %s/%d (%d candidates)", key, idx, len(urls))
            continue
        # Within this single run, also reject hashes we've already used —
        # two subsections in the same edition shouldn't share an image.
        # This doesn't block same-day re-runs because the prior run's rows
        # were deleted above before this loop started.
        reject_hashes.add(fetched.sha256)
        row = SubsectionImage(
            edition_date=day,
            section_key=key,
            subsection_idx=idx,
            bytes_=fetched.bytes_,
            mime_type=fetched.mime_type,
            width=fetched.width,
            height=fetched.height,
            image_hash=fetched.sha256,
        )
        s.add(row)
        s.flush()  # populate row.id without committing
        sub["image_id"] = row.id
        image_bytes_by_id[row.id] = (fetched.bytes_, fetched.mime_type)
    s.commit()
    log.info("Persisted %d subsection images for %s", len(image_bytes_by_id), day)
    return image_bytes_by_id


def _upsert_edition(
    s: Session,
    day: date,
    html: str,
    pdf_bytes: bytes,
    pdf_html: str | None = None,
    *,
    content_json: dict | None = None,
    weather_forecast: dict | None = None,
    weather_alerts: list | None = None,
) -> None:
    now = datetime.now(UTC)
    if s.bind.dialect.name == "postgresql":
        stmt = pg_insert(Edition).values(
            date=day,
            html=html,
            pdf_html=pdf_html,
            pdf=pdf_bytes,
            generated_at=now,
            content_json=content_json,
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
                "content_json": stmt.excluded.content_json,
                "weather_forecast_json": stmt.excluded.weather_forecast_json,
                "weather_alerts_json": stmt.excluded.weather_alerts_json,
            },
        )
        s.execute(stmt)
    else:
        # Fallback path used by tests / sqlite.
        s.execute(text("DELETE FROM editions WHERE date = :d"), {"d": day})
        s.add(
            Edition(
                date=day,
                html=html,
                pdf_html=pdf_html,
                pdf=pdf_bytes,
                generated_at=now,
                content_json=content_json,
                weather_forecast_json=weather_forecast or None,
                weather_alerts_json=weather_alerts or None,
            )
        )
    s.commit()


def _cli() -> int:
    import time
    from pathlib import Path

    p = argparse.ArgumentParser()
    p.add_argument("slot", choices=["morning", "evening", "refresh"])
    p.add_argument("--date", default=None, help="Override date (YYYY-MM-DD)")
    args = p.parse_args()

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
            cache.record_refresh_duration(elapsed)
        else:
            cache.set_edition_refresh_error(error_msg)
        if args.slot == "refresh":
            cache.end_edition_refresh()
    return 0 if error_msg is None else 1


def _summarize_error(e: BaseException) -> str:
    name = type(e).__name__
    msg = str(e).strip()
    if "credit balance" in msg.lower():
        return "Anthropic credit balance too low — top up at console.anthropic.com."
    if "rate limit" in msg.lower() or "429" in msg:
        return "Anthropic rate limit hit — wait a minute and try again."
    if len(msg) > 220:
        msg = msg[:217] + "..."
    return f"{name}: {msg}" if msg else name


if __name__ == "__main__":
    sys.exit(_cli())
