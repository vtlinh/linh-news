"""Tiny TMDB helper: given a movie title (and optional year), return the
poster URL hosted on image.tmdb.org.

Why TMDB and not IMDb directly?
  - IMDb has no free, key-less public API. IMDb Pro is a paid product.
  - OMDb (https://www.omdbapi.com) wraps IMDb data with a free API key tier
    but has stricter rate limits and a less reliable image CDN.
  - TMDB is community-maintained, free for personal use, and serves posters
    from image.tmdb.org, which our PDF preflight already accepts.

Set ``TMDB_API_KEY`` in the environment to enable. With no key set, this
module returns ``None`` for every lookup so the page falls back gracefully.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from urllib.parse import quote_plus

import httpx

from app.settings import get_settings

log = logging.getLogger(__name__)

_BASE = "https://api.themoviedb.org/3"
_IMG_BASE = "https://image.tmdb.org/t/p/w500"


@lru_cache(maxsize=512)
def lookup_poster(title: str, year: int | None = None) -> str | None:
    """Return a TMDB poster URL for the given movie title, or None.

    Cached for the process lifetime — TMDB poster URLs are immutable, and
    the admin Movies page can hit this many times per page load."""
    key = get_settings().tmdb_api_key
    if not key:
        return None
    title = (title or "").strip()
    if not title:
        return None

    params: dict[str, str] = {
        "api_key": key,
        "query": title,
        "include_adult": "false",
    }
    if year:
        params["year"] = str(year)
    try:
        with httpx.Client(timeout=8.0) as c:
            r = c.get(f"{_BASE}/search/movie", params=params)
            if r.status_code != 200:
                log.warning("TMDB lookup %s -> %s", title, r.status_code)
                return None
            results = r.json().get("results", [])
    except (httpx.HTTPError, ValueError) as e:
        log.warning("TMDB lookup %s failed: %s", title, e)
        return None

    if not results:
        return None
    # Best match heuristic: highest-popularity result that has a poster.
    results = [m for m in results if m.get("poster_path")]
    if not results:
        return None
    best = max(results, key=lambda m: float(m.get("popularity", 0) or 0))
    return _IMG_BASE + best["poster_path"]


def encode_title(title: str) -> str:
    """Convenience for callers that need a URL-encoded form."""
    return quote_plus(title)
