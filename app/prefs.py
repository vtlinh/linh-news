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
from typing import TYPE_CHECKING

from app import cache

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# "Unrated" is a synthetic value the admin Movies page uses to surface rows
# whose MPAA cert is empty or "NR" (typical for announced pre-release sequels
# whose MPAA cert TMDB hasn't filled in yet). Persisted alongside real ratings
# so the checkbox state survives reloads. It never matches an actual movie's
# rating field, so including it in the daily-edition filter set is a no-op.
_VALID_RATINGS = {"G", "PG", "PG-13", "R", "NC-17", "Unrated"}
# Per-user kv_cache key. Older deploys used a single global key
# ``linh_news:allowed_ratings`` — that lives on as the admin's key for
# backward compatibility (no migration needed; admin's saved value is
# untouched).
_RATINGS_KEY_LEGACY = "linh_news:allowed_ratings"


def _ratings_key(email: str | None) -> str:
    if not email:
        return _RATINGS_KEY_LEGACY
    return f"linh_news:allowed_ratings:{email.lower()}"


def get_allowed_ratings(
    today: date | None = None,
    *,
    session: Session | None = None,
    email: str | None = None,
) -> list[str]:
    """The persisted MPAA-rating selection (set via the admin Movies page).

    Per-user when ``email`` is provided; falls back to
    :func:`app.calendar_oauth.allowed_movie_ratings` so a fresh deploy or
    unset preference behaves the same as before. Pass ``session`` to reuse
    an already-open SQLAlchemy session for the underlying KV read."""
    raw = cache._get_backend().get(_ratings_key(email), session=session)  # noqa: SLF001
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


def set_allowed_ratings(
    ratings: list[str], *, email: str | None = None
) -> list[str]:
    """Persist the user's MPAA-rating selection. Returns the cleaned list
    actually stored (unknown values dropped)."""
    cleaned = [r for r in ratings if r in _VALID_RATINGS]
    cache._get_backend().set(_ratings_key(email), json.dumps(cleaned))  # noqa: SLF001
    return cleaned
