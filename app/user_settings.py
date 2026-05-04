"""Per-user newspaper configuration: load, save, seed, normalize, geocode.

The Data tab (``/data``) reads/writes the ``user_settings`` table through
this module. The generation pipeline reads from here too so each user gets
their own section list, masthead name, and weather coordinates.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.db import UserSettings
from app.settings import get_settings

log = logging.getLogger(__name__)

DEFAULT_SUBSECTION_COUNT = 5

_EMPTY_SECTION = {
    "key": "",
    "title": "",
    "description": "",
    "subsection_count": DEFAULT_SUBSECTION_COUNT,
    "preferred_sources": [],
    "use_global_sources": True,
}


def to_dict(row: UserSettings) -> dict:
    return {
        "email": row.email,
        "display_name": row.display_name,
        "address": row.address,
        "weather_coords": row.weather_coords,
        "sections": list(row.sections_json or []),
        "children": list(row.children_json or []),
    }


def get(s: Session, email: str) -> dict:
    """Return saved settings for ``email``, or a blank starter dict if the
    row doesn't exist. Read-only — never writes."""
    row = s.get(UserSettings, email)
    if row is not None:
        return to_dict(row)
    return {
        "email": email,
        "display_name": None,
        "address": None,
        "weather_coords": None,
        "sections": [],
        "children": [],
    }


# ── Validation ───────────────────────────────────────────────────────────


def validate(payload: dict) -> dict[str, str]:
    """Return ``{}`` on success or a map of ``error_key -> message``.

    Per-section rule: if ``use_global_sources == false`` *and* the section
    has no ``preferred_sources``, save is blocked with the row flagged so
    the UI can highlight it.
    """
    errors: dict[str, str] = {}
    sections = payload.get("sections") or []
    if not isinstance(sections, list) or not sections:
        errors["sections"] = "At least one section is required."
        return errors
    for idx, sec in enumerate(sections):
        title = (sec.get("title") or "").strip()
        if not title:
            errors[f"section.{idx}.title"] = "Title cannot be empty."
        try:
            n = int(sec.get("subsection_count", DEFAULT_SUBSECTION_COUNT))
        except (TypeError, ValueError):
            n = -1
        if n < 1 or n > 30:
            errors[f"section.{idx}.subsection_count"] = "Must be 1–30."
        sources = [str(u).strip() for u in (sec.get("preferred_sources") or []) if str(u).strip()]
        use_global = bool(sec.get("use_global_sources", True))
        if not use_global and not sources:
            errors[f"section.{idx}.sources"] = (
                "Add at least one preferred source, or check 'use global sources'."
            )
    from datetime import date as _date

    for cidx, child in enumerate(payload.get("children") or []):
        name = (child.get("name") or "").strip()
        bday = (child.get("birthday") or "").strip()
        if not name and not bday:
            continue
        if not name:
            errors[f"child.{cidx}.name"] = "Name is required."
        if bday:
            try:
                _date.fromisoformat(bday)
            except ValueError:
                errors[f"child.{cidx}.birthday"] = "Birthday must be YYYY-MM-DD."
        else:
            errors[f"child.{cidx}.birthday"] = "Birthday is required."
    return errors


# ── Geocoding (Nominatim) ────────────────────────────────────────────────


def geocode(address: str) -> str | None:
    """Resolve a free-form address to ``"lat,lon"`` via OSM Nominatim.
    Returns ``None`` on failure — caller keeps the prior coords.
    """
    address = (address or "").strip()
    if not address:
        return None
    try:
        r = httpx.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": address, "format": "json", "limit": 1},
            headers={"User-Agent": "linh-news/1.0 (per-user weather)"},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        lat = float(data[0]["lat"])
        lon = float(data[0]["lon"])
        return f"{lat:.4f},{lon:.4f}"
    except Exception as e:  # noqa: BLE001 — geocoding is best-effort
        log.warning("Nominatim geocode failed for %r: %s", address, e)
        return None


# ── LLM normalization (best-effort) ──────────────────────────────────────

_NORMALIZE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "descriptions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "idx": {"type": "integer"},
                    "description": {"type": "string"},
                },
                "required": ["idx", "description"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["descriptions"],
    "additionalProperties": False,
}


def normalize_with_llm(sections: list[dict]) -> list[dict]:
    """Fill in missing descriptions for sections whose ``description`` is
    empty. Sections with a non-empty description are returned untouched —
    titles, keys, sources, counts, flags, and the order are all preserved.
    On any failure (no API key, network error, mismatched response),
    returns the input unchanged.
    """
    if not sections:
        return sections
    if not get_settings().anthropic_api_key:
        return sections

    targets = [
        {"idx": i, "title": s.get("title", "")}
        for i, s in enumerate(sections)
        if not (s.get("description") or "").strip()
    ]
    if not targets:
        return sections  # nothing to fill — never call the LLM

    system = (
        "You write one-sentence descriptions for newspaper sections. For "
        "each section in the input array, return a single concise sentence "
        "describing what news belongs in that section, suitable for guiding "
        "an LLM that fills in stories. Do NOT change titles. Return one "
        "entry per input idx, in the same order."
    )
    user = (
        "Sections needing a description (JSON):\n"
        + json.dumps(targets, ensure_ascii=False)
        + "\n\nCall return_normalized_sections with one description per idx."
    )
    try:
        from app import claude_client

        out = claude_client.call_with_schema(
            system=system,
            user=user,
            schema=_NORMALIZE_SCHEMA,
            schema_name="return_normalized_sections",
            schema_description="Return one description per requested section idx.",
            max_tokens=2000,
            model="claude-haiku-4-5-20251001",
        )
    except Exception as e:  # noqa: BLE001 — best-effort
        log.warning("Section normalization LLM call failed: %s", e)
        return sections

    cleaned = out.get("descriptions") or []
    if len(cleaned) != len(targets):
        log.warning(
            "LLM returned %d descriptions, expected %d — keeping originals.",
            len(cleaned),
            len(targets),
        )
        return sections

    by_idx = {int(c.get("idx", -1)): (c.get("description") or "").strip() for c in cleaned}
    merged = []
    for i, orig in enumerate(sections):
        m = dict(orig)
        # Only overwrite when the original was empty AND we got something back.
        if not (m.get("description") or "").strip():
            new_desc = by_idx.get(i)
            if new_desc:
                m["description"] = new_desc
        merged.append(m)
    return merged


# ── Save ─────────────────────────────────────────────────────────────────


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "section"


def save(s: Session, email: str, payload: dict) -> dict:
    """Persist the user's settings. Caller has already run :func:`validate`
    and confirmed no errors. Geocodes the address if it changed; runs the
    LLM normalization pass before writing. Returns the saved dict."""
    row = s.get(UserSettings, email)
    prior_address = row.address if row else None
    prior_coords = row.weather_coords if row else None

    sections_in = list(payload.get("sections") or [])
    # Ensure every section has a stable key.
    used_keys: set[str] = set()
    for sec in sections_in:
        k = (sec.get("key") or "").strip() or _slugify(sec.get("title", ""))
        base = k
        i = 2
        while k in used_keys:
            k = f"{base}-{i}"
            i += 1
        used_keys.add(k)
        sec["key"] = k
        sec["title"] = (sec.get("title") or "").strip()
        sec["description"] = (sec.get("description") or "").strip()
        try:
            sec["subsection_count"] = int(sec.get("subsection_count", DEFAULT_SUBSECTION_COUNT))
        except (TypeError, ValueError):
            sec["subsection_count"] = DEFAULT_SUBSECTION_COUNT
        sec["preferred_sources"] = [
            str(u).strip() for u in (sec.get("preferred_sources") or []) if str(u).strip()
        ]
        sec["use_global_sources"] = bool(sec.get("use_global_sources", True))

    sections_clean = normalize_with_llm(sections_in)

    children_clean: list[dict] = []
    for c in payload.get("children") or []:
        name = (c.get("name") or "").strip()
        bday = (c.get("birthday") or "").strip()
        if not name or not bday:
            continue
        entry: dict = {"name": name, "birthday": bday}
        sc = c.get("school_calendar") or {}
        kind = (sc.get("kind") or "").strip()
        value = (sc.get("value") or "").strip()
        if kind in {"google", "url", "ics"} and value:
            entry["school_calendar"] = {"kind": kind, "value": value}
        children_clean.append(entry)

    address = (payload.get("address") or "").strip() or None
    if address and address != prior_address:
        coords = geocode(address) or prior_coords
    else:
        coords = prior_coords

    display_name = (payload.get("display_name") or "").strip() or None

    if row is None:
        row = UserSettings(
            email=email,
            display_name=display_name,
            address=address,
            weather_coords=coords,
            sections_json=sections_clean,
            children_json=children_clean,
            updated_at=datetime.now(UTC),
        )
        s.add(row)
    else:
        row.display_name = display_name
        row.address = address
        row.weather_coords = coords
        row.sections_json = sections_clean
        row.children_json = children_clean
        row.updated_at = datetime.now(UTC)
    s.commit()
    return to_dict(row)
