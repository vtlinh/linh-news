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
                    "trailer_url": {
                        "type": "string",
                        "description": (
                            "Canonical YouTube watch URL of the official "
                            "trailer (https://www.youtube.com/watch?v=XXXXXXXXXXX). "
                            "Omit this field entirely if you cannot find a "
                            "real, verified trailer. Do NOT use search URLs, "
                            "embeds, or short links."
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
        f"(set status='upcoming').\n"
        f"Allowed MPAA ratings: {ratings}. The audience is a {age}-year-old, "
        f"so include only films a parent would consider watching with that "
        f"age.\n\nFor each movie include: official title, MPAA rating, "
        f"wide-release date (ISO), status, and a one or two sentence plot "
        f"summary. Also include trailer_url when you can find an actual, "
        f"verified YouTube watch URL of the official trailer "
        f"(https://www.youtube.com/watch?v=<11-char id>). Use web_search to "
        f"find the real video — do not invent IDs, do not use search URLs, "
        f"do not use embed or short-link URLs. If you cannot find a real "
        f"trailer for a movie, omit the trailer_url field for that movie. "
        f"Sort by release_date ascending. When done, call return_movies."
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
