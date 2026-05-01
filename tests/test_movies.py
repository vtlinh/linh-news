from __future__ import annotations

from datetime import date, timedelta

from app import movies


def _make(
    title: str,
    rating: str,
    rd: date,
    *,
    summary: str = "Plot.",
    trailers: list[str] | None = None,
) -> dict:
    return {
        "title": title,
        "rating": rating,
        "release_date": rd.isoformat(),
        "status": "in_theaters" if rd <= date(2026, 5, 1) else "upcoming",
        "summary": summary,
        "trailers": trailers or [],
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
            inside_in_theaters, too_old, inside_upcoming, too_far,
            wrong_rating, hidden,
        ],
        today,
        hidden_titles={"Hidden Title"},
        allowed_ratings={"G", "PG"},
    )
    assert [m["title"] for m in in_theaters] == ["Inside Theaters"]
    assert [m["title"] for m in coming_soon] == ["Soon"]


def test_filter_for_edition_dedupes_by_title():
    today = date(2026, 5, 1)
    a = _make("Same Movie", "PG", today - timedelta(days=5))
    b = _make("Same Movie", "PG", today + timedelta(days=5))
    in_theaters, coming_soon = movies.filter_for_edition(
        [a, b], today,
        hidden_titles=set(), allowed_ratings={"PG"},
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
        [stale], today,
        hidden_titles=set(), allowed_ratings={"PG"},
    )
    assert in_theaters == []
    assert coming_soon == []


def test_render_html_section_basic():
    today = date(2026, 5, 1)
    items = [
        _make(
            "Now Showing", "PG", today - timedelta(days=5),
            trailers=["https://www.youtube.com/watch?v=abcdefghijk"],
        ),
        _make(
            "Coming Soon", "PG", today + timedelta(days=20),
            trailers=[
                "https://www.youtube.com/watch?v=11111111111",
                "https://www.youtube.com/watch?v=22222222222",
            ],
        ),
    ]
    html = movies.render_html_section(
        items, today,
        hidden_titles=set(), allowed_ratings={"PG"},
    )
    assert "<section>" in html
    assert "<h2>" in html and "Movies" in html
    assert "Now in theaters" in html
    assert "Coming soon" in html
    assert "Now Showing" in html
    assert "Coming Soon" in html
    assert "In theaters since" in html
    assert "Opens" in html
    assert 'class="hide-movie"' in html
    # Single-trailer renders as a direct anchor
    assert "▶ Trailer" in html
    # Multi-trailer renders the popup
    assert "▶ Trailers" in html
    assert "sources-popup" in html


def test_render_html_section_empty():
    today = date(2026, 5, 1)
    assert movies.render_html_section(
        [], today, hidden_titles=set(), allowed_ratings={"PG"},
    ) == ""


def test_render_pdf_html_basic():
    today = date(2026, 5, 1)
    items = [
        _make("In Theaters", "PG", today - timedelta(days=5)),
        _make("Future", "PG", today + timedelta(days=20)),
    ]
    pdf = movies.render_pdf_html(
        items, today,
        hidden_titles=set(), allowed_ratings={"PG"},
    )
    assert "Movies" in pdf
    assert "In Theaters" in pdf
    assert "Future" in pdf


def test_render_pdf_html_empty_when_no_matches():
    today = date(2026, 5, 1)
    out = movies.render_pdf_html(
        [], today, hidden_titles=set(), allowed_ratings={"PG"},
    )
    assert out == ""


def test_trailer_button_skips_invalid_urls():
    today = date(2026, 5, 1)
    item = _make(
        "Bad Trailers", "PG", today,
        trailers=[
            "https://www.youtube.com/results?search_query=foo",
            "https://youtu.be/abc",
            "https://www.youtube.com/embed/abc",
        ],
    )
    html = movies.render_html_section(
        [item], today,
        hidden_titles=set(), allowed_ratings={"PG"},
    )
    assert "▶ Trailer" not in html
