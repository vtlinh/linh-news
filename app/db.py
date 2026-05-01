from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, LargeBinary, String, Text, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.settings import get_settings


class Base(DeclarativeBase):
    pass


class Edition(Base):
    __tablename__ = "editions"
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    html: Mapped[str] = mapped_column(Text, nullable=False)
    # The print-styled HTML we hand to WeasyPrint. Stored alongside the rendered
    # PDF so we can reproduce / debug image-fetch failures and font sizing
    # without re-running the expensive Claude pipeline.
    pdf_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HiddenMovie(Base):
    __tablename__ = "hidden_movies"
    title: Mapped[str] = mapped_column(String, primary_key=True)
    hidden_until: Mapped[date] = mapped_column(Date, nullable=False)


class HiddenCalendar(Base):
    __tablename__ = "hidden_calendars"
    calendar_id: Mapped[str] = mapped_column(String, primary_key=True)
    calendar_name: Mapped[str] = mapped_column(String, nullable=False)


class SuppressedEvent(Base):
    """Events explicitly marked unimportant — excluded from both HTML and
    PDF generation, keyed by iCalUID."""
    __tablename__ = "suppressed_events"
    ical_uid: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)


class WatchlistStock(Base):
    """Stocks the user wants tracked in the daily edition's stocks section."""
    __tablename__ = "watchlist_stocks"
    symbol: Mapped[str] = mapped_column(String, primary_key=True)


class KvCache(Base):
    """Generic key/value persistence used by app.cache as the local fallback
    when Redis is not configured. Key is the cache key; value is JSON text."""
    __tablename__ = "kv_cache"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class ImportantEvent(Base):
    __tablename__ = "important_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Stable identifier for calendar-sourced events. None for manual entries.
    # Survives recurring instances + edits to title/date/location.
    ical_uid: Mapped[str | None] = mapped_column(String, nullable=True, index=True, unique=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class GoogleOAuth(Base):
    __tablename__ = "google_oauth"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_secret: Mapped[str] = mapped_column(Text, nullable=False)


class CalendarDaySummary(Base):
    __tablename__ = "calendar_day_summaries"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    summary_html: Mapped[str] = mapped_column(Text, nullable=False)
    event_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    events_json: Mapped[str] = mapped_column(Text, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EventEmoji(Base):
    """Cached emoji for a normalized calendar-event title.

    The Python calendar renderer looks up an event's emoji here first; if a
    title is missing we ask Claude (Haiku) for a single emoji once and store
    it. After that, the daily edition is built without any LLM call for
    calendar formatting."""
    __tablename__ = "event_emojis"
    title_norm: Mapped[str] = mapped_column(String, primary_key=True)
    emoji: Mapped[str] = mapped_column(String, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class SessionRow(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _init_engine() -> None:
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True, future=True)
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)


def engine() -> Engine:
    _init_engine()
    assert _engine is not None
    return _engine


def session_factory() -> sessionmaker[Session]:
    _init_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def get_session() -> Iterator[Session]:
    with session_factory()() as s:
        yield s


def set_session_factory(maker: sessionmaker[Session]) -> None:
    """Test hook: install an alternate session factory (e.g., a SQLite one)."""
    global _SessionLocal, _engine
    _SessionLocal = maker
    _engine = maker.kw["bind"] if "bind" in maker.kw else maker.kw.get("bind")
