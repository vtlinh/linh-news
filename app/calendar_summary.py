"""Per-day calendar summary rendering with a DB-cached emoji map.

The summary HTML for each day is produced in pure Python — no LLM call —
in the format::

    <div><strong>Friday, May 2:</strong> 9:00 AM 📚 Library visit
        • 12:00 PM 🍕 Lunch with team • 🎂 Dad's birthday</div>

Events are joined with ' • ' (bullet). Each event renders as
``time {emoji} title`` (timed) or ``{emoji} title`` (all-day).

Per-event emojis come from the ``event_emojis`` table, which maps a
normalized event title to a single emoji. When a title has no row, we
ask Claude (Haiku, no web_search) for an emoji once, then persist the
result so future runs are LLM-free.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import claude_client

log = logging.getLogger(__name__)

# Bumped when the per-day output format changes — folded into the
# fingerprint so cached rows are auto-invalidated and re-rendered.
_PROMPT_VERSION = "v4-python-bullet"

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
    (("holiday", "labor day", "memorial day", "thanksgiving", "christmas",
      "new year", "easter"), "🎉"),
    (("water",), "💧"),
    (("delivery",), "📦"),
    (("photo",), "📷"),
    (("library",), "📚"),
]


def _fallback_emoji(title: str) -> str:
    """Last-resort emoji when the LLM is unavailable."""
    t = (title or "").lower()
    for keywords, emoji in _FALLBACK_EMOJI_RULES:
        if any(kw in t for kw in keywords):
            return emoji
    return "📅"


def _normalize_title(title: str) -> str:
    """Collapse whitespace + lowercase so trivial variants share one emoji."""
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def _fingerprint(events: list[dict]) -> str:
    """sha-256 of fields that affect rendering — order-independent."""
    stable = sorted(
        [
            {
                "ical_uid": e.get("ical_uid", ""),
                "summary": e.get("summary", ""),
                "start": e.get("start", ""),
                "end": e.get("end", ""),
                "calendar_name": e.get("calendar_name", ""),
            }
            for e in events
        ],
        key=lambda x: (x["start"], x["summary"], x["ical_uid"]),
    )
    payload = json.dumps(
        {"prompt": _PROMPT_VERSION, "events": stable},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


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


def _llm_emoji_lookup(titles: list[str]) -> dict[str, str]:
    """Ask Claude (Haiku, no web_search) for one emoji per title.

    Returns ``{original_title: emoji}``. Titles whose response was empty are
    omitted — the caller is expected to fall back to ``_fallback_emoji``.
    """
    if not titles:
        return {}
    user = (
        "Return one representative emoji for each of these calendar event "
        "titles. Output via the `return_emojis` tool with one entry per "
        "title, in the same order:\n\n"
        + "\n".join(f"- {t}" for t in titles)
    )
    out = claude_client.call_with_schema(
        system=_EMOJI_SYSTEM,
        user=user,
        schema=_EMOJI_SCHEMA,
        schema_name="return_emojis",
        schema_description="Map of calendar event titles to a single emoji.",
        max_tokens=1500,
        model=_EMOJI_MODEL,
    )
    mapping: dict[str, str] = {}
    by_norm: dict[str, str] = {_normalize_title(t): t for t in titles}
    for entry in out.get("emojis", []):
        title = (entry.get("title") or "").strip()
        emoji = (entry.get("emoji") or "").strip()
        if not title or not emoji:
            continue
        # Match by normalized title so trivial differences (case / whitespace)
        # in the model's echoed title don't break the lookup.
        original = by_norm.get(_normalize_title(title))
        if original is not None:
            mapping[original] = emoji
    return mapping


def emojis_for_titles(s: Session, titles: list[str]) -> dict[str, str]:
    """Return ``{title: emoji}`` for every distinct title.

    DB cache first; one LLM call covers any titles missing from the table,
    and results are persisted so future runs skip the LLM entirely. On LLM
    failure we use a small built-in keyword fallback rather than blocking
    edition generation.
    """
    from app.db import EventEmoji

    distinct = list({(t or "").strip() for t in titles if (t or "").strip()})
    if not distinct:
        return {}

    norm_for = {t: _normalize_title(t) for t in distinct}
    norms = list({norm_for[t] for t in distinct})

    rows = s.execute(
        select(EventEmoji).where(EventEmoji.title_norm.in_(norms))
    ).scalars().all()
    cached: dict[str, str] = {row.title_norm: row.emoji for row in rows}

    missing = [t for t in distinct if norm_for[t] not in cached]
    if missing:
        try:
            llm_map = _llm_emoji_lookup(missing)
        except Exception:  # noqa: BLE001 — never fail edition generation on emoji lookup
            log.exception("Emoji LLM lookup failed; using fallback for %d titles",
                          len(missing))
            llm_map = {}
        now = datetime.now(UTC)
        for t in missing:
            tn = norm_for[t]
            emoji = (llm_map.get(t) or "").strip() or _fallback_emoji(t)
            cached[tn] = emoji
            existing = s.get(EventEmoji, tn)
            if existing:
                existing.emoji = emoji
                existing.generated_at = now
            else:
                s.add(EventEmoji(
                    title_norm=tn, emoji=emoji, generated_at=now,
                ))
        s.commit()

    return {t: cached[norm_for[t]] for t in distinct}


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

    Format: ``Day, Month D: time {emoji} title • {emoji} title``.
    Events are sorted by start time; all-day events sort to the front.
    """
    day_label = f"{day.strftime('%A, %B')} {day.day}"
    pieces: list[str] = []
    for ev in sorted(events, key=lambda x: x.get("start", "")):
        title = (ev.get("summary") or "(untitled)").strip() or "(untitled)"
        emoji = emoji_for.get(title) or _fallback_emoji(title)
        time_str = _format_time(ev.get("start", ""))
        if time_str:
            pieces.append(f"{time_str} {emoji} {title}")
        else:
            pieces.append(f"{emoji} {title}")
    return f"<div><strong>{day_label}:</strong> " + " • ".join(pieces) + "</div>"


def _upsert_day(s: Session, day: date, html: str, fp: str, events_json: str) -> None:
    from app.db import CalendarDaySummary

    now = datetime.now(UTC)
    try:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(CalendarDaySummary).values(
            day=day,
            summary_html=html,
            event_fingerprint=fp,
            events_json=events_json,
            generated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[CalendarDaySummary.day],
            set_={
                "summary_html": stmt.excluded.summary_html,
                "event_fingerprint": stmt.excluded.event_fingerprint,
                "events_json": stmt.excluded.events_json,
                "generated_at": stmt.excluded.generated_at,
            },
        )
        s.execute(stmt)
    except Exception:
        existing = s.get(CalendarDaySummary, day)
        if existing:
            existing.summary_html = html
            existing.event_fingerprint = fp
            existing.events_json = events_json
            existing.generated_at = now
        else:
            s.add(
                CalendarDaySummary(
                    day=day,
                    summary_html=html,
                    event_fingerprint=fp,
                    events_json=events_json,
                    generated_at=now,
                )
            )
    s.commit()


def get_or_generate_summaries(
    s: Session, events_by_day: dict[date, list[dict]]
) -> dict[date, str]:
    """Return ``{day: html}`` for all days with events.

    Cache hits return immediately. Misses are rendered in pure Python using a
    DB-backed emoji map; the only LLM call needed is a single Haiku request
    that backfills any titles whose emoji we haven't seen before.
    """
    from app.db import CalendarDaySummary

    results: dict[date, str] = {}
    to_generate: list[tuple[date, list[dict], str]] = []

    for day in sorted(events_by_day):
        events = events_by_day[day]
        fp = _fingerprint(events)
        row = s.get(CalendarDaySummary, day)
        if row is not None and row.event_fingerprint == fp:
            log.debug("Calendar day %s: cache hit", day)
            results[day] = row.summary_html
        else:
            log.info("Calendar day %s: %s — re-rendering",
                     day, "stale" if row else "new")
            to_generate.append((day, events, fp))

    if not to_generate:
        return results

    # One pass to look up every distinct title across the days we need to
    # render — backfills emojis with a single LLM call (if any are missing).
    titles: list[str] = []
    for _, events, _ in to_generate:
        for ev in events:
            t = (ev.get("summary") or "").strip()
            if t:
                titles.append(t)
    emoji_for = emojis_for_titles(s, titles)

    for day, events, fp in to_generate:
        html = render_day_html(day, events, emoji_for)
        events_json = json.dumps(events, ensure_ascii=False, default=str)
        _upsert_day(s, day, html, fp, events_json)
        results[day] = html

    return results


def build_calendar_section(day_htmls: dict[date, str]) -> str:
    """Assemble per-day HTML snippets into a full <section> for injection."""
    if not day_htmls:
        return ""
    inner = "\n".join(html for _, html in sorted(day_htmls.items()))
    return f"<section>\n<h2>\U0001f4c5 Calendar</h2>\n{inner}\n</section>"


def load_calendar_section(s: Session, today: date) -> str:
    """Read cached summaries for today..today+30d and assemble a section."""
    from app.db import CalendarDaySummary

    horizon = today + timedelta(days=30)
    rows = s.execute(
        select(CalendarDaySummary)
        .where(CalendarDaySummary.day >= today, CalendarDaySummary.day <= horizon)
        .order_by(CalendarDaySummary.day)
    ).scalars().all()
    day_htmls = {row.day: row.summary_html for row in rows}
    return build_calendar_section(day_htmls)


def build_pdf_calendar(events: list[dict], today: date, important_uids: set[str]) -> str:
    """Format a compact calendar block for the PDF — no LLM.

    Includes timed events for today and tomorrow. All-day events are included
    only if their ical_uid is in ``important_uids`` or they match the
    auto-important rule (the caller populates ``important_uids`` accordingly).
    """
    tomorrow = today + timedelta(days=1)
    target_days = {today.isoformat(), tomorrow.isoformat()}

    day_groups: dict[str, list[dict]] = defaultdict(list)
    for ev in events:
        start = ev.get("start", "")
        day_str = start[:10]
        if day_str not in target_days:
            continue
        is_timed = "T" in start
        is_important = ev.get("ical_uid", "") in important_uids
        if not is_timed and not is_important:
            continue
        day_groups[day_str].append(ev)

    if not day_groups:
        return ""

    label_style = (
        "font-size:7pt;letter-spacing:.05em;text-transform:uppercase;"
        "border-bottom:0.5pt solid #000;margin:0 0 2pt;padding-bottom:1pt"
    )
    row_style = "margin:0 0 1pt"
    parts: list[str] = []

    for day_str in sorted(day_groups):
        d = date.fromisoformat(day_str)
        label = "Today" if d == today else "Tomorrow"
        parts.append(
            f'<div style="{label_style}">'
            f"{label} — {d.strftime('%b')} {d.day}</div>"
        )
        for ev in sorted(day_groups[day_str], key=lambda x: x.get("start", "")):
            start = ev.get("start", "")
            title = ev.get("summary", "(untitled)")
            if "T" in start:
                try:
                    dt = datetime.fromisoformat(start)
                    h = dt.hour % 12 or 12
                    t = f"{h}:{dt.strftime('%M')} {'AM' if dt.hour < 12 else 'PM'}"
                    parts.append(f'<div style="{row_style}">{t} {title}</div>')
                except ValueError:
                    parts.append(f'<div style="{row_style}">{title}</div>')
            else:
                parts.append(
                    f'<div style="{row_style};color:#555">• {title}</div>'
                )

    return "\n".join(parts)
