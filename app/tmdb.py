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
* :func:`discover_popular_upcoming` — popularity-sorted ``/discover/movie``
  with no vote/cert filter, capped to a few pages. Catches announced
  mainstream sequels that don't yet have TMDB votes or an MPAA cert because
  they're months pre-release.
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
# Backdrop (landscape, 16:9) sizes available from TMDB are w300, w780, w1280,
# original. We use the smallest (w300) — at the rail's 2.4in width and the
# HTML card's column width, w300 is plenty crisp and keeps both bytes
# downloaded and PDF render cost minimal.
_BACKDROP_BASE = "https://image.tmdb.org/t/p/w300"
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


def discover_popular_upcoming(
    *,
    earliest: date,
    latest: date,
    max_pages: int = 10,
) -> list[dict]:
    """Paginate ``/discover/movie`` sorted by popularity descending — no
    vote_count or certification filter. Catches announced mainstream
    sequels (e.g. The Angry Birds Movie 3) that don't yet have TMDB votes
    or an MPAA cert because they're months pre-release.

    Empirically (May 2026 sampling) the popularity floor across the year
    window: page 3 ≈ pop 8, page 10 ≈ pop 2.1, page 15 ≈ pop 1.6. AB3
    lands at page 9 with popularity 2.48, so 10 pages reliably catches it
    and similar-tier sequels. Beyond ~page 12 the long tail (festival
    shorts, regional releases, foreign indies) dominates because their
    bot-bumped popularity exceeds quiet pre-release sequels — that's the
    cap line."""
    key = get_settings().tmdb_api_key
    if not key:
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
                    "primary_release_date.gte": earliest.isoformat(),
                    "primary_release_date.lte": latest.isoformat(),
                    "sort_by": "popularity.desc",
                    "include_adult": "false",
                    "page": str(page),
                },
            )
            if r.status_code != 200:
                log.warning(
                    "TMDB discover-popular page %s -> %s",
                    page,
                    r.status_code,
                )
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
                "append_to_response": "videos,release_dates,images",
                # Restrict images to language-agnostic (no embedded text) so
                # we don't end up showing a foreign-title backdrop card.
                "include_image_language": "null,en",
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
    backdrops = _backdrop_urls(body.get("images") or {})

    return {
        "tmdb_id": int(body.get("id")),
        "title": title,
        "summary": overview,
        "rating": us_rating,
        "release_date": release_date,
        "trailers": trailers,
        "poster_url": poster_url,
        "backdrops": backdrops,
    }


def _backdrop_urls(images_block: dict, *, max_count: int = 8) -> list[str]:
    """Return up to ``max_count`` landscape backdrop URLs, highest-rated first.

    TMDB's ``/movie/{id}/images`` (or ``append_to_response=images``) returns a
    ``backdrops`` list with each entry's ``file_path``, ``vote_average`` and
    ``aspect_ratio``. We sort by ``vote_average`` desc to favour the
    community-curated "best" stills, drop near-square crops (some re-releases
    sneak vertical posters into the backdrop bucket) and prefix the file_path
    with the w300 CDN base.
    """
    items = images_block.get("backdrops") or []
    cleaned = []
    for it in items:
        path = (it.get("file_path") or "").strip()
        if not path:
            continue
        ar = float(it.get("aspect_ratio") or 0)
        # Genuine 16:9 stills sit at ar≈1.78. Anything below 1.5 is
        # almost certainly a misfiled portrait poster.
        if ar and ar < 1.5:
            continue
        cleaned.append((float(it.get("vote_average") or 0), path))
    cleaned.sort(key=lambda x: x[0], reverse=True)
    return [_BACKDROP_BASE + p for _, p in cleaned[:max_count]]


def url_is_alive(url: str, *, timeout: float = 1.5) -> bool:
    """Best-effort HEAD check that a TMDB image URL still resolves to an
    actual image. Returns ``True`` on a 2xx, ``False`` on anything else
    (4xx, 5xx, network error, timeout). Used by the movie-card renderer
    to skip stale backdrop URLs at service time."""
    try:
        r = httpx.head(url, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError:
        return False
    return 200 <= r.status_code < 300


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
