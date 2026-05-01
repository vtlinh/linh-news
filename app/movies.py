"""Movie watchlist support for the admin /admin/movies page.

We ask Claude (with web_search) to return a long-horizon list of
kid-appropriate movies — currently in theaters or opening within the next
12 months — using a structured-output schema. The result is cached so the
admin page loads instantly on subsequent visits.

The same cached list is reused (no extra LLM call) to render the Movies
section on the daily edition (HTML) and in the printed Linh Times (PDF) —
see ``filter_for_edition``, ``render_html_section``, ``render_pdf_html``."""
from __future__ import annotations

import html
import logging
import re
from datetime import date, timedelta

from app import cache, claude_client
from app.calendar_oauth import allowed_movie_ratings, current_kid_age

log = logging.getLogger(__name__)

# Daily edition / PDF visibility window: a movie shows up only if its
# wide-release date is within [today - 3 weeks, today + 2 months].
EDITION_PAST_WINDOW = timedelta(weeks=3)
EDITION_FUTURE_WINDOW = timedelta(days=60)

_VALID_TRAILER_RE = re.compile(r"^https://www\.youtube\.com/watch\?v=[\w-]{8,}")

MOVIES_SCHEMA = {
    "type": "object",
    "properties": {
        "movies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "rating": {
                        "type": "string",
                        "enum": ["G", "PG", "PG-13", "R", "NC-17"],
                    },
                    "release_date": {
                        "type": "string",
                        "description": "ISO date (YYYY-MM-DD). Wide-release date.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["in_theaters", "upcoming"],
                    },
                    "summary": {
                        "type": "string",
                        "description": "1–2 sentence plot summary.",
                    },
                    "trailers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Up to 3 canonical YouTube watch URLs of "
                            "real, verified trailers/teasers (in the form "
                            "https://www.youtube.com/watch?v=XXXXXXXXXXX). "
                            "Empty array if no trailers are available. "
                            "Do NOT use search URLs, embeds, or short links."
                        ),
                    },
                },
                "required": ["title", "rating", "release_date", "status", "summary"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["movies"],
    "additionalProperties": False,
}


def fetch_year_movie_list() -> list[dict]:
    """Ask Claude for movies in/coming to theaters within the next 12 months,
    filtered to kid-appropriate ratings."""
    ratings = allowed_movie_ratings()
    age = current_kid_age()
    system = (
        "You are compiling a 12-month movie outlook for a parent's planning "
        "page. Use web_search aggressively to find real, currently-scheduled "
        "movies. Do NOT invent titles or guess release dates."
    )
    user = (
        f"Return up to 80 movies in two groups:\n"
        f"  (a) **Currently in theaters** — every kid-appropriate film still "
        f"playing in US theaters today, regardless of how long ago it "
        f"opened (set status='in_theaters').\n"
        f"  (b) **Coming soon** — every kid-appropriate film with a US "
        f"theatrical release date between tomorrow and 365 days from now "
        f"(set status='upcoming').\n\n"
        f"**EXCLUDE all re-releases** — any film whose current theatrical "
        f"showing is a re-release, anniversary screening, restored cut, "
        f"director's cut, IMAX re-release, or any other return to theaters of "
        f"a film that previously had a US wide release. Only include first-run "
        f"original theatrical releases. If the same title was in US theaters "
        f"in any prior year, skip it.\n\n"
        f"Allowed MPAA ratings: {ratings}. The audience is a {age}-year-old, "
        f"so include only films a parent would consider watching with that "
        f"age.\n\nFor each movie include: official title, MPAA rating, "
        f"wide-release date (ISO), status, and a 2-3 sentence plot summary. "
        f"Posters are fetched server-side from TMDB — DO NOT include any "
        f"poster URL. Also include up to 3 real trailer YouTube watch URLs "
        f"(trailers; https://www.youtube.com/watch?v=<11-char id>). Verify "
        f"each trailer via web_search. Do not invent video IDs, do not use "
        f"search URLs, do not use embed or short-link URLs. If a movie has "
        f"no verified trailers, return trailers=[]. Sort by release_date "
        f"ascending. When done, call return_movies."
    )
    out = claude_client.call_with_schema(
        system=system,
        user=user,
        schema=MOVIES_SCHEMA,
        schema_name="return_movies",
        schema_description="Return the kid-appropriate movie watchlist.",
        extra_tools=[claude_client.WEB_SEARCH_TOOL],
        max_tokens=12000,
    )
    movies = list(out.get("movies", []))
    movies = [m for m in movies if m.get("rating") in ratings]
    return movies


def get_or_fetch_movies(force: bool = False) -> list[dict]:
    """Return cached movies; trigger a fetch if cache is stale or forced."""
    cached, _ = cache.get_movies()
    if cached is not None and not (force or cache.movies_should_refresh()):
        return cached
    try:
        movies = fetch_year_movie_list()
        cache.store_movies(movies)
        return movies
    except Exception as e:  # noqa: BLE001
        log.exception("Movie fetch failed: %s", e)
        return cached or []


# ───────────────────── Edition / PDF rendering ──────────────────────
# These helpers render the cached movie list (no LLM call) into the same
# shapes the daily HTML edition and the Linh Times PDF used to ask Claude
# to generate. Filtering rules:
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
