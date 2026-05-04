from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

import pytest

from app import movies, overlays, tmdb
from app.db import Movie


@pytest.fixture(autouse=True)
def _stub_backdrop_liveness(monkeypatch):
    """The backdrop renderer HEAD-checks each candidate URL before serving
    it. Tests fabricate URLs that aren't real TMDB CDN entries — stub the
    network probe to always say 'alive' so tests stay offline and fast."""
    monkeypatch.setattr(tmdb, "url_is_alive", lambda url, **_: True)


def _make(
    title: str,
    rating: str,
    rd: date,
    *,
    summary: str = "Plot.",
    trailers: list[str] | None = None,
    backdrops: list[str] | None = None,
) -> dict:
    return {
        "title": title,
        "rating": rating,
        "release_date": rd.isoformat(),
        "status": "in_theaters" if rd <= date(2026, 5, 1) else "upcoming",
        "summary": summary,
        "trailers": trailers or [],
        "backdrops": backdrops or [],
    }


def test_filter_for_edition_window():
    today = date(2026, 5, 1)
    inside_in_theaters = _make("Inside Theaters", "PG", today - timedelta(days=10))
    too_old = _make("Old Film", "PG", today - timedelta(days=30))
    inside_upcoming = _make("Soon", "PG", today + timedelta(days=30))
    too_far = _make("Far Away", "PG", today + timedelta(days=120))
    wrong_rating = _make("Adult", "R", today + timedelta(days=10))
    hidden = _make("Hidden Title", "PG", today)

    in_theaters, coming_soon = movies.filter_for_edition(
        [
            inside_in_theaters,
            too_old,
            inside_upcoming,
            too_far,
            wrong_rating,
            hidden,
        ],
        today,
        hidden_titles={"Hidden Title"},
        allowed_ratings={"G", "PG"},
    )
    assert [m["title"] for m in in_theaters] == ["Inside Theaters"]
    assert [m["title"] for m in coming_soon] == ["Soon"]


def test_favorite_overrides_rating_within_favorite_window():
    today = date(2026, 5, 1)
    # PG-13 normally allowed in {"G","PG"} would NOT be — but as a favorite
    # within 30 days, it must be included.
    fav_pg13 = _make("Fav Drama", "PG-13", today + timedelta(days=20))
    in_theaters, coming_soon = movies.filter_for_edition(
        [fav_pg13],
        today,
        hidden_titles=set(),
        allowed_ratings={"G", "PG"},
        favorite_titles={"Fav Drama"},
    )
    assert [m["title"] for m in coming_soon] == ["Fav Drama"]
    assert in_theaters == []


def test_favorite_outside_tight_window_excluded():
    today = date(2026, 5, 1)
    # A favorite released > 30 days out is excluded — favorite window is
    # tighter than the standard 60-day window.
    fav_far = _make("Fav Far", "PG", today + timedelta(days=45))
    in_theaters, coming_soon = movies.filter_for_edition(
        [fav_far],
        today,
        hidden_titles=set(),
        allowed_ratings={"G", "PG"},
        favorite_titles={"Fav Far"},
    )
    assert in_theaters == [] and coming_soon == []


def test_favorite_does_not_override_hidden():
    today = date(2026, 5, 1)
    fav_and_hidden = _make("Both", "PG", today + timedelta(days=10))
    in_theaters, coming_soon = movies.filter_for_edition(
        [fav_and_hidden],
        today,
        hidden_titles={"Both"},
        allowed_ratings={"G", "PG"},
        favorite_titles={"Both"},
    )
    assert in_theaters == [] and coming_soon == []


def test_non_favorite_uses_full_60_day_window():
    today = date(2026, 5, 1)
    # A non-favorite PG release at +45 days is still in the standard
    # 60-day window — ensures the favorite-window change didn't shrink
    # the standard window.
    m = _make("Standard", "PG", today + timedelta(days=45))
    _, coming_soon = movies.filter_for_edition(
        [m],
        today,
        hidden_titles=set(),
        allowed_ratings={"G", "PG"},
    )
    assert [x["title"] for x in coming_soon] == ["Standard"]


def test_filter_for_edition_dedupes_by_title():
    today = date(2026, 5, 1)
    a = _make("Same Movie", "PG", today - timedelta(days=5))
    b = _make("Same Movie", "PG", today + timedelta(days=5))
    in_theaters, coming_soon = movies.filter_for_edition(
        [a, b],
        today,
        hidden_titles=set(),
        allowed_ratings={"PG"},
    )
    assert len(in_theaters) + len(coming_soon) == 1


def test_filter_for_edition_uses_release_date_not_status():
    today = date(2026, 5, 1)
    # Cache says in_theaters, but release was 4 weeks ago → drop it.
    stale = {
        "title": "Stale",
        "rating": "PG",
        "release_date": (today - timedelta(days=28)).isoformat(),
        "status": "in_theaters",
        "summary": "",
        "trailers": [],
    }
    in_theaters, coming_soon = movies.filter_for_edition(
        [stale],
        today,
        hidden_titles=set(),
        allowed_ratings={"PG"},
    )
    assert in_theaters == []
    assert coming_soon == []


def test_render_html_section_basic():
    today = date(2026, 5, 1)
    items = [
        _make(
            "Now Showing",
            "PG",
            today - timedelta(days=5),
            trailers=["https://www.youtube.com/watch?v=abcdefghijk"],
        ),
        _make(
            "Coming Soon",
            "PG",
            today + timedelta(days=20),
            trailers=[
                "https://www.youtube.com/watch?v=11111111111",
                "https://www.youtube.com/watch?v=22222222222",
            ],
        ),
    ]
    html = movies.render_html_section(
        items,
        today,
        hidden_titles=set(),
        allowed_ratings={"PG"},
    )
    assert "<section>" in html
    assert "<h2>" in html and "Movies" in html
    assert "Now in theaters" in html
    assert "Coming soon" in html
    assert "Now Showing" in html
    assert "Coming Soon" in html
    assert "In theaters since" in html
    assert "Opens" in html
    # The inline ✕ hide button was removed from cards (admins use /movies).
    assert "hide-movie" not in html
    # Single-trailer renders as a direct anchor
    assert "▶ Trailer" in html
    # Multi-trailer renders the popup
    assert "▶ Trailers" in html
    assert "sources-popup" in html


def test_render_html_section_empty():
    today = date(2026, 5, 1)
    assert (
        movies.render_html_section(
            [],
            today,
            hidden_titles=set(),
            allowed_ratings={"PG"},
        )
        == ""
    )


def test_render_pdf_html_basic():
    today = date(2026, 5, 1)
    items = [
        _make("In Theaters", "PG", today - timedelta(days=5)),
        _make("Future", "PG", today + timedelta(days=20)),
    ]
    pdf = movies.render_pdf_html(
        items,
        today,
        hidden_titles=set(),
        allowed_ratings={"PG"},
    )
    assert "Movies" in pdf
    assert "In Theaters" in pdf
    assert "Future" in pdf


def test_render_pdf_html_empty_when_no_matches():
    today = date(2026, 5, 1)
    out = movies.render_pdf_html(
        [],
        today,
        hidden_titles=set(),
        allowed_ratings={"PG"},
    )
    assert out == ""


def _fake_detail(
    *,
    tmdb_id: int,
    title: str,
    rating: str,
    rd: date,
    summary: str = "Plot.",
    trailers: list[str] | None = None,
    poster_url: str | None = "https://image.tmdb.org/t/p/w500/x.jpg",
    backdrops: list[str] | None = None,
) -> dict:
    return {
        "tmdb_id": tmdb_id,
        "title": title,
        "summary": summary,
        "rating": rating,
        "release_date": rd,
        "trailers": trailers or [],
        "poster_url": poster_url,
        "backdrops": backdrops or [],
    }


def test_fetch_year_movie_list_populates_table(db_session):
    today = date.today()
    candidates = [{"id": 1}, {"id": 2}, {"id": 3}]
    details = {
        1: _fake_detail(
            tmdb_id=1,
            title="Family Flick",
            rating="PG",
            rd=today + timedelta(days=10),
            trailers=["https://www.youtube.com/watch?v=aaaaaaaaaaa"],
        ),
        2: _fake_detail(
            tmdb_id=2,
            title="Adult Drama",
            rating="R",
            rd=today + timedelta(days=20),
        ),
        3: _fake_detail(
            tmdb_id=3,
            title="Now Showing",
            rating="PG-13",
            rd=today - timedelta(days=5),
        ),
    }

    with (
        patch.object(movies.tmdb, "now_playing_and_upcoming", return_value=candidates),
        patch.object(movies.tmdb, "discover_us_theatrical", return_value=[]),
        patch.object(movies.tmdb, "discover_popular_upcoming", return_value=[]),
        patch.object(
            movies.tmdb,
            "fetch_movie_detail",
            side_effect=lambda i, client=None: details[i],
        ),
    ):
        out = movies.fetch_year_movie_list()

    assert {m["title"] for m in out} == {"Family Flick", "Adult Drama", "Now Showing"}
    # R-rated row is in the table — service-time filter is what excludes it.
    titles_by_rating = {m["title"]: m["rating"] for m in out}
    assert titles_by_rating["Adult Drama"] == "R"
    # status derived from release_date vs today
    by_title = {m["title"]: m for m in out}
    assert by_title["Now Showing"]["status"] == "in_theaters"
    assert by_title["Family Flick"]["status"] == "upcoming"
    # poster_url stored
    assert by_title["Family Flick"]["poster_url"].startswith("https://image.tmdb.org/")
    # cache_age is fresh
    assert (movies.movies_cache_age_seconds() or 1e9) < 60


def test_fetch_merges_feeds_and_discover_dedup(db_session):
    today = date.today()
    feed = [{"id": 1}, {"id": 2}]
    discover = [{"id": 2}, {"id": 3}]  # id=2 dup with feed
    details = {
        1: _fake_detail(tmdb_id=1, title="Feed Only", rating="PG", rd=today),
        2: _fake_detail(tmdb_id=2, title="In Both", rating="PG-13", rd=today),
        3: _fake_detail(tmdb_id=3, title="Discover Only", rating="R", rd=today),
    }
    with (
        patch.object(movies.tmdb, "now_playing_and_upcoming", return_value=feed),
        patch.object(movies.tmdb, "discover_us_theatrical", return_value=discover),
        patch.object(movies.tmdb, "discover_popular_upcoming", return_value=[]),
        patch.object(
            movies.tmdb,
            "fetch_movie_detail",
            side_effect=lambda i, client=None: details[i],
        ),
    ):
        out = movies.fetch_year_movie_list()

    titles = {m["title"] for m in out}
    assert titles == {"Feed Only", "In Both", "Discover Only"}


def test_fetch_includes_popular_upcoming_pass(db_session):
    """Pre-release sequels with no votes/cert (e.g. Angry Birds 3) must
    arrive via the popularity-sorted discover pass."""
    today = date.today()
    feed: list[dict] = []
    discover: list[dict] = []  # cert-gated pass excludes pre-release sequels
    popular = [{"id": 1300926}]  # Angry Birds 3 stand-in
    details = {
        1300926: _fake_detail(
            tmdb_id=1300926,
            title="The Angry Birds Movie 3",
            rating="",
            rd=today + timedelta(days=200),
        ),
    }
    with (
        patch.object(movies.tmdb, "now_playing_and_upcoming", return_value=feed),
        patch.object(movies.tmdb, "discover_us_theatrical", return_value=discover),
        patch.object(movies.tmdb, "discover_popular_upcoming", return_value=popular),
        patch.object(
            movies.tmdb,
            "fetch_movie_detail",
            side_effect=lambda i, client=None: details[i],
        ),
    ):
        out = movies.fetch_year_movie_list()
    assert any(m["title"] == "The Angry Birds Movie 3" for m in out)


def test_filter_for_edition_excludes_disallowed_rating_from_db_rows(db_session):
    today = date.today()
    candidates = [{"id": 10}, {"id": 11}]
    details = {
        10: _fake_detail(
            tmdb_id=10,
            title="Kid OK",
            rating="PG",
            rd=today + timedelta(days=15),
        ),
        11: _fake_detail(
            tmdb_id=11,
            title="Adult Only",
            rating="R",
            rd=today + timedelta(days=15),
        ),
    }
    with (
        patch.object(movies.tmdb, "now_playing_and_upcoming", return_value=candidates),
        patch.object(movies.tmdb, "discover_us_theatrical", return_value=[]),
        patch.object(movies.tmdb, "discover_popular_upcoming", return_value=[]),
        patch.object(
            movies.tmdb,
            "fetch_movie_detail",
            side_effect=lambda i, client=None: details[i],
        ),
    ):
        rows = movies.fetch_year_movie_list()

    assert len(rows) == 2  # both stored
    in_theaters, coming_soon = movies.filter_for_edition(
        rows,
        today,
        hidden_titles=set(),
        allowed_ratings={"G", "PG"},
    )
    titles = {m["title"] for m in in_theaters + coming_soon}
    assert "Kid OK" in titles
    assert "Adult Only" not in titles


def test_hidden_title_filtered_without_touching_table(db_session):
    today = date.today()
    candidates = [{"id": 20}]
    details = {
        20: _fake_detail(
            tmdb_id=20,
            title="Will Be Hidden",
            rating="PG",
            rd=today + timedelta(days=10),
        ),
    }
    with (
        patch.object(movies.tmdb, "now_playing_and_upcoming", return_value=candidates),
        patch.object(movies.tmdb, "discover_us_theatrical", return_value=[]),
        patch.object(movies.tmdb, "discover_popular_upcoming", return_value=[]),
        patch.object(
            movies.tmdb,
            "fetch_movie_detail",
            side_effect=lambda i, client=None: details[i],
        ),
    ):
        rows = movies.fetch_year_movie_list()

    overlays.hide_movie(db_session, "Will Be Hidden")

    in_theaters, coming_soon = movies.filter_for_edition(
        rows,
        today,
        hidden_titles={"Will Be Hidden"},
        allowed_ratings={"PG"},
    )
    assert in_theaters == [] and coming_soon == []
    # Row still exists in the table — hide is a service-time overlay only.
    assert db_session.query(Movie).filter_by(tmdb_id=20).count() == 1


def test_render_html_section_includes_random_backdrop():
    today = date(2026, 5, 1)
    items = [
        _make(
            "Now Showing",
            "PG",
            today - timedelta(days=5),
            backdrops=[
                "https://image.tmdb.org/t/p/w780/aaa.jpg",
                "https://image.tmdb.org/t/p/w780/bbb.jpg",
            ],
        ),
    ]
    rendered = movies.render_html_section(
        items,
        today,
        hidden_titles=set(),
        allowed_ratings={"PG"},
    )
    assert 'class="movie-backdrop"' in rendered
    # Picked URL must be one of the candidates.
    assert (
        "image.tmdb.org/t/p/w780/aaa.jpg" in rendered
        or "image.tmdb.org/t/p/w780/bbb.jpg" in rendered
    )


def test_render_html_section_no_backdrop_when_empty():
    today = date(2026, 5, 1)
    items = [_make("No Backdrops", "PG", today, backdrops=[])]
    rendered = movies.render_html_section(
        items,
        today,
        hidden_titles=set(),
        allowed_ratings={"PG"},
    )
    assert 'class="movie-backdrop"' not in rendered


def test_render_pdf_html_includes_backdrop():
    today = date(2026, 5, 1)
    items = [
        _make(
            "PDF Movie",
            "PG",
            today,
            backdrops=["https://image.tmdb.org/t/p/w780/zzz.jpg"],
        ),
    ]
    pdf = movies.render_pdf_html(
        items,
        today,
        hidden_titles=set(),
        allowed_ratings={"PG"},
    )
    assert 'class="movie-backdrop"' in pdf
    assert "image.tmdb.org/t/p/w780/zzz.jpg" in pdf


def test_render_html_section_links_title_to_tmdb():
    today = date(2026, 5, 1)
    items = [_make("Linkable", "PG", today)]
    items[0]["tmdb_id"] = 12345
    rendered = movies.render_html_section(
        items,
        today,
        hidden_titles=set(),
        allowed_ratings={"PG"},
    )
    assert 'href="https://www.themoviedb.org/movie/12345"' in rendered
    assert 'target="_blank"' in rendered
    assert "Linkable</a>" in rendered


def test_render_html_section_no_link_when_tmdb_id_missing():
    today = date(2026, 5, 1)
    items = [_make("No Id", "PG", today)]  # _make does not set tmdb_id
    rendered = movies.render_html_section(
        items,
        today,
        hidden_titles=set(),
        allowed_ratings={"PG"},
    )
    assert "themoviedb.org" not in rendered


def test_pick_backdrop_rejects_malformed_urls():
    m = {
        "backdrops": [
            "not a url",
            "javascript:alert(1)",
            "https://evil.example/x.jpg",  # not the TMDB CDN
        ],
    }
    assert movies._pick_backdrop(m) is None


def test_pick_backdrop_skips_dead_urls(monkeypatch):
    dead = "https://image.tmdb.org/t/p/w300/dead.jpg"
    alive = "https://image.tmdb.org/t/p/w300/alive.jpg"
    monkeypatch.setattr(tmdb, "url_is_alive", lambda url, **_: url == alive)
    m = {"backdrops": [dead, alive]}
    # Repeat a few times so we exercise both shuffle orderings.
    for _ in range(20):
        assert movies._pick_backdrop(m) == alive


def test_pick_backdrop_returns_none_when_all_dead(monkeypatch):
    monkeypatch.setattr(tmdb, "url_is_alive", lambda url, **_: False)
    m = {
        "backdrops": [
            "https://image.tmdb.org/t/p/w300/a.jpg",
            "https://image.tmdb.org/t/p/w300/b.jpg",
        ],
    }
    assert movies._pick_backdrop(m) is None


def test_backdrop_urls_filters_portrait_and_sorts_by_vote(monkeypatch):
    from app import tmdb

    images_block = {
        "backdrops": [
            {"file_path": "/lo.jpg", "aspect_ratio": 1.78, "vote_average": 1.0},
            {"file_path": "/hi.jpg", "aspect_ratio": 1.78, "vote_average": 9.0},
            # Misfiled portrait poster — should be dropped.
            {"file_path": "/portrait.jpg", "aspect_ratio": 0.66, "vote_average": 7.0},
        ],
    }
    out = tmdb._backdrop_urls(images_block)
    assert out == [
        "https://image.tmdb.org/t/p/w300/hi.jpg",
        "https://image.tmdb.org/t/p/w300/lo.jpg",
    ]


def test_fetch_year_movie_list_persists_backdrops(db_session):
    today = date.today()
    candidates = [{"id": 1}]
    details = {
        1: _fake_detail(
            tmdb_id=1,
            title="With Backdrops",
            rating="PG",
            rd=today + timedelta(days=10),
            backdrops=[
                "https://image.tmdb.org/t/p/w780/a.jpg",
                "https://image.tmdb.org/t/p/w780/b.jpg",
            ],
        ),
    }
    with (
        patch.object(movies.tmdb, "now_playing_and_upcoming", return_value=candidates),
        patch.object(movies.tmdb, "discover_us_theatrical", return_value=[]),
        patch.object(movies.tmdb, "discover_popular_upcoming", return_value=[]),
        patch.object(
            movies.tmdb,
            "fetch_movie_detail",
            side_effect=lambda i, client=None: details[i],
        ),
    ):
        out = movies.fetch_year_movie_list()
    by_title = {m["title"]: m for m in out}
    assert by_title["With Backdrops"]["backdrops"] == [
        "https://image.tmdb.org/t/p/w780/a.jpg",
        "https://image.tmdb.org/t/p/w780/b.jpg",
    ]


def test_trailer_button_skips_invalid_urls():
    today = date(2026, 5, 1)
    item = _make(
        "Bad Trailers",
        "PG",
        today,
        trailers=[
            "https://www.youtube.com/results?search_query=foo",
            "https://youtu.be/abc",
            "https://www.youtube.com/embed/abc",
        ],
    )
    html = movies.render_html_section(
        [item],
        today,
        hidden_titles=set(),
        allowed_ratings={"PG"},
    )
    assert "▶ Trailer" not in html
