"""Per-day calendar event persistence + view-time HTML rendering.

The refresh pipeline persists raw events per day into
``calendar_day_summaries.events_json`` and backfills any missing
title→emoji rows in ``event_emojis`` (via Claude Haiku). Nothing about
the rendered HTML is cached — every page view re-renders the calendar
from the persisted events plus the (DB-only, no-LLM) emoji map. This
guarantees that whenever the renderer changes, the next page view
reflects it without requiring cache invalidation.

The output is a single ``<div>`` per day::

    <div><strong>Friday, May 2:</strong> 9:00 AM 📚 Library visit
        • 12:00 PM 🍕 Lunch with team • 🎂 Dad's birthday</div>
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import claude_client

log = logging.getLogger(__name__)

# Last-resort keyword map used when the LLM didn't return an emoji for a
# title (whole batch failed, individual entry empty, etc). The map lives
# only in memory — we never persist its output to ``event_emojis``, so the
# next refresh gets another LLM chance to upgrade the result.
_FALLBACK_EMOJI_RULES: list[tuple[tuple[str, ...], str]] = [
    (("birthday",), "🎂"),
    (("anniversary",), "💞"),
    (("soccer",), "⚽"),
    (("dance",), "💃"),
    (("school", "elementary", "dorchester"), "🏫"),
    (("dentist",), "🦷"),
    (("doctor", "appointment", "checkup"), "🩺"),
    (("dinner",), "🍽"),
    (("lunch",), "🍱"),
    (("breakfast",), "🥐"),
    (("flight", "travel", "trip"), "✈️"),
    (
        ("holiday", "labor day", "memorial day", "thanksgiving", "christmas", "new year", "easter"),
        "🎉",
    ),
    (("water",), "💧"),
    (("delivery",), "📦"),
    (("photo",), "📷"),
    (("library",), "📚"),
]


def _fallback_emoji(title: str) -> str:
    """Keyword-based emoji guess used at render time when the LLM didn't
    supply one. Never written to the DB — only used in the live page.

    Returns ``""`` when no keyword matches; the renderer drops the emoji
    prefix entirely rather than printing a generic 📅 placeholder."""
    t = (title or "").lower()
    for keywords, emoji in _FALLBACK_EMOJI_RULES:
        if any(kw in t for kw in keywords):
            return emoji
    return ""


def _normalize_title(title: str) -> str:
    """Collapse whitespace + lowercase so trivial variants share one emoji."""
    return re.sub(r"\s+", " ", (title or "").strip().lower())


# ───────────────── Emoji lookup (LLM only on cache miss) ─────────────────

_EMOJI_SCHEMA = {
    "type": "object",
    "properties": {
        "emojis": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "emoji": {
                        "type": "string",
                        "description": (
                            "A single emoji glyph that best represents the "
                            "calendar event. Examples: 🎂 birthday, ⚽ soccer, "
                            "🏫 school, 🦷 dentist, 🩺 doctor, ✈️ flight, "
                            "📚 library, 🍕 lunch, 💃 dance. Use 📅 only "
                            "as a last resort."
                        ),
                    },
                },
                "required": ["title", "emoji"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["emojis"],
    "additionalProperties": False,
}

_EMOJI_SYSTEM = (
    "You assign a single representative emoji to each calendar event title. "
    "Pick the most evocative emoji from the title's subject — birthday → 🎂, "
    "soccer → ⚽, school → 🏫, dentist → 🦷, doctor → 🩺, flight/travel → ✈️, "
    "library → 📚, dance → 💃, dinner → 🍽, etc. Use 📅 only when no other "
    "emoji fits. Return one emoji per title, in the same order."
)
_EMOJI_MODEL = "claude-haiku-4-5-20251001"


# Schema used by the per-title BATCH path: each batch entry returns one
# emoji directly (no need to echo the title — we key by custom_id instead).
_EMOJI_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "emoji": {
            "type": "string",
            "description": (
                "A single emoji glyph that best represents the calendar "
                "event. Use 📅 only as a last resort."
            ),
        },
    },
    "required": ["emoji"],
    "additionalProperties": False,
}


def _emoji_lookup_single_call(missing: list[str]) -> dict[str, str]:
    """Low-latency path: one Haiku call returning all emojis at once.

    Used for user-triggered /refresh runs where wall-clock matters more
    than cost. Returns ``{title: emoji}`` for whatever titles the model
    answered — the caller fills gaps with the keyword fallback.
    """
    out = claude_client.call_with_schema(
        system=_EMOJI_SYSTEM,
        user=(
            "Return one representative emoji for each of these calendar "
            "event titles. Output via the `return_emojis` tool with one "
            "entry per title, in the same order:\n\n" + "\n".join(f"- {t}" for t in missing)
        ),
        schema=_EMOJI_SCHEMA,
        schema_name="return_emojis",
        schema_description="Map of calendar event titles to a single emoji.",
        max_tokens=1500,
        model=_EMOJI_MODEL,
    )
    by_norm = {_normalize_title(t): t for t in missing}
    result: dict[str, str] = {}
    for entry in out.get("emojis", []):
        title = (entry.get("title") or "").strip()
        emoji = (entry.get("emoji") or "").strip()
        if not title or not emoji:
            continue
        original = by_norm.get(_normalize_title(title))
        if original is not None:
            result[original] = emoji
    return result


def _emoji_lookup_batch(missing: list[str]) -> dict[str, str]:
    """Cost-optimised path: one Anthropic Batch with one entry per title.

    Used by cron runs (morning/evening) where the 5+ min batch latency is
    fine but the 50% cost discount and server-side fan-out matter. Each
    request asks for a single emoji; the title is encoded into ``custom_id``
    so we don't need the model to echo it.
    """
    from app.claude_client import _client

    client = _client()
    # custom_id must match ``^[a-zA-Z0-9_-]{1,64}$`` per Anthropic; hash the
    # title to a short stable id and keep a side-table back to the original.
    id_for: dict[str, str] = {}
    title_for: dict[str, str] = {}
    requests = []
    for t in missing:
        cid = "e_" + hashlib.sha256(t.encode("utf-8")).hexdigest()[:32]
        id_for[t] = cid
        title_for[cid] = t
        requests.append(
            {
                "custom_id": cid,
                "params": {
                    "model": _EMOJI_MODEL,
                    "max_tokens": 50,
                    "system": _EMOJI_SYSTEM,
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                f"Calendar event title: {t}\n\n"
                                "Call the return_emoji tool with one representative "
                                "emoji for this title."
                            ),
                        }
                    ],
                    "tools": [
                        {
                            "name": "return_emoji",
                            "description": "Return one emoji for the calendar event.",
                            "input_schema": _EMOJI_ITEM_SCHEMA,
                        }
                    ],
                    "tool_choice": {"type": "tool", "name": "return_emoji"},
                },
            }
        )

    batch = client.messages.batches.create(requests=requests)
    log.info("Emoji batch %s submitted (%d titles), polling…", batch.id, len(missing))

    deadline = time.monotonic() + 1800  # 30-min cap (Anthropic typically <10m)
    while True:
        if time.monotonic() > deadline:
            log.warning("Emoji batch %s exceeded 30-min cap; cancelling", batch.id)
            with contextlib.suppress(Exception):
                client.messages.batches.cancel(batch.id)
            raise TimeoutError(f"emoji batch {batch.id} timed out")
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        log.info("Emoji batch %s status=%s — sleeping 10s", batch.id, batch.processing_status)
        time.sleep(10)

    log.info("Emoji batch %s ended, reading results", batch.id)

    out: dict[str, str] = {}
    for entry in client.messages.batches.results(batch.id):
        original = title_for.get(entry.custom_id)
        if original is None:
            continue
        if getattr(entry.result, "type", None) != "succeeded":
            log.warning(
                "Emoji batch entry for %r: %s", original[:60], getattr(entry.result, "type", None)
            )
            continue
        msg = entry.result.message
        for block in msg.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "return_emoji":
                emoji = ((block.input or {}).get("emoji") or "").strip()
                if emoji:
                    out[original] = emoji
                break

    log.info("Emoji batch %s: %d/%d titles produced an emoji", batch.id, len(out), len(missing))
    return out


def emojis_for_titles(
    s: Session,
    titles: list[str],
    *,
    use_batch: bool = False,
) -> dict[str, str]:
    """Return ``{title: emoji}`` for every distinct title.

    DB cache first; new titles are looked up via Claude (Haiku). The lookup
    path depends on ``use_batch``:

    * ``use_batch=True`` — Anthropic Batch API (one entry per title). ~50%
      cheaper but adds 5+ min of latency. Use for cron-triggered runs.
    * ``use_batch=False`` — single consolidated Messages call. ~10s latency,
      full price. Use for user-triggered ``/refresh`` so the spinner doesn't
      run for half an hour.

    Either way, results are persisted to ``event_emojis`` so the next run
    is a pure cache hit. On any LLM failure we fall back to a small built-in
    keyword map rather than blocking edition generation.
    """
    from app.db import EventEmoji

    distinct = list({(t or "").strip() for t in titles if (t or "").strip()})
    if not distinct:
        return {}

    norm_for = {t: _normalize_title(t) for t in distinct}
    norms = list({norm_for[t] for t in distinct})

    # 1. Cache lookup.
    rows = s.execute(select(EventEmoji).where(EventEmoji.title_norm.in_(norms))).scalars().all()
    cached: dict[str, str] = {row.title_norm: row.emoji for row in rows}

    missing = [t for t in distinct if norm_for[t] not in cached]
    if not missing:
        return {t: cached[norm_for[t]] for t in distinct}

    # 2. LLM lookup — batched for cron, single-shot for refresh. ANY failure
    #    here is non-fatal: titles whose emoji we can't get just render without
    #    an emoji, and we DON'T persist a guess — so the next refresh gets
    #    another chance to look them up properly.
    llm_map: dict[str, str] = {}
    try:
        if use_batch:
            log.info("Emoji lookup: %d missing titles via Batch API", len(missing))
            llm_map = _emoji_lookup_batch(missing)
        else:
            log.info("Emoji lookup: %d missing titles via single call", len(missing))
            llm_map = _emoji_lookup_single_call(missing)
    except Exception:  # noqa: BLE001 — never fail edition generation on emoji lookup
        log.exception(
            "Emoji LLM lookup failed (use_batch=%s); %d titles will render "
            "without an emoji this run.",
            use_batch,
            len(missing),
        )

    # 3. Persist ONLY successful lookups. Titles the LLM didn't answer are
    #    left uncached so they'll be re-attempted on the next refresh.
    now = datetime.now(UTC)
    persisted = 0
    for t in missing:
        emoji = (llm_map.get(t) or "").strip()
        if not emoji:
            continue  # leave uncached → render without emoji, retry next run
        tn = norm_for[t]
        cached[tn] = emoji
        existing = s.get(EventEmoji, tn)
        if existing:
            existing.emoji = emoji
            existing.generated_at = now
        else:
            s.add(
                EventEmoji(
                    title_norm=tn,
                    emoji=emoji,
                    generated_at=now,
                )
            )
        persisted += 1
    if persisted:
        s.commit()
    if persisted < len(missing):
        log.info(
            "Emoji lookup: %d/%d new titles got an emoji this run (%d will retry next time).",
            persisted,
            len(missing),
            len(missing) - persisted,
        )

    # Titles still missing from `cached` will simply be absent from the
    # returned dict — `render_day_html` handles that by skipping the emoji.
    return {t: cached[norm_for[t]] for t in distinct if norm_for[t] in cached}


# ───────────────────────── HTML rendering ───────────────────────────


def _format_time(start: str) -> str | None:
    """Return ``"9:00 AM"``-style time for a timed event, or None for all-day."""
    if "T" not in start:
        return None
    try:
        dt = datetime.fromisoformat(start)
    except ValueError:
        return None
    h = dt.hour % 12 or 12
    return f"{h}:{dt.strftime('%M')} {'AM' if dt.hour < 12 else 'PM'}"


def render_day_html(day: date, events: list[dict], emoji_for: dict[str, str]) -> str:
    """Return the one-line ``<div>…</div>`` for a single calendar day.

    Format: ``Day, Month D: time {emoji} title • {emoji} title``. If a
    title's emoji isn't in ``emoji_for`` (LLM lookup failed or hasn't
    happened yet) the renderer applies the keyword-based fallback so every
    event still gets a leading glyph. The fallback emoji is rendered but
    never persisted, so the next refresh can upgrade it to a real LLM
    result.

    Events are sorted by start time; all-day events sort to the front.
    """
    day_label = f"{day.strftime('%A, %B')} {day.day}"
    pieces: list[str] = []
    for ev in sorted(events, key=lambda x: x.get("start", "")):
        title = (ev.get("summary") or "(untitled)").strip() or "(untitled)"
        emoji = (emoji_for.get(title) or "").strip() or _fallback_emoji(title)
        time_str = _format_time(ev.get("start", ""))
        # Build the piece without double-spaces when emoji is missing.
        parts = [p for p in (time_str, emoji, title) if p]
        pieces.append(" ".join(parts))
    return f"<div><strong>{day_label}:</strong> " + " • ".join(pieces) + "</div>"


def _upsert_day(s: Session, email: str, day: date, events_json: str) -> None:
    from app.db import CalendarDaySummary

    now = datetime.now(UTC)
    try:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(CalendarDaySummary).values(
            email=email,
            day=day,
            events_json=events_json,
            generated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[CalendarDaySummary.email, CalendarDaySummary.day],
            set_={
                "events_json": stmt.excluded.events_json,
                "generated_at": stmt.excluded.generated_at,
            },
        )
        s.execute(stmt)
    except Exception:
        existing = s.get(CalendarDaySummary, (email, day))
        if existing:
            existing.events_json = events_json
            existing.generated_at = now
        else:
            s.add(
                CalendarDaySummary(
                    email=email,
                    day=day,
                    events_json=events_json,
                    generated_at=now,
                )
            )
    s.commit()


def persist_events_for_days(
    s: Session,
    email: str,
    events_by_day: dict[date, list[dict]],
    *,
    use_batch: bool = False,
) -> None:
    """Refresh-time: persist raw events per day and backfill missing emojis.

    No HTML is rendered or cached here — the page renders the calendar
    inline at view time from the persisted ``events_json`` plus the
    ``event_emojis`` map. The emoji backfill (LLM) runs at refresh time
    because it's the slow part; the per-day render at view time then has
    a guaranteed cache hit on every emoji and is pure Python.

    ``use_batch=True`` uses the Anthropic Batch API for cron runs;
    ``use_batch=False`` uses a single low-latency call for user /refresh.
    """
    titles: list[str] = []
    for events in events_by_day.values():
        for ev in events:
            t = (ev.get("summary") or "").strip()
            if t:
                titles.append(t)
    if titles:
        emojis_for_titles(s, titles, use_batch=use_batch)

    for day in sorted(events_by_day):
        events = events_by_day[day]
        events_json = json.dumps(events, ensure_ascii=False, default=str)
        _upsert_day(s, email, day, events_json)


def read_emoji_map(s: Session, titles: list[str]) -> dict[str, str]:
    """View-time emoji lookup: pure DB read of ``event_emojis`` — no LLM.

    Titles missing from the cache are simply absent from the result;
    ``render_day_html`` handles a missing emoji by applying the
    keyword-based fallback (and dropping the prefix entirely if even that
    fails to match). Missing entries get backfilled by the next refresh
    via :func:`persist_events_for_days`."""
    from app.db import EventEmoji

    distinct = list({(t or "").strip() for t in titles if (t or "").strip()})
    if not distinct:
        return {}
    norm_for = {t: _normalize_title(t) for t in distinct}
    rows = (
        s.execute(select(EventEmoji).where(EventEmoji.title_norm.in_(list(norm_for.values()))))
        .scalars()
        .all()
    )
    by_norm = {row.title_norm: row.emoji for row in rows}
    return {t: by_norm[norm_for[t]] for t in distinct if norm_for[t] in by_norm}


def build_calendar_section(day_htmls: dict[date, str]) -> str:
    """Assemble per-day HTML snippets into a full <section> for injection."""
    if not day_htmls:
        return ""
    inner = "\n".join(html for _, html in sorted(day_htmls.items()))
    return f"<section>\n<h2>\U0001f4c5 Calendar</h2>\n{inner}\n</section>"


def load_calendar_section(s: Session, email: str, today: date) -> str:
    """View-time: render the calendar section inline from persisted events.

    Reads the raw events for ``[today, today+30d]`` from
    ``calendar_day_summaries.events_json``, looks up emojis from the
    ``event_emojis`` table (no LLM call), runs :func:`render_day_html`
    for each day, and assembles the section. Re-rendering on every view
    means renderer changes take effect immediately, no cache-bust needed.
    """
    from app.db import CalendarDaySummary

    horizon = today + timedelta(days=30)
    rows = (
        s.execute(
            select(CalendarDaySummary)
            .where(
                CalendarDaySummary.email == email,
                CalendarDaySummary.day >= today,
                CalendarDaySummary.day <= horizon,
            )
            .order_by(CalendarDaySummary.day)
        )
        .scalars()
        .all()
    )

    events_by_day: dict[date, list[dict]] = {}
    titles: list[str] = []
    for row in rows:
        try:
            events = json.loads(row.events_json) or []
        except (TypeError, ValueError):
            continue
        events_by_day[row.day] = events
        for ev in events:
            t = (ev.get("summary") or "").strip()
            if t:
                titles.append(t)

    emoji_for = read_emoji_map(s, titles)
    day_htmls = {
        day: render_day_html(day, events, emoji_for) for day, events in events_by_day.items()
    }
    return build_calendar_section(day_htmls)


def build_dorchester_event_list(
    events: list[dict],
    cal_names: dict[str, str],
) -> str:
    """Compact bullet list of Dorchester Parent Calendar events for the LLM.

    This is the one calendar feed that *does* get passed into the prompt, so
    Claude can ground the Dorchester Elementary School news section in real
    upcoming events instead of inventing dates. Format::

        - 2026-05-08 (Fri) 9:00 AM: Spring concert
        - 2026-05-15 (Fri) all-day: Field day

    Returns ``"(none)"`` when no events match.
    """
    from app import calendar_oauth

    lines: list[str] = []
    for ev in sorted(events, key=lambda x: x.get("start", "")):
        cn = (cal_names.get(ev.get("calendar_id"), "") or "").lower()
        if "dorchester parent calendar" not in cn:
            continue
        title = (ev.get("summary") or "").strip()
        start = ev.get("start", "")
        if not title or not start:
            continue
        if calendar_oauth.is_filtered(cn, title):
            continue
        day_str = start[:10]
        try:
            d = date.fromisoformat(day_str)
        except ValueError:
            continue
        dow = d.strftime("%a")
        if "T" in start:
            try:
                dt = datetime.fromisoformat(start)
                h = dt.hour % 12 or 12
                t = f"{h}:{dt.strftime('%M')} {'AM' if dt.hour < 12 else 'PM'}"
                lines.append(f"- {day_str} ({dow}) {t}: {title}")
                continue
            except ValueError:
                pass
        lines.append(f"- {day_str} ({dow}) all-day: {title}")
    return "\n".join(lines) if lines else "(none)"


def build_pdf_calendar(events: list[dict], today: date, important_uids: set[str]) -> str:
    """Format the calendar block for the PDF rail — no LLM.

    Always includes today and tomorrow. Then extends out to 7 days for any
    timed event, and out to 30 days for events flagged as important
    (``important_uids``). All-day non-important events outside today/tomorrow
    are filtered out so the block stays focused on actionable items.
    """
    week_horizon = today + timedelta(days=7)
    month_horizon = today + timedelta(days=30)

    day_groups: dict[str, list[dict]] = defaultdict(list)
    for ev in events:
        start = ev.get("start", "")
        day_str = start[:10]
        if not day_str:
            continue
        try:
            d = date.fromisoformat(day_str)
        except ValueError:
            continue
        if d < today or d > month_horizon:
            continue
        is_timed = "T" in start
        is_important = ev.get("ical_uid", "") in important_uids
        # Within the next 7 days: include all timed events + any important.
        # Past 7 days, up to 30: only important events surface.
        if d <= week_horizon:
            if not (is_timed or is_important):
                continue
        else:
            if not is_important:
                continue
        day_groups[day_str].append(ev)

    # Skip days with no events entirely — only render headers we'll fill.
    if not day_groups:
        return ""

    label_style = (
        "font-size:10pt;letter-spacing:.05em;text-transform:uppercase;"
        "border-bottom:0.5pt solid #000;margin:0 0 2pt;padding-bottom:1pt;"
        "font-weight:bold"
    )
    # ``cal-event`` class lets the PDF fit loop find + drop event rows.
    # Font sizes are intentionally NOT set here; the user-stylesheet in
    # app.pdf._make_css scales ``.cal-event`` / ``.cal-date`` against the
    # base_pt that the fit algorithm chooses (10-20pt body, dates +20%).
    row_style = "margin:0 0 1pt"
    row_open = f'<div class="cal-event" style="{row_style}'
    parts: list[str] = [f'<div style="{label_style}">Calendar</div>']

    for day_str in sorted(day_groups):
        d = date.fromisoformat(day_str)
        if d == today:
            label = "Today"
        elif d == today + timedelta(days=1):
            label = "Tomorrow"
        else:
            label = d.strftime("%A")  # full day name, e.g. "Friday"
        # Date label: bigger + bold so each day-group's header stands out
        # from the event rows below it.
        # Font size set externally via .cal-date in app.pdf._make_css so it
        # tracks the fit-algorithm's chosen base_pt.
        sub_label_style = (
            "letter-spacing:.05em;text-transform:uppercase;"
            "margin:4pt 0 1pt;font-weight:bold;color:#000"
        )
        parts.append(
            f'<div class="cal-date" style="{sub_label_style}">'
            f"<strong>{label} — {d.strftime('%b')} {d.day}</strong></div>"
        )
        evs = sorted(day_groups[day_str], key=lambda x: x.get("start", ""))
        for ev in evs:
            start = ev.get("start", "")
            title = ev.get("summary", "(untitled)")
            if "T" in start:
                try:
                    dt = datetime.fromisoformat(start)
                    h = dt.hour % 12 or 12
                    t = f"{h}:{dt.strftime('%M')} {'AM' if dt.hour < 12 else 'PM'}"
                    parts.append(f'{row_open}">{t} {title}</div>')
                except ValueError:
                    parts.append(f'{row_open}">{title}</div>')
            else:
                parts.append(f'{row_open};color:#555">• {title}</div>')

    return "\n".join(parts)
