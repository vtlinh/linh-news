from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import HiddenCalendar, HiddenMovie, ImportantEvent


def active_hidden_movie_titles(s: Session, today: date) -> list[str]:
    rows = s.execute(
        select(HiddenMovie).where(HiddenMovie.hidden_until >= today)
    ).scalars().all()
    return [r.title for r in rows]


def hidden_calendar_ids(s: Session) -> list[dict]:
    rows = s.execute(select(HiddenCalendar)).scalars().all()
    return [{"id": r.calendar_id, "name": r.calendar_name} for r in rows]


def important_events_from(s: Session, today: date) -> list[dict]:
    rows = s.execute(
        select(ImportantEvent)
        .where(ImportantEvent.event_date >= today)
        .order_by(ImportantEvent.importance.desc(), ImportantEvent.event_date.asc())
    ).scalars().all()
    return [
        {
            "title": r.title,
            "date": r.event_date.isoformat(),
            "importance": r.importance,
            "notes": r.notes,
        }
        for r in rows
    ]


def hide_movie(s: Session, title: str, days: int = 60) -> None:
    today = date.today()
    existing = s.get(HiddenMovie, title)
    until = today + timedelta(days=days)
    if existing:
        existing.hidden_until = until
    else:
        s.add(HiddenMovie(title=title, hidden_until=until))
    s.commit()


def hide_calendar(s: Session, calendar_id: str, calendar_name: str) -> None:
    if s.get(HiddenCalendar, calendar_id):
        return
    s.add(HiddenCalendar(calendar_id=calendar_id, calendar_name=calendar_name))
    s.commit()


def unhide_calendar(s: Session, calendar_id: str) -> None:
    row = s.get(HiddenCalendar, calendar_id)
    if row:
        s.delete(row)
        s.commit()
