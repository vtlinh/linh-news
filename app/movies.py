"""Movie watchlist support, sourced from TMDB.

The full unfiltered watchlist (all MPAA ratings) lives in the ``movies`` DB
table. :func:`fetch_year_movie_list` calls TMDB once a week to refresh the
table, merging two candidate sources:

* ``GET /movie/now_playing`` + ``GET /movie/upcoming`` — TMDB's curated US
  theatrical feeds (mainstream Hollywood, ~3-month forward horizon).
* ``GET /discover/movie`` restricted to US wide-theatrical releases with
  US certification data and a minimum vote count, for primary releases in
  ``[today - 21 days, today + 365 days]`` — catches mainstream titles
  further out than the curated feeds reach.

Then ``GET /movie/{id}?append_to_response=videos,release_dates`` per
candidate to extract MPAA cert, plot summary, YouTube trailer URLs, and
poster URL.

Filtering (allowed MPAA ratings + admin-hidden titles + edition window) is
applied at service time:

* :func:`filter_for_edition` — used by both the daily HTML edition's
  ``_inject_movies`` and PDF generation.
* :func:`render_html_section` / :func:`render_pdf_html` — render the
  filtered list.

The admin Movies page calls :func:`get_movies` directly and applies its own
rating filter from the query string."""

from __future__ import annotations

import html
import logging
import random
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import tmdb
from app.calendar_oauth import (  # noqa: F401  (re-exports kept for callers)
    allowed_movie_ratings,
    current_kid_age,
)
from app.db import Movie, session_factory

log = logging.getLogger(__name__)

# Daily edition / PDF visibility window: a movie shows up only if its
# wide-release date is within [today - 3 weeks, today + 2 months].
EDITION_PAST_WINDOW = timedelta(weeks=3)
EDITION_FUTURE_WINDOW = timedelta(days=60)
# Favorite-only inclusion: admin-marked favorites bypass the MPAA rating
# filter when their release falls in [today - 3 weeks, today + 1 month].
# Tighter forward window than the standard so the edition isn't dragged
# forward by a long-horizon must-watch sequel.
EDITION_FAVORITE_FUTURE_WINDOW = timedelta(days=30)

# Refresh policy: at most once per week.
_REFRESH_MIN_SECONDS = 7 * 24 * 60 * 60

# Discover window — extends past the ~3-month horizon of TMDB's curated
# now_playing / upcoming feeds.
_DISCOVER_PAST = timedelta(weeks=3)
_DISCOVER_FUTURE = timedelta(days=365)

_VALID_TRAILER_RE = re.compile(r"^https://www\.youtube\.com/watch\?v=[\w-]{8,}")
_VALID_BACKDROP_RE = re.compile(r"^https://image\.tmdb\.org/t/p/[\w]+/[\w./-]+$")


def _pick_backdrop(
    m: dict,
    *,
    rng: random.Random | None = None,
    liveness_check: bool = True,
) -> str | None:
    """Return one backdrop URL chosen at random, skipping any URL that no
    longer resolves on the TMDB CDN. Returns ``None`` when the movie has no
    usable backdrops (or all of them are dead).

    Validates the URL shape first so a malformed cache entry can't slip an
    arbitrary string into the rendered page. Then shuffles and HEAD-checks
    each candidate in turn; the first 2xx wins. ``liveness_check=False``
    short-circuits the HEAD checks (used by tests to keep them offline)."""
    raw = m.get("backdrops") or []
    valid = [u for u in raw if isinstance(u, str) and _VALID_BACKDROP_RE.match(u)]
    if not valid:
        return None
    if not liveness_check:
        return (rng or random).choice(valid)
    shuffled = list(valid)
    (rng or random).shuffle(shuffled)
    for url in shuffled:
        if tmdb.url_is_alive(url):
            return url
    return None


def fetch_year_movie_list() -> list[dict]:
    """Refresh the ``movies`` table from TMDB. Returns the upserted list of
    dicts (same shape as :func:`get_movies`).

    No MPAA-rating filter is applied here — every certification is stored
    so service-time filters can include or exclude any rating without a
    refetch.

    Three candidate sources are merged:

    * curated US theatrical feeds (``now_playing`` + ``upcoming``) for
      mainstream Hollywood in the next ~3 months,
    * cert-gated discover for mainstream titles further out (up to a year),
    * popularity-sorted discover (10 pages) for announced pre-release
      sequels with no votes/cert yet (e.g. The Angry Birds Movie 3).
    """
    today = date.today()
    earliest = today - _DISCOVER_PAST
    latest = today + _DISCOVER_FUTURE
    feed = tmdb.now_playing_and_upcoming()
    discover = tmdb.discover_us_theatrical(earliest=earliest, latest=latest)
    popular = tmdb.discover_popular_upcoming(earliest=earliest, latest=latest)
    # Merge & de-dupe by id, preserving feed entries first (they're the
    # mainstream curated set), then the cert-gated discover pass, then
    # the popularity-sorted pre-release pass.
    seen: set[int] = set()
    candidates: list[dict] = []
    for m in feed + discover + popular:
        mid = m.get("id")
        if mid is None or int(mid) in seen:
            continue
        seen.add(int(mid))
        candidates.append(m)
    if not candidates:
        log.warning("TMDB feeds returned no results — table left untouched.")
        return _read_all_as_dicts()
    log.info(
        "TMDB candidates: %d from now_playing+upcoming, %d from cert-gated "
        "discover, %d from popularity-sorted discover, %d unique after merge.",
        len(feed),
        len(discover),
        len(popular),
        len(candidates),
    )

    ids = [int(c["id"]) for c in candidates if c.get("id") is not None]

    with tmdb._client() as client, ThreadPoolExecutor(max_workers=8) as pool:  # noqa: SLF001 — same module family
        details = list(pool.map(lambda i: tmdb.fetch_movie_detail(i, client=client), ids))

    rows: list[dict] = []
    now = datetime.now(UTC)
    for d in details:
        if not d or not d.get("title"):
            continue
        rd = d.get("release_date")
        status = "in_theaters" if (rd is not None and rd <= today) else "upcoming"
        rows.append(
            {
                "tmdb_id": d["tmdb_id"],
                "title": d["title"],
                "release_date": rd,
                "rating": d.get("rating") or "",
                "status": status,
                "summary": d.get("summary") or "",
                "trailers": d.get("trailers") or [],
                "poster_url": d.get("poster_url"),
                "backdrops": d.get("backdrops") or [],
                "fetched_at": now,
            }
        )

    _upsert_movies(rows, today=today)
    return _read_all_as_dicts()


def get_movies(*, refresh_if_stale: bool = False) -> list[dict]:
    """Return all rows from the ``movies`` table as dicts.

    With ``refresh_if_stale=True``, kicks off a TMDB refresh first if the
    most recent ``fetched_at`` is older than the weekly threshold (or if
    the table is empty). Failures during refresh are logged and the existing
    table contents are returned unchanged."""
    if refresh_if_stale and _should_refresh():
        try:
            fetch_year_movie_list()
        except Exception as e:  # noqa: BLE001
            log.exception("Movie refresh failed: %s", e)
    return _read_all_as_dicts()


def movies_cache_age_seconds() -> float | None:
    """Return seconds since the most recent ``fetched_at``, or ``None`` if
    the table is empty."""
    Maker = session_factory()
    with Maker() as s:
        latest = s.execute(select(func.max(Movie.fetched_at))).scalar()
    if latest is None:
        return None
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=UTC)
    return (datetime.now(UTC) - latest).total_seconds()


def _should_refresh() -> bool:
    age = movies_cache_age_seconds()
    return age is None or age >= _REFRESH_MIN_SECONDS


def _read_all_as_dicts() -> list[dict]:
    Maker = session_factory()
    with Maker() as s:
        rows = (
            s.execute(select(Movie).order_by(Movie.release_date.asc().nullslast())).scalars().all()
        )
        return [m.to_dict() for m in rows]


def _upsert_movies(rows: list[dict], *, today: date) -> None:
    """Upsert the given rows by tmdb_id. Also delete stale rows that fell
    out of TMDB's response and whose release_date is already outside the
    edition past window (so we don't churn rows that may matter for an
    edition viewed today)."""
    if not rows:
        return
    Maker = session_factory()
    fresh_ids = {r["tmdb_id"] for r in rows}
    cutoff = today - EDITION_PAST_WINDOW
    with Maker() as s:
        bind = s.get_bind()
        dialect = bind.dialect.name if bind is not None else ""
        if dialect == "postgresql":
            stmt = pg_insert(Movie.__table__).values(rows)
            update_cols = {
                c.name: stmt.excluded[c.name]
                for c in Movie.__table__.columns
                if c.name != "tmdb_id"
            }
            stmt = stmt.on_conflict_do_update(
                index_elements=["tmdb_id"],
                set_=update_cols,
            )
            s.execute(stmt)
        else:
            # SQLite path used in tests — emulate upsert with merge.
            for r in rows:
                existing = s.get(Movie, r["tmdb_id"])
                if existing is None:
                    s.add(Movie(**r))
                else:
                    for k, v in r.items():
                        setattr(existing, k, v)
        # Drop rows that fell out of TMDB and are already past the edition
        # window — anything still in-window stays so a viewer's current
        # edition isn't disrupted mid-week.
        s.execute(
            delete(Movie).where(
                ~Movie.tmdb_id.in_(fresh_ids),
                (Movie.release_date.is_(None)) | (Movie.release_date < cutoff),
            )
        )
        s.commit()


# ───────────────────── Edition / PDF rendering ──────────────────────
# Filtering rules (applied at service time):
#   * exclude any title in ``hidden_titles``
#   * include only ratings in ``allowed_ratings``
#   * include only movies whose release_date is within
#     [today - 3 weeks, today + 2 months]
#   * dedupe by title


def _parse_release(m: dict) -> date | None:
    raw = (m.get("release_date") or "").strip()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def filter_for_edition(
    movies: list[dict],
    today: date,
    *,
    hidden_titles: set[str],
    allowed_ratings: set[str],
    favorite_titles: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Return ``(in_theaters, coming_soon)`` lists for the daily edition.

    Standard inclusion: release_date in ``[today - 3 weeks, today + 2 months]``
    AND ``rating`` in ``allowed_ratings`` AND title not in ``hidden_titles``.

    Favorites override the rating filter on a tighter forward window:
    if a movie's title is in ``favorite_titles`` and its release_date is
    in ``[today - 3 weeks, today + 1 month]``, it is included regardless
    of ``allowed_ratings``. ``hidden_titles`` still applies."""
    favorite_titles = favorite_titles or set()
    earliest = today - EDITION_PAST_WINDOW
    latest = today + EDITION_FUTURE_WINDOW
    fav_latest = today + EDITION_FAVORITE_FUTURE_WINDOW
    in_theaters: list[dict] = []
    coming_soon: list[dict] = []
    seen: set[str] = set()
    for m in movies:
        title = (m.get("title") or "").strip()
        if not title or title in seen or title in hidden_titles:
            continue
        rd = _parse_release(m)
        if rd is None or rd < earliest:
            continue
        is_fav = title in favorite_titles
        if is_fav:
            if rd > fav_latest:
                continue
        else:
            if rd > latest:
                continue
            if m.get("rating") not in allowed_ratings:
                continue
        seen.add(title)
        if rd <= today:
            in_theaters.append(m)
        else:
            coming_soon.append(m)
    in_theaters.sort(key=lambda m: _parse_release(m) or date.max, reverse=True)
    coming_soon.sort(key=lambda m: _parse_release(m) or date.max)
    return in_theaters, coming_soon


def _format_release_label(d: date) -> str:
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def _valid_trailers(trailers) -> list[str]:
    return [t for t in (trailers or []) if isinstance(t, str) and _VALID_TRAILER_RE.match(t)]


def _trailer_button_html(trailers: list[str]) -> str:
    valid = _valid_trailers(trailers)
    if not valid:
        return ""
    if len(valid) == 1:
        return (
            f'<a class="sources" href="{html.escape(valid[0])}" '
            'target="_blank" rel="noopener" '
            'title="Watch the trailer on YouTube">▶ Trailer</a>'
        )
    parts = ['<span class="sources" tabindex="0">▶ Trailers<span class="sources-popup">']
    for i, url in enumerate(valid):
        label = "Official Trailer" if i == 0 else f"Trailer {i + 1}"
        parts.append(
            f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{html.escape(label)}</a>'
        )
    parts.append("</span></span>")
    return "".join(parts)


def _render_card_html(
    m: dict,
    today: date,
    *,
    rng: random.Random | None = None,
) -> str:
    title = (m.get("title") or "").strip()
    rd = _parse_release(m)
    if not title or rd is None:
        return ""
    summary = (m.get("summary") or "").strip()
    is_past = rd <= today
    sub = (
        f"In theaters since {_format_release_label(rd)}"
        if is_past
        else f"Opens {_format_release_label(rd)}"
    )
    title_esc = html.escape(title)
    summary_html = f"<p>{html.escape(summary)}</p>" if summary else ""
    trailer_html = _trailer_button_html(m.get("trailers") or [])
    backdrop = _pick_backdrop(m, rng=rng)
    backdrop_html = (
        f'<img class="movie-backdrop" src="{html.escape(backdrop)}" alt="" loading="lazy">'
        if backdrop
        else ""
    )
    # The title links out to the movie's TMDB page when we know the id;
    # otherwise it renders as plain text.
    tmdb_id = m.get("tmdb_id")
    if tmdb_id:
        title_inner = (
            f'<a href="https://www.themoviedb.org/movie/{int(tmdb_id)}" '
            f'target="_blank" rel="noopener">{title_esc}</a>'
        )
    else:
        title_inner = title_esc
    # No inline ✕ on the card — the X visually attaches to the backdrop
    # image and reads as "discard this image", which we don't support.
    # Admins still hide movies from the dedicated /movies admin page.
    return (
        '<article class="movie-card">'
        f"{backdrop_html}"
        f'<div class="movie-title">{title_inner}</div>'
        f'<div class="movie-subtitle">{html.escape(sub)}</div>'
        f"{summary_html}{trailer_html}"
        "</article>"
    )


def render_html_section(
    movies: list[dict],
    today: date,
    *,
    hidden_titles: set[str],
    allowed_ratings: set[str],
    favorite_titles: set[str] | None = None,
) -> str:
    """Render the full Movies <section> block for the daily HTML edition.

    Returns an empty string when no movies match — the caller should drop
    the section entirely in that case."""
    in_theaters, coming_soon = filter_for_edition(
        movies,
        today,
        hidden_titles=hidden_titles,
        allowed_ratings=allowed_ratings,
        favorite_titles=favorite_titles,
    )
    if not in_theaters and not coming_soon:
        return ""
    parts: list[str] = ["<section><h2>🎬 Movies</h2>"]
    if in_theaters:
        parts.append('<h3 style="font-size:1em;margin:8px 0 4px">Now in theaters</h3>')
        parts.extend(_render_card_html(m, today) for m in in_theaters)
    if coming_soon:
        parts.append('<h3 style="font-size:1em;margin:12px 0 4px">Coming soon</h3>')
        parts.extend(_render_card_html(m, today) for m in coming_soon)
    parts.append("</section>")
    return "".join(parts)


def render_pdf_html(
    movies: list[dict],
    today: date,
    *,
    hidden_titles: set[str],
    allowed_ratings: set[str],
    favorite_titles: set[str] | None = None,
    max_items: int = 8,
    min_items: int = 5,
) -> str:
    """Render the Movies block for the Linh Times PDF rail.

    Always renders at least ``min_items`` cards (when available), each with
    title, release-date subtitle, and the short ``summary`` description.
    """
    in_theaters, coming_soon = filter_for_edition(
        movies,
        today,
        hidden_titles=hidden_titles,
        allowed_ratings=allowed_ratings,
        favorite_titles=favorite_titles,
    )
    pool = in_theaters + coming_soon
    target = max(min_items, 0)
    items = pool[: max(target, max_items)] if pool else []
    if not items:
        return ""
    label_style = (
        "font-size:11pt;letter-spacing:.05em;text-transform:uppercase;"
        "border-bottom:0.5pt solid #000;margin:0 0 3pt;padding-bottom:1pt;"
        "font-weight:bold"
    )
    row_style = "margin:0 0 8pt;line-height:1.25;break-inside:avoid"
    # Font sizes for .movie-title and .movie-desc come from the user
    # stylesheet in app.pdf._make_css and scale with the fit-algorithm's
    # chosen base_pt (10-20pt body, title +20%). Subtitle stays a fixed
    # small caption.
    title_style = "font-weight:bold;line-height:1.2"
    sub_style = "font-size:9pt;color:#555"
    desc_style = "color:#222;margin-top:2pt;line-height:1.3"
    # Backdrops keep their original aspect ratio (no cover-cropping). With a
    # 2.4in rail and ~16:9 TMDB stills this is ~1.35in tall per card. The
    # two-phase fit loop in app/pdf.py drops movie cards (last first) and
    # then calendar events when the rail can't fit at the minimum font.
    # Backdrops are physically downscaled to one column-width before
    # embedding (see app/images.fetch_for_pdf). With intrinsic width
    # already at the column size, we use width:auto so WeasyPrint never
    # has to inflate the image to fit a flex container.
    backdrop_style = "display:block;width:auto;max-width:100%;height:auto;margin:0 0 3pt"
    parts = [f'<div style="{label_style}">Movies</div>']
    for m in items:
        title = (m.get("title") or "").strip()
        rd = _parse_release(m)
        if not title or rd is None:
            continue
        is_past = rd <= today
        sub = (
            f"In theaters {_format_release_label(rd)}"
            if is_past
            else f"Opens {_format_release_label(rd)}"
        )
        summary = (m.get("summary") or "").strip()
        desc_html = (
            f'<div class="movie-desc" style="{desc_style}">{html.escape(summary)}</div>'
            if summary else ""
        )
        backdrop_url = _pick_backdrop(m)
        backdrop_html = ""
        if backdrop_url:
            from app import images as _images
            fetched = _images.fetch_for_pdf(backdrop_url)
            if fetched is not None:
                src = _images.to_data_uri(*fetched)
                backdrop_html = (
                    f'<img class="movie-backdrop" src="{src}" '
                    f'alt="" style="{backdrop_style}">'
                )
        parts.append(
            f'<div class="movie-card" style="{row_style}">{backdrop_html}'
            f'<div class="movie-title" style="{title_style}">{html.escape(title)}</div>'
            f'<div style="{sub_style}">{html.escape(sub)}</div>'
            f"{desc_html}</div>"
        )
    return "\n".join(parts)
