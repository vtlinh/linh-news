"""Per-day calendar summary generation and caching.

Each calendar day with events gets its own HTML snippet, generated once by
Claude (no web_search — pure formatting of structured data) and stored in
``calendar_day_summaries``. A sha-256 fingerprint of the day's events
determines whether regeneration is needed, so generation only runs when events
actually change (new, deleted, or edited).

The HTML page injects summaries at serve time (<!-- CALENDAR_PLACEHOLDER -->).
The PDF gets a pre-formatted plain-Python block (no LLM).
"""
from __future__ import annotations

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

# Bumped whenever the per-day prompt format changes — folded into the
# fingerprint so old DB rows are auto-invalidated and regenerated.
_PROMPT_VERSION = "v3-no-bullet"

_DAY_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "html": {
            "type": "string",
            "description": (
                "Compact one-line HTML for a single calendar day — a <div> "
                "containing the bold date and all events on one line, "
                "separated by ' · '. No <section> wrapper, no <br>."
            ),
        }
    },
    "required": ["html"],
    "additionalProperties": False,
}


def _fingerprint(events: list[dict]) -> str:
    """sha-256 of the fields that affect rendering, order-independent.

    Includes _PROMPT_VERSION so a prompt change auto-invalidates every cached
    summary on the next run.
    """
    stable = sorted(
        [
            {
                "ical_uid": e.get("ical_uid", ""),
                "summary": e.get("summary", ""),
                "start": e.get("start", ""),
                "end": e.get("end", ""),
                "description": e.get("description", ""),
                "location": e.get("location", ""),
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


_DAY_SYSTEM_PROMPT = (
    "You format one calendar day's events as a compact, single-line HTML snippet "
    "for a daily newspaper.\n"
    "OUTPUT FORMAT — produce exactly this shape:\n"
    "  <div><strong>Friday, May 2:</strong> 9:00 AM 📚 Library visit · "
    "12:00 PM 🍕 Lunch with team · 🎂 Dad's birthday</div>\n"
    "RULES:\n"
    "- All events for the day go on ONE line inside a single <div>. "
    "Never use <br>, <ul>, <li>, or any vertical separator.\n"
    "- Separate events with ' · ' (space, middle dot, space).\n"
    "- Bold the date with <strong>…</strong> and follow it with a colon.\n"
    "- Timed events: show the time in 12-hour format ('9:00 AM') BEFORE the emoji.\n"
    "- All-day events: NO time and NO bullet — just emoji + title.\n"
    "- DO NOT use the '•' bullet character anywhere — the emoji is the only "
    "visual marker for each event.\n"
    "- Add ONE contextually relevant emoji to each event, placed between the "
    "time (if any) and the event title — e.g. '9:00 AM 📚 Library visit', "
    "'🎂 Dad's birthday', '🎉 International Labor Day'. Pick the emoji from "
    "the event title — birthday → 🎂, soccer → ⚽, dance → 💃, school → 🏫, "
    "dentist/doctor → 🦷/🩺, dinner → 🍽, flight/travel → ✈️, holiday → 🎉, "
    "water/utility → 💧, etc. Use 📅 only as a last resort.\n"
    "- HTML ONLY. NEVER use markdown syntax: do NOT write '**bold**', '*emphasis*', "
    "'# heading', '- list', or backticks. Use <strong> tags instead of asterisks.\n"
    "- No source links, no citations, no external URLs, no inline styles.\n"
    "- Do NOT mention William or Elizabeth by name unless their name appears "
    "verbatim in the event title.\n"
    "- Keep it tight — the whole day fits on one line of newsprint."
)
_DAY_MODEL = "claude-haiku-4-5-20251001"
_DAY_MAX_TOKENS = 800


def _day_user_prompt(day: date, events: list[dict]) -> str:
    day_label = f"{day.strftime('%A, %B')} {day.day}, {day.year}"
    return (
        f"Day: {day_label}\n\n"
        f"Events:\n{json.dumps(events, indent=2, ensure_ascii=False, default=str)}\n\n"
        "Produce the HTML snippet."
    )


_BAD_TAG_RE = re.compile(r"<\s*/?\s*(ul|ol|li|br|p\b)", re.IGNORECASE)


def _looks_single_line(html: str) -> bool:
    """Reject Claude outputs that fall back to bulleted lists / vertical
    layouts despite the prompt. The single-line format we want is a single
    <div> with <strong> + ' · '-separated events — no list/break tags."""
    if not html or not html.strip():
        return False
    return _BAD_TAG_RE.search(html) is None


def _generate_day_html(day: date, events: list[dict]) -> str:
    """Ask Claude (Haiku — no web_search) to format one day's events as HTML.

    Used as a fallback when the batch path can't run a single day (rare)."""
    result = claude_client.call_with_schema(
        system=_DAY_SYSTEM_PROMPT,
        user=_day_user_prompt(day, events),
        schema=_DAY_SUMMARY_SCHEMA,
        schema_name="return_day_summary",
        schema_description="HTML snippet for one calendar day.",
        max_tokens=_DAY_MAX_TOKENS,
        model=_DAY_MODEL,
    )
    html = result["html"]
    if not _looks_single_line(html):
        log.warning("Calendar day %s: Claude returned non-single-line HTML "
                    "(%r); falling back to plain formatter.", day, html[:200])
        return _plain_fallback(day, events)
    return html


def _batch_generate_days(
    items: list[tuple[date, list[dict], str]],
) -> dict[date, str]:
    """Submit one Anthropic Batch request with one entry per day.

    Polls until the batch ends, then returns ``{day: html}``. Days whose entry
    failed inside the batch are simply absent from the result — the caller
    falls back to ``_plain_fallback`` for those.
    """
    from app.claude_client import _client

    client = _client()

    requests = []
    for day, events, _fp in items:
        requests.append({
            "custom_id": day.isoformat(),
            "params": {
                "model": _DAY_MODEL,
                "max_tokens": _DAY_MAX_TOKENS,
                "system": _DAY_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": _day_user_prompt(day, events)}],
                "tools": [{
                    "name": "return_day_summary",
                    "description": "HTML snippet for one calendar day.",
                    "input_schema": _DAY_SUMMARY_SCHEMA,
                }],
                "tool_choice": {"type": "tool", "name": "return_day_summary"},
            },
        })

    batch = client.messages.batches.create(requests=requests)
    log.info("Calendar batch %s submitted (%d days), polling…", batch.id, len(items))

    deadline = time.monotonic() + 600  # 10 min cap
    while True:
        if time.monotonic() > deadline:
            log.warning("Calendar batch %s exceeded 10-min cap; cancelling", batch.id)
            try:
                client.messages.batches.cancel(batch.id)
            except Exception:  # noqa: BLE001
                pass
            raise TimeoutError(f"calendar batch {batch.id} timed out")
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        log.info("Calendar batch %s status=%s — sleeping 5s",
                 batch.id, batch.processing_status)
        time.sleep(5)

    log.info("Calendar batch %s ended, reading results", batch.id)

    out: dict[date, str] = {}
    for entry in client.messages.batches.results(batch.id):
        try:
            day = date.fromisoformat(entry.custom_id)
        except ValueError:
            continue
        result_type = getattr(entry.result, "type", None)
        if result_type != "succeeded":
            log.warning("Batch entry %s: %s", entry.custom_id, result_type)
            continue
        msg = entry.result.message
        for block in msg.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "return_day_summary":
                html = (block.input or {}).get("html", "").strip()
                if not html:
                    break
                if not _looks_single_line(html):
                    log.warning(
                        "Batch entry %s: Claude returned non-single-line HTML "
                        "(%r); will fall back to plain formatter.",
                        entry.custom_id, html[:200],
                    )
                    break  # leave `day` absent → caller falls back
                out[day] = html
                break

    log.info("Calendar batch %s: %d/%d days produced single-line HTML",
             batch.id, len(out), len(items))
    return out


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
    t = (title or "").lower()
    for keywords, emoji in _FALLBACK_EMOJI_RULES:
        if any(kw in t for kw in keywords):
            return emoji
    return "📅"


def _plain_fallback(day: date, events: list[dict]) -> str:
    """Pure-Python fallback (one-line, matches the LLM output format)."""
    day_label = f"{day.strftime('%A, %B')} {day.day}"
    pieces: list[str] = []
    for e in sorted(events, key=lambda x: x.get("start", "")):
        start = e.get("start", "")
        title = e.get("summary", "(untitled)")
        emoji = _fallback_emoji(title)
        if "T" in start:
            try:
                dt = datetime.fromisoformat(start)
                h = dt.hour % 12 or 12
                t = f"{h}:{dt.strftime('%M')} {'AM' if dt.hour < 12 else 'PM'}"
                pieces.append(f"{t} {emoji} {title}")
            except ValueError:
                pieces.append(f"{emoji} {title}")
        else:
            pieces.append(f"{emoji} {title}")
    return f"<div><strong>{day_label}:</strong> " + " · ".join(pieces) + "</div>"


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
    """Return {day: html} for all days with events.

    Cache hits return immediately. Misses are submitted as a single Anthropic
    Batch API request — Haiku, no web_search, no streaming, all parallel
    server-side at 50% the per-request cost. On batch failure or timeout each
    missing day falls back to a plain-Python listing.
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
            log.info("Calendar day %s: %s — queueing for batch",
                     day, "stale" if row else "new")
            to_generate.append((day, events, fp))

    if not to_generate:
        return results

    batched_html: dict[date, str] = {}
    try:
        batched_html = _batch_generate_days(to_generate)
    except Exception:
        log.exception("Calendar batch failed; will use plain fallbacks")

    for day, events, fp in to_generate:
        html = batched_html.get(day) or _plain_fallback(day, events)
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
    """Read cached summaries for today..today+30d and assemble a section.

    Called at serve time by the route layer — no LLM involved here.
    """
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
    only if their ical_uid is in important_uids or they match the auto-important
    rule (the caller is responsible for populating important_uids accordingly).
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
