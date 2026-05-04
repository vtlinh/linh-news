from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    create_engine,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.settings import get_settings

log = logging.getLogger(__name__)


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
    # Weather forecast + alerts captured at generation time so view-time
    # injection can render the weather strip without re-querying NWS for
    # the slowly-changing parts. The 'Now' observation is refreshed
    # separately on each view via the weather_now cache.
    weather_forecast_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    weather_alerts_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Structured LLM response (LinhNews from app.llm_schema). The renderer
    # produces ``html`` from this; keeping it lets us re-render without
    # re-calling Claude. ``NULL`` for legacy editions generated before the
    # structured pipeline.
    content_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class SubsectionImage(Base):
    """Image bytes belonging to a Subsection of an Edition.

    The LLM returns candidate image URLs; the generation pipeline downloads
    one (landscape preferred), resizes to ≤400px wide, and stores the bytes
    here. The viewer references the row by id via ``/edition-image/{id}``.
    """

    __tablename__ = "subsection_images"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    edition_date: Mapped[date] = mapped_column(
        Date,
        ForeignKey("editions.date", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    section_key: Mapped[str] = mapped_column(String, nullable=False)
    subsection_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    bytes_: Mapped[bytes] = mapped_column("bytes", LargeBinary, nullable=False)
    mime_type: Mapped[str] = mapped_column(String, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)


class HiddenMovie(Base):
    __tablename__ = "hidden_movies"
    title: Mapped[str] = mapped_column(String, primary_key=True)
    hidden_until: Mapped[date] = mapped_column(Date, nullable=False)


class FavoriteMovie(Base):
    """Admin-marked must-watch titles. Bypass the MPAA rating filter on the
    admin Movies page (always visible) and force inclusion in the daily
    edition / PDF when their release date falls in the favorite window
    (today - 3 weeks, today + 1 month)."""

    __tablename__ = "favorite_movies"
    title: Mapped[str] = mapped_column(String, primary_key=True)


class Movie(Base):
    """The full TMDB-sourced movie list. Refreshed weekly; filtered to the
    user's allowed MPAA ratings + ``hidden_movies`` overlay at service time
    (admin Movies page, edition HTML injection, PDF generation)."""

    __tablename__ = "movies"
    tmdb_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    release_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    rating: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    trailers: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    poster_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Up to N landscape (16:9) still URLs from TMDB, highest-rated first.
    # The daily edition picks one at random per generation to display above
    # the movie title in both the HTML page and the PDF rail.
    backdrops: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    def to_dict(self) -> dict:
        return {
            "tmdb_id": self.tmdb_id,
            "title": self.title,
            "release_date": self.release_date.isoformat() if self.release_date else "",
            "rating": self.rating or "",
            "status": self.status,
            "summary": self.summary or "",
            "trailers": list(self.trailers or []),
            "poster_url": self.poster_url,
            "backdrops": list(self.backdrops or []),
        }


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
    """Persisted raw calendar events per day, written by the refresh
    pipeline. The HTML summary is rendered inline at view time from
    ``events_json`` plus the ``event_emojis`` map — no cached HTML."""

    __tablename__ = "calendar_day_summaries"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    events_json: Mapped[str] = mapped_column(Text, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WeatherNow(Base):
    """Cached NWS 'Now' observation, keyed by coordinates. Refreshed on view
    when older than 1 hour so the page never blocks on NWS in the common
    case but the displayed temperature stays current."""

    __tablename__ = "weather_now"
    coords: Mapped[str] = mapped_column(String, primary_key=True)
    now_text: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


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
        DateTime(timezone=True),
        nullable=False,
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
        url = get_settings().database_url
        # Postgres connections through the local Fly proxy stall on an
        # optional libpq handshake step for ~130s on cold connect; a short
        # connect_timeout cleanly skips that step and the connection works
        # normally afterwards. libpq enforces a 2-second minimum, so 2 is
        # the lowest useful value. Only applies to the postgres dialect —
        # sqlite used in tests doesn't accept connect_timeout.
        connect_args: dict = {}
        is_pg = url.startswith(("postgres://", "postgresql://", "postgresql+"))
        if is_pg:
            connect_args["connect_timeout"] = 2
        _engine = create_engine(
            url,
            pool_pre_ping=True,
            future=True,
            connect_args=connect_args,
        )
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
        if is_pg:
            _attach_connect_backoff(_engine)


_BACKOFF_BUDGET_SECONDS = 20.0


def _attach_connect_backoff(engine: Engine) -> None:
    """Retry psycopg connect failures with exponential backoff (1s, 2s, 4s,
    8s, …) up to ~20s of cumulative wait, then re-raise.

    The short ``connect_timeout`` is a workaround for a libpq handshake
    stall — under normal conditions it returns a working connection. But
    if the proxy is genuinely down, psycopg raises OperationalError
    immediately. Without backoff, the first request after a brief proxy
    hiccup would surface a hard failure to the user; with backoff we
    ride out short outages while still failing fast on real outages."""
    import time as _time

    from sqlalchemy import event

    @event.listens_for(engine, "do_connect")
    def _retry(dialect, conn_rec, cargs, cparams):  # noqa: ARG001
        delay = 1.0
        elapsed = 0.0
        attempt = 0
        while True:
            attempt += 1
            try:
                return dialect.connect(*cargs, **cparams)
            except Exception as e:  # noqa: BLE001 — DBAPI exception type varies
                if elapsed + delay > _BACKOFF_BUDGET_SECONDS:
                    log.error(
                        "DB connect failed after %d attempts (%.1fs total): %s",
                        attempt,
                        elapsed,
                        e,
                    )
                    raise
                log.warning(
                    "DB connect attempt %d failed (%s) — retrying in %.1fs",
                    attempt,
                    e,
                    delay,
                )
                _time.sleep(delay)
                elapsed += delay
                delay = min(delay * 2, 8.0)


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
