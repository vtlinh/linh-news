"""Persisted single-user preferences.

Single-family app, single admin — one row per setting in ``kv_cache`` is
plenty. Centralized here so callers don't reach into the cache backend.

Currently:

* :func:`get_allowed_ratings` / :func:`set_allowed_ratings` — the MPAA
  ratings the admin selected on ``/admin/movies``. Used as the source of
  truth for both the daily HTML edition's Movies section and the printed
  Linh Times PDF, falling back to the kid-age-derived default when the
  user has not explicitly set them.
"""
from __future__ import annotations

import json
import logging
from datetime import date

from app import cache

log = logging.getLogger(__name__)

_VALID_RATINGS = {"G", "PG", "PG-13", "R", "NC-17"}
_RATINGS_KEY = "linh_news:allowed_ratings"


def get_allowed_ratings(today: date | None = None) -> list[str]:
    """The persisted MPAA-rating selection (set via the admin Movies page).

    Falls back to :func:`app.calendar_oauth.allowed_movie_ratings` so a
    fresh deploy or unset preference behaves the same as before."""
    raw = cache._get_backend().get(_RATINGS_KEY)  # noqa: SLF001
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Corrupt allowed_ratings pref in kv_cache; ignoring.")
            data = None
        if isinstance(data, list):
            cleaned = [r for r in data if r in _VALID_RATINGS]
            if cleaned:
                return cleaned
    from app.calendar_oauth import allowed_movie_ratings
    return allowed_movie_ratings(today)


def set_allowed_ratings(ratings: list[str]) -> list[str]:
    """Persist the admin's MPAA-rating selection. Returns the cleaned list
    actually stored (unknown values dropped)."""
    cleaned = [r for r in ratings if r in _VALID_RATINGS]
    cache._get_backend().set(_RATINGS_KEY, json.dumps(cleaned))  # noqa: SLF001
    return cleaned
