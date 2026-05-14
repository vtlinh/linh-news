from __future__ import annotations

import logging
import secrets
from collections.abc import Iterator
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy import event as _sa_event
from sqlalchemy import func as _sa_func
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.settings import get_settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class Edition(Base):
    __tablename__ = "editions"
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    # Owner of this rendering. The same calendar date can have multiple
    # rows — one per user with personalization enabled — so the calendar /
    # watchlist / overlays in each row reflect that user.
    email: Mapped[str] = mapped_column(Text, primary_key=True)
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
    # Cached PDF side rail. Shape:
    #   {"version": int, "calendar_html": str, "movies_html": str}
    # Re-runs for the same date reuse this when ``version`` matches the
    # current ``app.pdf_renderer.PDF_RAIL_VERSION`` — this skips the
    # movie-backdrop downloads and the calendar/movies HTML build.
    pdf_rail_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class DebugEdition(Base):
    """Debug-retention copy of ``content_json`` for runs whose PDF render
    failed. Written immediately after the LLM returns a valid structured
    response; deleted when the same run later upserts a real ``editions``
    row. Anything left here is from a failed run and self-expires after
    ``expires_at``."""

    __tablename__ = "debug_editions"
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    email: Mapped[str] = mapped_column(Text, primary_key=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    content_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class SubsectionImage(Base):
    """Image bytes belonging to a Subsection of an Edition.

    The LLM returns candidate image URLs; the generation pipeline downloads
    one (landscape preferred), resizes to ≤400px wide, and stores the bytes
    here. The viewer references the row by id via ``/edition-image/{id}``.
    """

    __tablename__ = "subsection_images"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    edition_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    edition_email: Mapped[str] = mapped_column(Text, nullable=False)
    section_key: Mapped[str] = mapped_column(String, nullable=False)
    subsection_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    bytes_: Mapped[bytes] = mapped_column("bytes", LargeBinary, nullable=False)
    mime_type: Mapped[str] = mapped_column(String, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    # SHA-256 of the raw downloaded bytes. Used to reject images that already
    # appeared in a different edition (e.g. site banners served as og:image).
    image_hash: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["edition_date", "edition_email"],
            ["editions.date", "editions.email"],
            name="subsection_images_edition_fkey",
            ondelete="CASCADE",
        ),
    )


class HiddenMovie(Base):
    __tablename__ = "hidden_movies"
    email: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(String, primary_key=True)
    hidden_until: Mapped[date] = mapped_column(Date, nullable=False)


class FavoriteMovie(Base):
    """Per-user must-watch titles. Bypass the MPAA rating filter on the
    admin Movies page (always visible) and force inclusion in the daily
    edition / PDF when their release date falls in the favorite window
    (today - 3 weeks, today + 1 month)."""

    __tablename__ = "favorite_movies"
    email: Mapped[str] = mapped_column(Text, primary_key=True)
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
    email: Mapped[str] = mapped_column(Text, primary_key=True)
    calendar_id: Mapped[str] = mapped_column(String, primary_key=True)
    calendar_name: Mapped[str] = mapped_column(String, nullable=False)


class SuppressedEvent(Base):
    """Per-user events explicitly marked unimportant — excluded from both
    HTML and PDF generation, keyed by (email, iCalUID)."""

    __tablename__ = "suppressed_events"
    email: Mapped[str] = mapped_column(Text, primary_key=True)
    ical_uid: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)


class WatchlistStock(Base):
    """Per-user stocks tracked in the daily edition's stocks section."""

    __tablename__ = "watchlist_stocks"
    email: Mapped[str] = mapped_column(Text, primary_key=True)
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
    email: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    # Stable identifier for calendar-sourced events. None for manual entries.
    # Survives recurring instances + edits to title/date/location.
    ical_uid: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("email", "ical_uid", name="uq_important_events_email_ical_uid"),
    )


class GoogleCalendar(Base):
    """Cached snapshot of the user's Google Calendar list. Refreshed on
    demand (no live polling). Used by the Data tab to show a calendar
    picker without hitting the Google API on every request."""

    __tablename__ = "google_calendars"
    email: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GoogleOAuth(Base):
    __tablename__ = "google_oauth"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Google account email this credential belongs to. Unique — only one
    # row per account is ever needed.
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_secret: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Timestamp of the most recent successful per-user generation. NULL until
    # the first run. Surfaced on the admin Users page.
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set when Google rejects this refresh_token with invalid_grant (user
    # revoked access, password change, 6-month inactivity, …). While set,
    # the row is treated as if it were absent: require_viewer bounces the
    # user to re-consent and the cron skips them. Cleared on the next
    # successful sign-in that captures a fresh refresh token.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SharedEdition(Base):
    """Phase A LLM output for a date — the shared parts of the edition
    minus per-user customization. Phase B reads this once per user and
    assembles the per-user edition without another LLM call."""

    __tablename__ = "shared_editions"
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    content_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CalendarDaySummary(Base):
    """Persisted raw calendar events per (user, day), written by the refresh
    pipeline. The HTML summary is rendered inline at view time from
    ``events_json`` plus the ``event_emojis`` map — no cached HTML."""

    __tablename__ = "calendar_day_summaries"
    email: Mapped[str] = mapped_column(Text, primary_key=True)
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


class WeatherGrid(Base):
    """Cached ``/points/{lat},{lon}`` → NWS grid identifier lookup.

    Stored so every subsequent generation can hit
    ``/gridpoints/{grid_id}/{grid_x},{grid_y}/forecast/hourly`` directly,
    skipping the per-run resolution roundtrip."""

    __tablename__ = "weather_grid"
    coords: Mapped[str] = mapped_column(String, primary_key=True)
    grid_id: Mapped[str] = mapped_column(String, nullable=False)
    grid_x: Mapped[int] = mapped_column(Integer, nullable=False)
    grid_y: Mapped[int] = mapped_column(Integer, nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class WeatherHourly(Base):
    """One hour of NWS hourly forecast, keyed by ``(coords, start_at)``.

    Each generation upserts ~156 future hours from the NWS hourly endpoint
    and prunes rows older than 14 days. This rolling cache lets the strip
    renderer reconstruct any day within roughly ±7 days from the DB alone,
    without an extra NWS call. Temperatures are normalised to integer
    Celsius at write time (NWS hourly returns °F)."""

    __tablename__ = "weather_hourly"
    coords: Mapped[str] = mapped_column(String, primary_key=True)
    start_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    temp_c: Mapped[int] = mapped_column(Integer, nullable=False)
    short_forecast: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class WeatherPhrase(Base):
    """A hand-written prose clause used to render today/tomorrow weather as
    newspaper-style text. Twenty variants exist per (period, bucket); the
    renderer picks one of each at generation time and concatenates them.

    ``text`` carries ``{h}`` / ``{l}`` slots filled with high/low
    temperatures (Celsius int) and ``<b>Today</b>`` / ``<b>Tomorrow</b>`` /
    ``<b>Tonight</b>`` already wrapped in bold tags."""

    __tablename__ = "weather_phrases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    period: Mapped[str] = mapped_column(String, nullable=False)
    bucket: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_weather_phrases_period_bucket", "period", "bucket"),)


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


class UserSettings(Base):
    """Per-user newspaper configuration. Drives the section list, masthead
    name (``The {display_name} News``), and weather coords for that user's
    edition. ``sections_json`` is an ordered list (ordering = priority);
    each entry: ``{key, title, description, subsection_count,
    preferred_sources: list[str], use_global_sources: bool}``."""

    __tablename__ = "user_settings"
    email: Mapped[str] = mapped_column(String, primary_key=True)
    # Stable, human-friendly numeric handle (1, 2, 3, …). Used in the
    # admin Users table and in the /pdf/{day}/{name} & /d/{day}/{name}
    # routes when display names collide.
    user_id: Mapped[int] = mapped_column(
        Integer, Identity(start=1), nullable=False, unique=True, autoincrement=True
    )
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    address: Mapped[str | None] = mapped_column(String, nullable=True)
    # "lat,lon" — resolved from ``address`` on save via Nominatim.
    weather_coords: Mapped[str | None] = mapped_column(String, nullable=True)
    # Preferred temperature unit for weather display: "C" (default) or "F".
    # Applied at render time — the underlying NWS data is always fetched
    # in Celsius; the renderer converts to °F when this is "F".
    temperature_unit: Mapped[str] = mapped_column(
        String, nullable=False, default="F", server_default="F"
    )
    sections_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # List of ``{"name": str, "birthday": "YYYY-MM-DD"}``. Drives kid-age /
    # grade computation that feeds the prompt's school grade-filter rule.
    children_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Admin-controlled toggle. When false, this user sees the admin's shared
    # "Linh News" edition. When true, cron generates a personalized edition
    # for them and / serves it.
    personalized_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Secret bearer token granting unauthenticated access to this user's
    # PDF editions via /pdf/{day}/{email}?token=…. Auto-minted on user
    # creation; admin can rotate from the Users page.
    pdf_token: Mapped[str] = mapped_column(
        String, nullable=False, unique=True, default=lambda: secrets.token_urlsafe(36)
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SessionRow(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


@_sa_event.listens_for(UserSettings, "before_insert")
def _assign_user_id(_mapper, connection, target: UserSettings) -> None:  # noqa: ARG001
    """Backends without a true Identity column (SQLite in tests) need a
    Python-side default. ORM bulk-inserts fire ``before_insert`` for every
    row before issuing any INSERT, so a naive ``max(user_id)+1`` query
    would hand the same id to every row in the batch. Cache the
    high-water-mark on the connection and increment it in-Python."""
    if target.user_id is not None:
        return
    if connection.dialect.name == "postgresql":
        # Postgres GENERATED IDENTITY assigns the value on INSERT — leave NULL
        # and let the DB fill it in.
        return
    next_id = connection.info.get("_user_settings_next_id")
    if next_id is None:
        current = connection.execute(
            _sa_func.coalesce(_sa_func.max(UserSettings.user_id), 0).select()
        ).scalar_one()
        next_id = int(current) + 1
    target.user_id = next_id
    connection.info["_user_settings_next_id"] = next_id + 1


@_sa_event.listens_for(Session, "after_flush")
def _clear_user_id_counter(session: Session, _flush_context) -> None:  # noqa: ARG001
    """Drop the cached id counter once the flush that allocated them ends,
    so the next flush re-reads ``max(user_id)`` from the table (now
    including the rows we just inserted)."""
    conn = session.connection()
    conn.info.pop("_user_settings_next_id", None)


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
