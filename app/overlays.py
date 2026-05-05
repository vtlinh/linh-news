from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import (
    FavoriteMovie,
    HiddenCalendar,
    HiddenMovie,
    ImportantEvent,
    SuppressedEvent,
    WatchlistStock,
)


def active_hidden_movie_titles(s: Session, email: str, today: date) -> list[str]:
    rows = (
        s.execute(
            select(HiddenMovie).where(
                HiddenMovie.email == email,
                HiddenMovie.hidden_until >= today,
            )
        )
        .scalars()
        .all()
    )
    return [r.title for r in rows]


def hidden_calendar_ids(s: Session, email: str) -> list[dict]:
    rows = (
        s.execute(select(HiddenCalendar).where(HiddenCalendar.email == email))
        .scalars()
        .all()
    )
    return [{"id": r.calendar_id, "name": r.calendar_name} for r in rows]


def important_events_from(s: Session, email: str, today: date) -> list[dict]:
    rows = (
        s.execute(
            select(ImportantEvent)
            .where(
                ImportantEvent.email == email,
                ImportantEvent.event_date >= today,
            )
            .order_by(ImportantEvent.importance.desc(), ImportantEvent.event_date.asc())
        )
        .scalars()
        .all()
    )
    return [
        {
            "title": r.title,
            "date": r.event_date.isoformat(),
            "importance": r.importance,
            "notes": r.notes,
        }
        for r in rows
    ]


def hide_movie(s: Session, email: str, title: str, days: int = 60) -> None:
    today = date.today()
    existing = s.get(HiddenMovie, (email, title))
    until = today + timedelta(days=days)
    if existing:
        existing.hidden_until = until
    else:
        s.add(HiddenMovie(email=email, title=title, hidden_until=until))
    s.commit()


def unhide_movie(s: Session, email: str, title: str) -> None:
    row = s.get(HiddenMovie, (email, title))
    if row:
        s.delete(row)
        s.commit()


def all_hidden_movies(s: Session, email: str) -> list[dict]:
    rows = (
        s.execute(
            select(HiddenMovie)
            .where(HiddenMovie.email == email)
            .order_by(HiddenMovie.title)
        )
        .scalars()
        .all()
    )
    return [{"title": r.title, "hidden_until": r.hidden_until.isoformat()} for r in rows]


def favorite_movie_titles(s: Session, email: str) -> set[str]:
    return set(
        s.execute(
            select(FavoriteMovie.title).where(FavoriteMovie.email == email)
        ).scalars()
    )


def favorite_movie(s: Session, email: str, title: str) -> None:
    if not title or s.get(FavoriteMovie, (email, title)):
        return
    s.add(FavoriteMovie(email=email, title=title))
    s.commit()


def unfavorite_movie(s: Session, email: str, title: str) -> None:
    row = s.get(FavoriteMovie, (email, title))
    if row:
        s.delete(row)
        s.commit()


def hide_calendar(s: Session, email: str, calendar_id: str, calendar_name: str) -> None:
    if s.get(HiddenCalendar, (email, calendar_id)):
        return
    s.add(HiddenCalendar(email=email, calendar_id=calendar_id, calendar_name=calendar_name))
    s.commit()


def unhide_calendar(s: Session, email: str, calendar_id: str) -> None:
    row = s.get(HiddenCalendar, (email, calendar_id))
    if row:
        s.delete(row)
        s.commit()


def suppressed_event_uids(s: Session, email: str) -> set[str]:
    return set(
        s.execute(
            select(SuppressedEvent.ical_uid).where(SuppressedEvent.email == email)
        ).scalars()
    )


def suppressed_events_list(s: Session, email: str) -> list[dict]:
    rows = (
        s.execute(select(SuppressedEvent).where(SuppressedEvent.email == email))
        .scalars()
        .all()
    )
    return [{"ical_uid": r.ical_uid, "title": r.title} for r in rows]


def suppress_event(s: Session, email: str, ical_uid: str, title: str) -> None:
    if s.get(SuppressedEvent, (email, ical_uid)):
        return
    s.add(SuppressedEvent(email=email, ical_uid=ical_uid, title=title))
    s.commit()


def unsuppress_event(s: Session, email: str, ical_uid: str) -> None:
    row = s.get(SuppressedEvent, (email, ical_uid))
    if row:
        s.delete(row)
        s.commit()


def watchlist_symbols(s: Session, email: str) -> list[str]:
    rows = (
        s.execute(
            select(WatchlistStock)
            .where(WatchlistStock.email == email)
            .order_by(WatchlistStock.symbol)
        )
        .scalars()
        .all()
    )
    return [r.symbol for r in rows]


def add_watchlist_symbol(s: Session, email: str, symbol: str) -> None:
    sym = symbol.strip().upper()
    if not sym:
        return
    if s.get(WatchlistStock, (email, sym)):
        return
    s.add(WatchlistStock(email=email, symbol=sym))
    s.commit()


def remove_watchlist_symbol(s: Session, email: str, symbol: str) -> None:
    row = s.get(WatchlistStock, (email, symbol.strip().upper()))
    if row:
        s.delete(row)
        s.commit()
