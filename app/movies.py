"""Movie watchlist support for the admin /admin/movies page.

We ask Claude (with web_search) to return a long-horizon list of
kid-appropriate movies — currently in theaters or opening within the next
12 months — using a structured-output schema. The result is cached so the
admin page loads instantly on subsequent visits."""
from __future__ import annotations

import logging

from app import cache, claude_client
from app.calendar_oauth import allowed_movie_ratings, current_kid_age

log = logging.getLogger(__name__)

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
