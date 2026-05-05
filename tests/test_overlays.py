from __future__ import annotations

from datetime import date, timedelta

from app.db import FavoriteMovie, HiddenMovie, ImportantEvent
from app.overlays import (
    active_hidden_movie_titles,
    favorite_movie,
    favorite_movie_titles,
    hide_calendar,
    hide_movie,
    important_events_from,
    unfavorite_movie,
    unhide_calendar,
)

EMAIL = "vtlinh87@gmail.com"


def test_active_hidden_movie_titles_filters_expired(db_session):
    today = date.today()
    db_session.add_all(
        [
            HiddenMovie(email=EMAIL, title="Active", hidden_until=today + timedelta(days=10)),
            HiddenMovie(email=EMAIL, title="Expired", hidden_until=today - timedelta(days=1)),
        ]
    )
    db_session.commit()
    titles = active_hidden_movie_titles(db_session, EMAIL, today)
    assert titles == ["Active"]


def test_hide_movie_inserts_and_extends(db_session):
    today = date.today()
    hide_movie(db_session, EMAIL, "Foo", days=10)
    row = db_session.get(HiddenMovie, (EMAIL, "Foo"))
    assert row.hidden_until == today + timedelta(days=10)
    hide_movie(db_session, EMAIL, "Foo", days=30)
    row = db_session.get(HiddenMovie, (EMAIL, "Foo"))
    assert row.hidden_until == today + timedelta(days=30)


def test_favorite_toggle_round_trip(db_session):
    assert favorite_movie_titles(db_session, EMAIL) == set()
    favorite_movie(db_session, EMAIL, "Angry Birds 3")
    favorite_movie(db_session, EMAIL, "Angry Birds 3")  # idempotent
    favorite_movie(db_session, EMAIL, "Toy Story 5")
    assert favorite_movie_titles(db_session, EMAIL) == {"Angry Birds 3", "Toy Story 5"}
    unfavorite_movie(db_session, EMAIL, "Angry Birds 3")
    unfavorite_movie(db_session, EMAIL, "missing")  # no error
    assert favorite_movie_titles(db_session, EMAIL) == {"Toy Story 5"}
    # Underlying row is gone, not just hidden.
    assert db_session.get(FavoriteMovie, (EMAIL, "Angry Birds 3")) is None


def test_hide_and_unhide_calendar(db_session):
    hide_calendar(db_session, EMAIL, "cal-1", "Family")
    hide_calendar(db_session, EMAIL, "cal-1", "Family")  # idempotent
    unhide_calendar(db_session, EMAIL, "cal-1")
    unhide_calendar(db_session, EMAIL, "missing")  # no error


def test_important_events_sorted_by_importance_then_date(db_session):
    today = date.today()
    db_session.add_all(
        [
            ImportantEvent(
                email=EMAIL,
                title="Birthday",
                event_date=today + timedelta(days=20),
                importance=10,
            ),
            ImportantEvent(
                email=EMAIL,
                title="Random",
                event_date=today + timedelta(days=2),
                importance=3,
            ),
            ImportantEvent(
                email=EMAIL,
                title="Past",
                event_date=today - timedelta(days=1),
                importance=10,
            ),
        ]
    )
    db_session.commit()
    out = important_events_from(db_session, EMAIL, today)
    titles = [e["title"] for e in out]
    assert "Past" not in titles
    assert titles[0] == "Birthday"  # higher importance first
    assert titles[-1] == "Random"
