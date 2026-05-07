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
    prompt_template,
    prompts,
    weather,
    weather_prose,
)
from app import movies as movies_mod
from app import (
    user_settings as user_settings_mod,
)
from app.db import DebugEdition, Edition, SubsectionImage, session_factory
from app.llm_schema import (
    MIN_STOCK_SOURCES,
    MIN_SUBSECTIONS,
    SECTION_REROLL_SCHEMA,
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


def run(
    slot: Slot,
    today: date | None = None,
    email: str | None = None,
    *,
    linhnews_override: dict | None = None,
    debug_generated_at_override: datetime | None = None,
) -> date:
    """Generate today's edition and upsert into editions. Returns the date row.

    If ``linhnews_override`` is supplied, skip the LLM call and use that
    structured response instead. Used by ``--from-debug`` to re-render an
    edition whose post-LLM pipeline failed (e.g. OOM during image fetch),
    without burning another LLM call. Build-context still runs so the rail,
    weather, and image fetch use today's fresh data.
    """
    today = today or local_today()
    settings = get_settings()
    Maker = session_factory()
    target_email = (email or settings.admin_email).lower()
    with Maker() as s:
        usettings = user_settings_mod.get(s, target_email)
    masthead_name = (usettings.get("display_name") or "the reader").strip() or "the reader"
    weather_coords = (usettings.get("weather_coords") or settings.weather_coords).strip()
    user_sections = list(usettings.get("sections") or [])
    user_children = list(usettings.get("children") or [])

    overall_t0 = time.monotonic()
    log.info(
        "⏱  ── refresh pipeline begin (slot=%s, date=%s, email=%s) ──",
        slot,
        today,
        target_email,
    )

    with Maker() as s:
        cached_rail = _load_cached_rail(s, today, target_email)
    if cached_rail is not None:
        log.info(
            "Reusing cached PDF rail for %s (version=%d) — skipping movie/calendar rail rebuild",
            today,
            pdf_renderer.PDF_RAIL_VERSION,
        )

    with _step("build_context"), Maker() as s:
        ctx = _build_context(
            s, today, slot,
            email=target_email,
            cached_rail=cached_rail,
            weather_coords=weather_coords,
        )
    pdf_movies_html = ctx.pop("_pdf_movies_html", "")
    pdf_calendar_html = ctx.pop("_pdf_calendar_html", "")
    weather_forecast = ctx.pop("_weather_forecast", {})
    weather_alerts = ctx.pop("_weather_alerts", [])

    rendered_prompt = prompt_template.build_prompt(
        display_name=masthead_name,
        today=today,
        sections=user_sections,
        children=user_children,
        watchlist_stocks=ctx.get("WATCHLIST_STOCKS") or [],
        dorchester_events=ctx.get("DORCHESTER_CALENDAR_EVENTS") or "",
    )

    if linhnews_override is not None:
        log.info("Re-render mode: reusing structured LLM response from debug_editions")
        linhnews = linhnews_override
    else:
        with _step("claude_generate_edition"):
            linhnews = claude_client.generate_edition(rendered_prompt)
    _validate_and_log(linhnews, expected_keys=[s.get("key") for s in user_sections])

    with _step("backfill_missing_sections"):
        linhnews = _backfill_missing_sections(linhnews, today, user_sections, masthead_name)

    # Persist a debug copy of the structured response *before* anything that
    # can fail downstream (image fetch, PDF render). Deleted after a successful
    # upsert; otherwise self-expires via the 3-day TTL. Re-render mode skips
    # this write and keeps the originally loaded row's timestamp so the
    # cleanup at the end drops the right row.
    if linhnews_override is None:
        debug_generated_at = datetime.now(UTC)
        with Maker() as s:
            _save_debug_content(s, today, target_email, debug_generated_at, linhnews)
    else:
        assert debug_generated_at_override is not None
        debug_generated_at = debug_generated_at_override

    with _step("fetch_subsection_images"), Maker() as s:
        image_bytes_by_id = _fetch_and_persist_images(s, today, target_email, linhnews)

    with _step("html_renderer.render_edition_html"):
        html_body = html_renderer.render_edition_html(linhnews)

    # Build the weather strip used in the PDF (baked at generation time;
    # the screen page substitutes its own at view time so 'Now' stays
    # within the 1-hour cache window).
    with _step("build_pdf_weather_strip"), Maker() as s:
        pdf_now = weather.get_now_cached(s, weather_coords)
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
            weather_prose_html=weather_forecast.get("prose_html", "") or "",
            today=today,
            image_bytes_by_id=image_bytes_by_id,
            masthead_name=masthead_name,
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

    skip_phase1 = cached_rail is not None
    with _step("html_to_pdf"):
        pdf_bytes, phase1_font_pt, trimmed_rail = pdf.html_to_pdf_ex(
            pdf_html,
            skip_phase1=skip_phase1,
        )
    # When Phase 1 was skipped, keep the previously cached Phase-1 font as
    # the proof-of-fit; otherwise persist the freshly chosen one.
    persisted_font_pt = (
        (cached_rail or {}).get("font_pt") if skip_phase1 else phase1_font_pt
    )
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

    # Persist the rail that Phase 1 *actually fit*, not the original
    # pre-trim rail strings. Phase 1 may have dropped movie cards or
    # calendar events to make rail+chrome fit at MIN font; caching the
    # untrimmed originals would re-introduce that content the next time
    # this rail is reused with skip_phase1=True, and Phase 2 (which only
    # trims news) wouldn't be able to recover. When Phase 1 was skipped,
    # ``trimmed_rail`` is None and we keep the already-cached rail
    # strings (cached_rail) — they previously passed Phase 1.
    if trimmed_rail is not None:
        rail_calendar_html = trimmed_rail.get("calendar_html") or ""
        rail_movies_html = trimmed_rail.get("movies_html") or ""
    else:
        rail_calendar_html = pdf_calendar_html
        rail_movies_html = pdf_movies_html
    rail_to_persist = {
        "version": pdf_renderer.PDF_RAIL_VERSION,
        "calendar_html": rail_calendar_html,
        "movies_html": rail_movies_html,
        # Body font (in pt) Phase 1 (rail-only fit) landed on. Stored as
        # proof that the cached rail fits at one of the supported fonts;
        # next same-day run will skip Phase 1. Phase 2 runs normally and
        # is unaffected by this value.
        "font_pt": persisted_font_pt,
    }
    with _step("upsert_edition"), Maker() as s:
        _upsert_edition(
            s,
            today,
            target_email,
            html_body,
            pdf_bytes,
            pdf_html,
            content_json=linhnews,
            weather_forecast=weather_forecast,
            weather_alerts=weather_alerts,
            pdf_rail=rail_to_persist,
        )
        _delete_debug_content(s, today, target_email, debug_generated_at)
    log.info("Generated edition for %s (slot=%s)", today, slot)

    log.info("⏱  ── refresh pipeline end (total %.2fs) ──", time.monotonic() - overall_t0)
    return today


_DEBUG_TTL = timedelta(days=3)


def _save_debug_content(
    s: Session,
    day: date,
    email: str,
    generated_at: datetime,
    content_json: dict,
) -> None:
    """Insert a row into ``debug_editions`` retaining the LLM response for
    this run. Also opportunistically purges any rows past their TTL so the
    table self-trims without a separate cleaner."""
    s.execute(delete(DebugEdition).where(DebugEdition.expires_at < generated_at))
    s.add(
        DebugEdition(
            date=day,
            email=email,
            generated_at=generated_at,
            content_json=content_json,
            expires_at=generated_at + _DEBUG_TTL,
            failure_reason=None,
        )
    )
    s.commit()


def _load_latest_debug(day: date, email: str) -> tuple[dict, datetime]:
    """Return ``(content_json, generated_at)`` for the newest debug row of
    ``(day, email)``. Raises if none exists."""
    Maker = session_factory()
    with Maker() as s:
        row = s.execute(
            select(DebugEdition)
            .where(DebugEdition.date == day, DebugEdition.email == email)
            .order_by(DebugEdition.generated_at.desc())
            .limit(1)
        ).scalar_one_or_none()
    if row is None:
        raise RuntimeError(
            f"No debug_editions row found for ({day}, {email}); cannot re-render"
        )
    return dict(row.content_json), row.generated_at


def _delete_debug_content(
    s: Session, day: date, email: str, generated_at: datetime
) -> None:
    """Drop the debug row written at the start of this run — the upsert
    succeeded so the structured response now lives on the real edition row."""
    s.execute(
        delete(DebugEdition).where(
            DebugEdition.date == day,
            DebugEdition.email == email,
            DebugEdition.generated_at == generated_at,
        )
    )
    s.commit()


def _load_cached_rail(s: Session, today: date, email: str) -> dict | None:
    """Return the previously persisted PDF rail for ``(today, email)`` if it
    was produced by the current ``PDF_RAIL_VERSION``; ``None`` otherwise.

    The shape on disk is ``{"version", "calendar_html", "movies_html"}``.
    A version mismatch means the renderer has changed and we must rebuild.
    """
    row = s.get(Edition, (today, email))
    if row is None:
        return None
    rail = row.pdf_rail_json
    if not isinstance(rail, dict):
        return None
    if rail.get("version") != pdf_renderer.PDF_RAIL_VERSION:
        return None
    font_pt = rail.get("font_pt")
    try:
        font_pt = float(font_pt) if font_pt is not None else None
    except (TypeError, ValueError):
        font_pt = None
    return {
        "calendar_html": rail.get("calendar_html") or "",
        "movies_html": rail.get("movies_html") or "",
        "font_pt": font_pt,
    }


def _build_context(
    s: Session,
    today: date,
    slot: Slot,
    *,
    email: str | None = None,
    cached_rail: dict | None = None,
    weather_coords: str | None = None,
) -> dict:
    settings = get_settings()
    target_email = (email or settings.admin_email).lower()
    horizon = today + timedelta(days=30)
    coords = weather_coords or settings.weather_coords

    with _step("overlays (hidden movies + watchlist)"):
        hidden_movies = overlays.active_hidden_movie_titles(s, target_email, today)
        favorite_movies = overlays.favorite_movie_titles(s, target_email)
        watchlist = overlays.watchlist_symbols(s, target_email)

    with _step("prefs.get_allowed_ratings"):
        allowed_ratings = set(prefs.get_allowed_ratings(today, session=s))
    hidden_set = set(hidden_movies)
    if cached_rail is not None:
        pdf_movies_html = cached_rail.get("movies_html", "")
    else:
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
            hidden_cals = overlays.hidden_calendar_ids(s, target_email)
            suppressed_uids = overlays.suppressed_event_uids(s, target_email)
            important = overlays.important_events_from(s, target_email, today)
            important_uids = {e["ical_uid"] for e in important if e.get("ical_uid")}

        with _step("google list_calendars"):
            all_calendars = calendar_oauth.list_calendars(s, target_email)
            hidden_ids = {c["id"] for c in hidden_cals}
            active_ids = [c["id"] for c in all_calendars if c["id"] not in hidden_ids]
            cal_names = {c["id"]: c["name"] for c in all_calendars}

        with _step("google fetch_events (today..+30d)"):
            events = calendar_oauth.fetch_events(
                s, active_ids, today, horizon,
                calendar_names=cal_names, email=target_email,
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
                target_email,
                events_by_day,
                use_batch=emoji_use_batch,
            )

    except Exception as e:  # noqa: BLE001 — never let calendar break generation
        log.warning("Calendar unavailable: %s", e)

    if cached_rail is not None:
        pdf_calendar_html = cached_rail.get("calendar_html", "")
    else:
        pdf_calendar_html = calendar_summary.build_pdf_calendar(events, today, important_uids)
    dorchester_text = calendar_summary.build_dorchester_event_list(events, cal_names)

    with _step("NWS fetch_forecast"):
        weather_forecast = weather.fetch_forecast(coords)
    with _step("NWS fetch_alerts"):
        weather_alerts = weather.fetch_alerts(coords)
    # Render the human-sounding prose paragraph once per generation. The
    # picked phrases get baked into ``weather_forecast["prose_html"]`` so
    # the HTML viewer and the PDF render the same text. Empty string when
    # the seed table is unpopulated or H/L data is missing — callers fall
    # back to the legacy strip in that case.
    with _step("weather_prose.render"):
        weather_forecast["prose_html"] = weather_prose.render_prose_html(
            weather_forecast,
            weather_alerts,
            s,
        )

    return {
        "WATCHLIST_STOCKS": watchlist,
        "DORCHESTER_CALENDAR_EVENTS": dorchester_text,
        "_pdf_movies_html": pdf_movies_html,
        "_pdf_calendar_html": pdf_calendar_html,
        "_weather_forecast": weather_forecast,
        "_weather_alerts": weather_alerts,
    }


# ── Validation / re-roll on the structured response ─────────────────────


def _validate_and_log(linhnews: dict, *, expected_keys: list[str]) -> None:
    """Surface visible warnings for any structural shortfall — missing keys,
    short subsection counts, stocks below the source threshold. Never raises.
    """
    sections = linhnews.get("sections") or []
    keys_present = {s.get("key") for s in sections}
    missing = [k for k in expected_keys if k and k not in keys_present]
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


def _regenerate_section(sec: dict, today: date, display_name: str) -> dict | None:
    """Re-roll one missing section and return a Section dict, or None."""
    key = sec.get("key", "")
    title = sec.get("title", "")
    topic = sec.get("description", "") or ""
    target = int(sec.get("subsection_count", MIN_SUBSECTIONS))
    system = prompts.render("section_reroll_system", display_name=display_name)
    user = prompts.render(
        "section_reroll_user",
        today_iso=today.isoformat(),
        key=key,
        title=title,
        topic=topic,
        target=target,
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


def _backfill_missing_sections(
    linhnews: dict,
    today: date,
    user_sections: list[dict],
    display_name: str = "the reader",
) -> dict:
    keys_present = {s.get("key") for s in (linhnews.get("sections") or [])}
    missing = [s for s in user_sections if s.get("key") and s.get("key") not in keys_present]
    if not missing:
        return linhnews
    log.warning(
        "Edition missing %d required section(s): %s — attempting re-roll",
        len(missing),
        [s.get("key") for s in missing],
    )
    sections = list(linhnews.get("sections") or [])
    for sec in missing:
        with _step(f"reroll_section:{sec.get('key')}"):
            section = _regenerate_section(sec, today, display_name)
        if section:
            sections.append(section)
        else:
            log.warning(
                "Could not backfill section %s — storing edition without it",
                sec.get("key"),
            )
    linhnews["sections"] = sections
    return linhnews


# ── Image fetch ─────────────────────────────────────────────────────────


def _ensure_edition_stub(s: Session, day: date, email: str) -> None:
    """Insert an empty ``editions`` row for ``(day, email)`` if one doesn't
    already exist, so the ``subsection_images`` FK has a parent. The real
    content is filled in later by ``_upsert_edition``."""
    if s.get(Edition, (day, email)) is not None:
        return
    s.add(
        Edition(
            date=day,
            email=email,
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
    email: str,
    linhnews: dict,
) -> dict[int, tuple[bytes, str]]:
    """For each subsection, scrape og:image from its sources, download one
    that decodes, persist the bytes, and stamp the row id back onto the
    in-memory structure as ``subsection["image_id"]``.

    Returns a ``{image_id: (bytes, mime_type)}`` mapping the PDF renderer
    embeds inline as data URIs (avoiding the round-trip through HTTP).

    Wipes any pre-existing rows for ``day`` so re-runs don't accumulate.
    """
    _ensure_edition_stub(s, day, email)
    s.execute(
        delete(SubsectionImage).where(
            SubsectionImage.edition_date == day,
            SubsectionImage.edition_email == email,
        )
    )
    s.commit()

    # Hashes seen on any *other* (day, email) edition's images — used to
    # drop generic site banners that recur day after day. The current
    # (day, email)'s rows were just deleted above, so excluding them is
    # conservative in case a parallel run interleaves.
    reject_hashes: set[str] = set(
        h
        for h in s.execute(
            select(SubsectionImage.image_hash).where(
                SubsectionImage.image_hash.is_not(None),
                ~(
                    (SubsectionImage.edition_date == day)
                    & (SubsectionImage.edition_email == email)
                ),
            )
        ).scalars()
        if h
    )

    image_bytes_by_id: dict[int, tuple[bytes, str]] = {}
    for section in linhnews.get("sections") or []:
        key = section.get("key", "")
        # At most one image per section, attached to the earliest
        # subsection whose sources yield a usable image. We walk the
        # subsections in order and stop as soon as one succeeds.
        subs = section.get("subsections") or []
        for idx, sub in enumerate(subs):
            urls = _candidate_image_urls(sub)
            if not urls:
                continue
            try:
                fetched = images.fetch_one(urls, reject_hashes=reject_hashes)
            except Exception:  # noqa: BLE001
                log.exception("Image fetch raised for %s/%d", key, idx)
                continue
            if fetched is None:
                log.info(
                    "No usable image for %s/%d (%d candidates)", key, idx, len(urls)
                )
                continue
            # Within this single run, also reject hashes we've already used —
            # two subsections in the same edition shouldn't share an image.
            # This doesn't block same-day re-runs because the prior run's
            # rows were deleted above before this loop started.
            reject_hashes.add(fetched.sha256)
            row = SubsectionImage(
                edition_date=day,
                edition_email=email,
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
            break  # one image per section — stop at the first success
    s.commit()
    log.info("Persisted %d subsection images for %s", len(image_bytes_by_id), day)
    return image_bytes_by_id


def _upsert_edition(
    s: Session,
    day: date,
    email: str,
    html: str,
    pdf_bytes: bytes,
    pdf_html: str | None = None,
    *,
    content_json: dict | None = None,
    weather_forecast: dict | None = None,
    weather_alerts: list | None = None,
    pdf_rail: dict | None = None,
) -> None:
    now = datetime.now(UTC)
    if s.bind.dialect.name == "postgresql":
        stmt = pg_insert(Edition).values(
            date=day,
            email=email,
            html=html,
            pdf_html=pdf_html,
            pdf=pdf_bytes,
            generated_at=now,
            content_json=content_json,
            weather_forecast_json=weather_forecast or None,
            weather_alerts_json=weather_alerts or None,
            pdf_rail_json=pdf_rail or None,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[Edition.date, Edition.email],
            set_={
                "html": stmt.excluded.html,
                "pdf_html": stmt.excluded.pdf_html,
                "pdf": stmt.excluded.pdf,
                "generated_at": stmt.excluded.generated_at,
                "content_json": stmt.excluded.content_json,
                "weather_forecast_json": stmt.excluded.weather_forecast_json,
                "weather_alerts_json": stmt.excluded.weather_alerts_json,
                "pdf_rail_json": stmt.excluded.pdf_rail_json,
            },
        )
        s.execute(stmt)
    else:
        # Fallback path used by tests / sqlite.
        s.execute(
            text("DELETE FROM editions WHERE date = :d AND email = :e"),
            {"d": day, "e": email},
        )
        s.add(
            Edition(
                date=day,
                email=email,
                html=html,
                pdf_html=pdf_html,
                pdf=pdf_bytes,
                generated_at=now,
                content_json=content_json,
                weather_forecast_json=weather_forecast or None,
                weather_alerts_json=weather_alerts or None,
                pdf_rail_json=pdf_rail or None,
            )
        )
    s.commit()


def _cli() -> int:
    import time
    from pathlib import Path

    p = argparse.ArgumentParser()
    p.add_argument("slot", choices=["morning", "evening", "refresh"])
    p.add_argument("--date", default=None, help="Override date (YYYY-MM-DD)")
    p.add_argument(
        "--email",
        default=None,
        help="Generate the edition for this user's settings (default: admin)",
    )
    p.add_argument(
        "--from-debug",
        action="store_true",
        help=(
            "Skip the LLM call and reuse the most recent structured response "
            "from debug_editions for (date, email). Requires --email."
        ),
    )
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
        if args.from_debug:
            if not args.email:
                log.error("--from-debug requires --email")
                return 1
            override_day = target_date or local_today()
            override_linhnews, override_dt = _load_latest_debug(
                override_day, args.email.lower()
            )
            run(
                args.slot,
                today=target_date,
                email=args.email,
                linhnews_override=override_linhnews,
                debug_generated_at_override=override_dt,
            )
        elif args.email:
            run(args.slot, today=target_date, email=args.email)
        else:
            error_msg = _run_for_all_enabled_users(args.slot, target_date)
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


def _run_for_all_enabled_users(slot: Slot, target_date: date | None) -> str | None:
    """Cron entry: run the per-user pipeline for every user whose admin-set
    ``personalized_enabled`` flag is true and who has both signed in (so we
    have a refresh_token to fetch their calendar) and configured at least
    one section on the Data tab. Admin always runs even with an empty
    config — their credentials are seeded by the OAuth setup script.
    Errors on individual users are logged and skipped so one bad token
    doesn't abort the whole batch. Returns an aggregate error string when
    at least one user failed, else None.
    """
    from datetime import UTC
    from datetime import datetime as _dt

    from app.db import GoogleOAuth, UserSettings

    settings = get_settings()
    admin_email = settings.admin_email.lower()
    Maker = session_factory()
    with Maker() as s:
        rows = (
            s.execute(
                select(UserSettings)
                .join(GoogleOAuth, GoogleOAuth.email == UserSettings.email)
                .where(GoogleOAuth.refresh_token != "")
                .where(
                    (UserSettings.personalized_enabled.is_(True))
                    | (UserSettings.email == admin_email)
                )
            )
            .scalars()
            .all()
        )
        emails: list[str] = []
        skipped: list[tuple[str, str]] = []
        for r in rows:
            em = r.email.lower()
            if em == admin_email:
                emails.append(em)
                continue
            if not (r.sections_json or []):
                skipped.append((em, "no sections configured"))
                continue
            emails.append(em)
    if skipped:
        for em, reason in skipped:
            log.info("⏱  cron: skipping %s — %s", em, reason)
    log.info("⏱  cron: %d enabled user(s) → %s", len(emails), emails)
    failed: list[tuple[str, str]] = []
    for email in emails:
        try:
            run(slot, today=target_date, email=email)
            with Maker() as s:
                row = s.execute(
                    select(GoogleOAuth).where(GoogleOAuth.email == email)
                ).scalar_one_or_none()
                if row is not None:
                    row.last_refreshed_at = _dt.now(UTC)
                    s.commit()
        except Exception as e:  # noqa: BLE001 — never abort the batch
            log.exception("Per-user generate failed for %s", email)
            failed.append((email, _summarize_error(e)))
    if failed:
        joined = "; ".join(f"{e}: {m}" for e, m in failed)
        return f"{len(failed)}/{len(emails)} per-user runs failed — {joined}"
    return None


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
