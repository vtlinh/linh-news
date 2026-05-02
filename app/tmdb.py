"""TMDB API helpers used to populate the ``movies`` table.

We use TMDB (themoviedb.org) — a free, community-maintained movie database —
as the source of truth for the kid-appropriate movie watchlist. TMDB exposes
release dates, US MPAA certifications, plot summaries, YouTube trailer keys,
and poster paths through a simple JSON API. Set ``TMDB_API_KEY`` in the
environment.

Public helpers:

* :func:`now_playing_and_upcoming` — paginated ``/movie/now_playing`` +
  ``/movie/upcoming`` (US region). These are TMDB's curated US theatrical
  release feeds — mainstream entries only, every one with US certification
  data. Results merged and de-duped by id.
* :func:`discover_us_theatrical` — paginated ``/discover/movie`` restricted
  to US wide-theatrical releases with US certification data and a minimum
  vote count, for movies whose primary release falls in a date window.
  Catches mainstream titles further out than the curated feeds reach
  (those typically only span ~3 months).
* :func:`fetch_movie_detail` — ``/movie/{id}?append_to_response=videos,
  release_dates`` for one movie, returning the fields we store.
"""
from __future__ import annotations

import logging
from datetime import date

import httpx

from app.settings import get_settings

log = logging.getLogger(__name__)

_BASE = "https://api.themoviedb.org/3"
_IMG_BASE = "https://image.tmdb.org/t/p/w500"
_YT_WATCH = "https://www.youtube.com/watch?v="

# US theatrical release types per TMDB:
#   1 = Premiere, 2 = Theatrical (limited), 3 = Theatrical, 4 = Digital,
#   5 = Physical, 6 = TV.
_THEATRICAL_TYPES = {2, 3}


def _client() -> httpx.Client:
    return httpx.Client(timeout=15.0, limits=httpx.Limits(max_connections=10))


def now_playing_and_upcoming(*, max_pages: int = 5) -> list[dict]:
    """Return merged ``/movie/now_playing`` + ``/movie/upcoming`` results
    (US region), de-duped by id. These are TMDB's curated US theatrical
    feeds — mainstream Hollywood + studio releases only, every entry with
    US certification data. Returns ``[]`` if ``TMDB_API_KEY`` is unset."""
    key = get_settings().tmdb_api_key
    if not key:
        log.warning("TMDB_API_KEY not set — TMDB feeds return empty.")
        return []
    seen: set[int] = set()
    out: list[dict] = []
    with _client() as c:
        for endpoint in ("now_playing", "upcoming"):
            for page in range(1, max_pages + 1):
                r = c.get(
                    f"{_BASE}/movie/{endpoint}",
                    params={
                        "api_key": key,
                        "region": "US",
                        "page": str(page),
                    },
                )
                if r.status_code != 200:
                    log.warning("TMDB %s page %s -> %s", endpoint, page, r.status_code)
                    break
                body = r.json()
                for m in body.get("results") or []:
                    mid = m.get("id")
                    if mid is None or mid in seen:
                        continue
                    seen.add(int(mid))
                    out.append(m)
                if page >= int(body.get("total_pages") or 0):
                    break
    return out


def discover_us_theatrical(
    *,
    earliest: date,
    latest: date,
    max_pages: int = 10,
    min_votes: int = 20,
) -> list[dict]:
    """Paginate ``/discover/movie`` restricted to US wide-theatrical releases
    with US certifications and a minimum vote count. ``primary_release_date``
    is constrained to ``[earliest, latest]``.

    The vote-count and certification filters drop the long tail of festival
    shorts, foreign indies, and self-published entries that ``with_release_type``
    alone doesn't filter out — the certification filter is *not* a rating
    filter (it allows every cert from G to NC-17), it just requires that
    the movie has a US cert, which correlates almost perfectly with "real
    US wide release".
    """
    key = get_settings().tmdb_api_key
    if not key:
        log.warning("TMDB_API_KEY not set — discover returns empty.")
        return []
    out: list[dict] = []
    seen: set[int] = set()
    with _client() as c:
        for page in range(1, max_pages + 1):
            r = c.get(
                f"{_BASE}/discover/movie",
                params={
                    "api_key": key,
                    "region": "US",
                    "with_release_type": "3",
                    "certification_country": "US",
                    "certification.lte": "NC-17",
                    "vote_count.gte": str(min_votes),
                    "primary_release_date.gte": earliest.isoformat(),
                    "primary_release_date.lte": latest.isoformat(),
                    "sort_by": "primary_release_date.asc",
                    "include_adult": "false",
                    "page": str(page),
                },
            )
            if r.status_code != 200:
                log.warning("TMDB discover page %s -> %s", page, r.status_code)
                break
            body = r.json()
            for m in body.get("results") or []:
                mid = m.get("id")
                if mid is None or mid in seen:
                    continue
                seen.add(int(mid))
                out.append(m)
            if page >= int(body.get("total_pages") or 0):
                break
    return out


def fetch_movie_detail(tmdb_id: int, client: httpx.Client | None = None) -> dict | None:
    """Return a normalized dict for one movie:

    ``{tmdb_id, title, summary, rating, release_date, status, trailers,
    poster_url}``

    ``release_date`` is the earliest US theatrical date if present; otherwise
    falls back to the global ``release_date`` field. Returns ``None`` on any
    HTTP error."""
    key = get_settings().tmdb_api_key
    if not key:
        return None
    own_client = client is None
    c = client or _client()
    try:
        r = c.get(
            f"{_BASE}/movie/{tmdb_id}",
            params={
                "api_key": key,
                "append_to_response": "videos,release_dates",
            },
        )
        if r.status_code != 200:
            log.warning("TMDB detail %s -> %s", tmdb_id, r.status_code)
            return None
        body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("TMDB detail %s failed: %s", tmdb_id, e)
        return None
    finally:
        if own_client:
            c.close()

    return _normalize_detail(body)


def _normalize_detail(body: dict) -> dict:
    title = (body.get("title") or "").strip()
    overview = (body.get("overview") or "").strip()
    poster_path = body.get("poster_path") or ""
    poster_url = (_IMG_BASE + poster_path) if poster_path else None

    us_rating, us_theatrical_date = _us_release_info(body.get("release_dates") or {})
    fallback_date = _parse_iso(body.get("release_date") or "")
    release_date = us_theatrical_date or fallback_date

    trailers = _youtube_trailers(body.get("videos") or {})

    return {
        "tmdb_id": int(body.get("id")),
        "title": title,
        "summary": overview,
        "rating": us_rating,
        "release_date": release_date,
        "trailers": trailers,
        "poster_url": poster_url,
    }


def _us_release_info(release_dates_block: dict) -> tuple[str | None, date | None]:
    """Pick the earliest US theatrical date and first non-empty US cert."""
    results = release_dates_block.get("results") or []
    us = next((r for r in results if r.get("iso_3166_1") == "US"), None)
    if not us:
        return None, None
    entries = us.get("release_dates") or []
    cert: str | None = None
    earliest: date | None = None
    for entry in entries:
        if cert is None:
            c = (entry.get("certification") or "").strip()
            if c:
                cert = c
        rt = entry.get("type")
        if rt in _THEATRICAL_TYPES:
            d = _parse_iso((entry.get("release_date") or "")[:10])
            if d and (earliest is None or d < earliest):
                earliest = d
    return cert, earliest


def _youtube_trailers(videos_block: dict) -> list[str]:
    results = videos_block.get("results") or []
    youtube = [v for v in results if v.get("site") == "YouTube"]
    # Trailer first, then Teaser; preserve TMDB's ordering within each group.
    trailers = [v for v in youtube if v.get("type") == "Trailer"]
    teasers = [v for v in youtube if v.get("type") == "Teaser"]
    seen: set[str] = set()
    urls: list[str] = []
    for v in trailers + teasers:
        key = (v.get("key") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        urls.append(_YT_WATCH + key)
        if len(urls) >= 3:
            break
    return urls


def _parse_iso(s: str) -> date | None:
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None
