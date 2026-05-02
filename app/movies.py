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
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import tmdb
from app.calendar_oauth import allowed_movie_ratings, current_kid_age  # noqa: F401  (re-exports kept for callers)
from app.db import Movie, session_factory

log = logging.getLogger(__name__)

# Daily edition / PDF visibility window: a movie shows up only if its
# wide-release date is within [today - 3 weeks, today + 2 months].
EDITION_PAST_WINDOW = timedelta(weeks=3)
EDITION_FUTURE_WINDOW = timedelta(days=60)

# Refresh policy: at most once per week.
_REFRESH_MIN_SECONDS = 7 * 24 * 60 * 60

# Discover window — extends past the ~3-month horizon of TMDB's curated
# now_playing / upcoming feeds.
_DISCOVER_PAST = timedelta(weeks=3)
_DISCOVER_FUTURE = timedelta(days=365)

_VALID_TRAILER_RE = re.compile(r"^https://www\.youtube\.com/watch\?v=[\w-]{8,}")


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
        len(feed), len(discover), len(popular), len(candidates),
    )

    ids = [int(c["id"]) for c in candidates if c.get("id") is not None]

    with tmdb._client() as client:  # noqa: SLF001 — same module family
        with ThreadPoolExecutor(max_workers=8) as pool:
            details = list(pool.map(
                lambda i: tmdb.fetch_movie_detail(i, client=client), ids
            ))

    rows: list[dict] = []
    now = datetime.now(timezone.utc)
    for d in details:
        if not d or not d.get("title"):
            continue
        rd = d.get("release_date")
        status = (
            "in_theaters" if (rd is not None and rd <= today) else "upcoming"
        )
        rows.append({
            "tmdb_id": d["tmdb_id"],
            "title": d["title"],
            "release_date": rd,
            "rating": d.get("rating") or "",
            "status": status,
            "summary": d.get("summary") or "",
            "trailers": d.get("trailers") or [],
            "poster_url": d.get("poster_url"),
            "fetched_at": now,
        })

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
        latest = latest.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - latest).total_seconds()


def _should_refresh() -> bool:
    age = movies_cache_age_seconds()
    return age is None or age >= _REFRESH_MIN_SECONDS


def _read_all_as_dicts() -> list[dict]:
    Maker = session_factory()
    with Maker() as s:
        rows = s.execute(
            select(Movie).order_by(Movie.release_date.asc().nullslast())
        ).scalars().all()
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
                index_elements=["tmdb_id"], set_=update_cols,
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
) -> tuple[list[dict], list[dict]]:
    """Return ``(in_theaters, coming_soon)`` lists for the daily edition.

    ``in_theaters`` are movies whose release_date is in
    ``[today - 3 weeks, today]``; ``coming_soon`` are movies whose
    release_date is in ``(today, today + 2 months]``."""
    earliest = today - EDITION_PAST_WINDOW
    latest = today + EDITION_FUTURE_WINDOW
    in_theaters: list[dict] = []
    coming_soon: list[dict] = []
    seen: set[str] = set()
    for m in movies:
        title = (m.get("title") or "").strip()
        if not title or title in seen or title in hidden_titles:
            continue
        if m.get("rating") not in allowed_ratings:
            continue
        rd = _parse_release(m)
        if rd is None or rd < earliest or rd > latest:
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
    return [
        t for t in (trailers or [])
        if isinstance(t, str) and _VALID_TRAILER_RE.match(t)
    ]


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
    parts = ['<span class="sources" tabindex="0">▶ Trailers'
             '<span class="sources-popup">']
    for i, url in enumerate(valid):
        label = "Official Trailer" if i == 0 else f"Trailer {i + 1}"
        parts.append(
            f'<a href="{html.escape(url)}" target="_blank" rel="noopener">'
            f'{html.escape(label)}</a>'
        )
    parts.append('</span></span>')
    return "".join(parts)


def _render_card_html(m: dict, today: date) -> str:
    title = (m.get("title") or "").strip()
    rd = _parse_release(m)
    if not title or rd is None:
        return ""
    summary = (m.get("summary") or "").strip()
    is_past = rd <= today
    sub = (
        f"In theaters since {_format_release_label(rd)}" if is_past
        else f"Opens {_format_release_label(rd)}"
    )
    title_esc = html.escape(title)
    summary_html = f'<p>{html.escape(summary)}</p>' if summary else ""
    trailer_html = _trailer_button_html(m.get("trailers") or [])
    return (
        '<article class="movie-card">'
        f'<button class="hide-movie" data-title="{title_esc}" '
        f'aria-label="Hide {title_esc}">×</button>'
        f'<div class="movie-title">{title_esc}</div>'
        f'<div class="movie-subtitle">{html.escape(sub)}</div>'
        f'{summary_html}{trailer_html}'
        '</article>'
    )


def render_html_section(
    movies: list[dict],
    today: date,
    *,
    hidden_titles: set[str],
    allowed_ratings: set[str],
) -> str:
    """Render the full Movies <section> block for the daily HTML edition.

    Returns an empty string when no movies match — the caller should drop
    the section entirely in that case."""
    in_theaters, coming_soon = filter_for_edition(
        movies, today,
        hidden_titles=hidden_titles, allowed_ratings=allowed_ratings,
    )
    if not in_theaters and not coming_soon:
        return ""
    parts: list[str] = ['<section><h2>🎬 Movies</h2>']
    if in_theaters:
        parts.append(
            '<h3 style="font-size:1em;margin:8px 0 4px">Now in theaters</h3>'
        )
        parts.extend(_render_card_html(m, today) for m in in_theaters)
    if coming_soon:
        parts.append(
            '<h3 style="font-size:1em;margin:12px 0 4px">Coming soon</h3>'
        )
        parts.extend(_render_card_html(m, today) for m in coming_soon)
    parts.append('</section>')
    return ''.join(parts)


def render_pdf_html(
    movies: list[dict],
    today: date,
    *,
    hidden_titles: set[str],
    allowed_ratings: set[str],
    max_items: int = 8,
) -> str:
    """Render a compact Movies block for the Linh Times PDF."""
    in_theaters, coming_soon = filter_for_edition(
        movies, today,
        hidden_titles=hidden_titles, allowed_ratings=allowed_ratings,
    )
    items = (in_theaters + coming_soon)[:max_items]
    if not items:
        return ""
    label_style = (
        "font-size:7pt;letter-spacing:.05em;text-transform:uppercase;"
        "border-bottom:0.5pt solid #000;margin:0 0 2pt;padding-bottom:1pt"
    )
    row_style = "margin:0 0 2pt;font-size:8pt;line-height:1.15"
    sub_style = "font-size:7pt;color:#555"
    parts = [f'<div style="{label_style}">Movies</div>']
    for m in items:
        title = (m.get("title") or "").strip()
        rd = _parse_release(m)
        if not title or rd is None:
            continue
        is_past = rd <= today
        sub = (
            f"In theaters {_format_release_label(rd)}" if is_past
            else f"Opens {_format_release_label(rd)}"
        )
        parts.append(
            f'<div style="{row_style}"><strong>{html.escape(title)}</strong> '
            f'<span style="{sub_style}">— {html.escape(sub)}</span></div>'
        )
    return "\n".join(parts)
