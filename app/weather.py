"""Weather is sourced entirely from the NWS (National Weather Service) API.

NWS is free, no API key required, and authoritative for US locations. We
fetch the current observation, today/tomorrow forecast highs/lows, and
active alerts here. The LLM has no role in weather generation.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

_UA = "Linh-News/1.0 (vtlinh87+linhnews@gmail.com)"

# Regex over rendered HTML / plain text to find integer Celsius readings
# emitted by ``build_weather_strip`` / ``weather_prose`` / NWS now-cache.
# Captures the signed integer so we can convert in-place to Fahrenheit
# at the display boundary.
_CELSIUS_NUMBER_RE = re.compile(r"(-?\d+)°C")
_CELSIUS_BARE_DEGREE_RE = re.compile(r"(-?\d+)°(?!C|F)")


def convert_celsius_html(html: str, unit: str) -> str:
    """Convert every integer °C reading in ``html`` to °F when ``unit``
    is ``"F"`` (case-insensitive). When ``unit`` is anything else, return
    the input unchanged.

    Catches two forms: ``"12°C"`` (the cached "Now" string and the prose
    paragraph use this) and the bare-degree form ``"H 14°"`` /
    ``"L 7°"`` emitted by the weather strip — those carry an implicit
    Celsius unit and need conversion too. The bare-degree regex is
    deliberately written to not double-convert a degree marker that's
    already followed by ``C`` or ``F``.
    """
    if not html or (unit or "").upper() != "F":
        return html

    def _to_f(m: re.Match) -> str:
        c = int(m.group(1))
        return f"{round(c * 9 / 5 + 32)}°F"

    html = _CELSIUS_NUMBER_RE.sub(_to_f, html)

    def _bare_to_f(m: re.Match) -> str:
        c = int(m.group(1))
        return f"{round(c * 9 / 5 + 32)}°"

    return _CELSIUS_BARE_DEGREE_RE.sub(_bare_to_f, html)


_TIMEOUT = 8  # seconds per request

_CONDITION_EMOJI: list[tuple[str, str]] = [
    ("thunderstorm", "⛈"),
    ("lightning", "⛈"),
    ("tornado", "🌪"),
    ("hurricane", "🌀"),
    ("snow", "❄️"),
    ("blizzard", "❄️"),
    ("sleet", "🌨"),
    ("freezing", "🧊"),
    ("ice", "🧊"),
    ("hail", "🌨"),
    ("drizzle", "🌦"),
    ("shower", "🌦"),
    ("rain", "🌧"),
    ("fog", "🌫"),
    ("haze", "🌫"),
    ("smoke", "🌫"),
    ("dust", "🌫"),
    ("sand", "🌫"),
    ("overcast", "☁️"),
    ("mostly cloudy", "🌥"),
    ("cloudy", "☁️"),
    ("partly cloudy", "⛅"),
    ("partly sunny", "⛅"),
    ("mostly clear", "🌤"),
    ("mostly sunny", "🌤"),
    ("clear", "☀️"),
    ("sunny", "☀️"),
    ("fair", "☀️"),
    ("windy", "💨"),
    ("breezy", "💨"),
]


def _emoji(desc: str) -> str:
    d = desc.lower()
    for kw, em in _CONDITION_EMOJI:
        if kw in d:
            return em
    return "🌡"


# Bucket codes used by the weather_phrases table. Keyword order matters —
# the first hit wins, so more specific buckets ("thunderstorm", "snow")
# come before broader ones ("rain", "cloudy").
_CONDITION_BUCKETS: list[tuple[str, str]] = [
    ("thunderstorm", "thunderstorm"),
    ("lightning", "thunderstorm"),
    ("tornado", "thunderstorm"),
    ("hurricane", "thunderstorm"),
    ("snow", "snow"),
    ("blizzard", "snow"),
    ("sleet", "snow"),
    ("freezing", "snow"),
    ("ice", "snow"),
    ("hail", "snow"),
    ("fog", "fog"),
    ("haze", "fog"),
    ("smoke", "fog"),
    ("dust", "fog"),
    ("sand", "fog"),
    ("drizzle", "rain"),
    ("shower", "rain"),
    ("rain", "rain"),
    ("windy", "windy"),
    ("breezy", "windy"),
    # Cloudy variants — order: most specific first.
    ("partly cloudy", "partly_cloudy"),
    ("partly sunny", "partly_cloudy"),
    ("mostly cloudy", "cloudy"),
    ("overcast", "cloudy"),
    ("cloudy", "cloudy"),
    ("mostly clear", "mostly_sunny"),
    ("mostly sunny", "mostly_sunny"),
    ("sunny", "sunny"),
    ("clear", "sunny"),
    ("fair", "sunny"),
]


def bucket(short_forecast: str) -> str:
    """Map an NWS shortForecast / textDescription string to a phrase bucket.

    Returns one of: ``sunny``, ``mostly_sunny``, ``partly_cloudy``,
    ``cloudy``, ``rain``, ``thunderstorm``, ``snow``, ``fog``, ``windy``.
    Falls back to ``cloudy`` when nothing matches — the most generic neutral
    phrasing."""
    d = (short_forecast or "").lower()
    for kw, b in _CONDITION_BUCKETS:
        if kw in d:
            return b
    return "cloudy"


def _wind_dir(degrees: float) -> str:
    dirs = [
        "N",
        "NNE",
        "NE",
        "ENE",
        "E",
        "ESE",
        "SE",
        "SSE",
        "S",
        "SSW",
        "SW",
        "WSW",
        "W",
        "WNW",
        "NW",
        "NNW",
    ]
    return dirs[round(degrees / 22.5) % 16]


def _get_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _UA, "Accept": "application/geo+json"},
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:  # noqa: S310
        return json.loads(r.read())


def fetch_current_now(lat_lon: str) -> str:
    """Return a formatted 'Now' string from NWS latest observation.

    Format: '12°C ⛅ · Wind NW 10 mph'
    Returns '' on any error — the caller falls back to the previously
    cached value (see ``get_now_cached``).
    """
    try:
        lat, lon = [p.strip() for p in lat_lon.split(",", 1)]

        # 1. Resolve NWS grid point → observationStations URL.
        point = _get_json(f"https://api.weather.gov/points/{lat},{lon}")
        stations_url = point["properties"]["observationStations"]

        # 2. Pick the nearest station.
        stations = _get_json(stations_url)
        features = stations.get("features", [])
        if not features:
            log.warning("NWS: no observation stations near %s,%s", lat, lon)
            return ""
        station_id = features[0]["properties"]["stationIdentifier"]

        # 3. Fetch latest observation.
        obs = _get_json(f"https://api.weather.gov/stations/{station_id}/observations/latest")
        props = obs["properties"]

        temp_raw = (props.get("temperature") or {}).get("value")
        if temp_raw is None:
            log.warning("NWS: temperature missing in observation from %s", station_id)
            return ""
        temp_c = round(float(temp_raw))

        desc = props.get("textDescription") or ""
        em = _emoji(desc)

        wind_ms = (props.get("windSpeed") or {}).get("value") or 0.0
        wind_deg = (props.get("windDirection") or {}).get("value")
        wind_part = ""
        if float(wind_ms) > 0.9:
            wind_mph = round(float(wind_ms) * 2.237)
            if wind_deg is not None:
                wind_part = f" · Wind {_wind_dir(float(wind_deg))} {wind_mph} mph"
            else:
                wind_part = f" · Wind {wind_mph} mph"

        result = f"{temp_c}°C {em}{wind_part}"
        log.info("NWS now: %s (station %s)", result, station_id)
        return result

    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        OSError,
        KeyError,
        ValueError,
        json.JSONDecodeError,
    ) as e:
        log.warning("NWS current-conditions fetch failed: %s", e)
        return ""


# ───────────────── Hourly forecast (grid cache + summarize) ─────────────────


# Bucket precedence used to pick a single dominant condition for a multi-hour
# daytime window. Most newsworthy first — a day with two hours of
# thunderstorms and fourteen hours of partly cloudy reads as "thunderstorms".
_BUCKET_PRIORITY: tuple[str, ...] = (
    "thunderstorm",
    "snow",
    "rain",
    "fog",
    "windy",
    "cloudy",
    "partly_cloudy",
    "mostly_sunny",
    "sunny",
)

# Severe buckets that earn an overnight clause when seen in the 10 PM–7 AM
# window. Must stay in sync with ``app.weather_prose.NIGHT_BUCKETS``.
_NIGHT_SEVERE_BUCKETS: frozenset[str] = frozenset({"thunderstorm", "snow"})

# Bucket → display emoji, used for the dominant-condition emoji on a
# summarized day. Distinct from ``_CONDITION_EMOJI`` (which keys on the raw
# NWS shortForecast string).
_BUCKET_EMOJI: dict[str, str] = {
    "thunderstorm": "⛈",
    "snow": "❄️",
    "rain": "🌧",
    "fog": "🌫",
    "windy": "💨",
    "cloudy": "☁️",
    "partly_cloudy": "⛅",
    "mostly_sunny": "🌤",
    "sunny": "☀️",
}

# Daytime window expressed in local hours: 7 AM (inclusive) – 10 PM (exclusive).
# The 10 PM hour itself is the first hour of the overnight window.
_DAY_START_HOUR = 7
_DAY_END_HOUR_EXCL = 22

# How many days of past hourly data we retain in ``weather_hourly``. Anything
# older is pruned on each refresh. ±7 days of look-back is the user-visible
# guarantee; 14 leaves slack for clock skew and missed runs.
_HOURLY_RETENTION_DAYS = 14


def _to_celsius(value: float, unit: str) -> float:
    """NWS may return F or C depending on `units`. Normalise to C."""
    u = (unit or "").lower()
    if u in ("f", "fahrenheit", "wmounit:degf", "wmounit:degree_(fahrenheit)"):
        return (float(value) - 32) * 5 / 9
    return float(value)


def _dominant_bucket(short_forecasts: list[str]) -> str:
    """Pick the most newsworthy bucket across a set of hourly shortForecast
    strings. Falls back to ``"cloudy"`` (the default in :func:`bucket`) when
    the input list is empty."""
    seen = {bucket(s) for s in short_forecasts if s}
    for b in _BUCKET_PRIORITY:
        if b in seen:
            return b
    return "cloudy"


def resolve_grid(s: Session, coords: str) -> tuple[str, int, int] | None:
    """Return ``(grid_id, grid_x, grid_y)`` for ``coords`` — cached forever
    in ``weather_grid`` once resolved.

    Subsequent generations skip the ``/points`` lookup entirely. Returns
    ``None`` if NWS refuses the resolution (network error, coords outside
    NWS coverage); callers should treat that as "no hourly data available"."""
    from app.db import WeatherGrid

    row = s.get(WeatherGrid, coords)
    if row is not None:
        return row.grid_id, row.grid_x, row.grid_y

    try:
        lat, lon = [p.strip() for p in coords.split(",", 1)]
        point = _get_json(f"https://api.weather.gov/points/{lat},{lon}")
        props = point["properties"]
        grid_id = props["gridId"]
        grid_x = int(props["gridX"])
        grid_y = int(props["gridY"])
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        OSError,
        KeyError,
        ValueError,
        json.JSONDecodeError,
    ) as e:
        log.warning("NWS grid lookup failed for %s: %s", coords, e)
        return None

    s.add(
        WeatherGrid(
            coords=coords,
            grid_id=grid_id,
            grid_x=grid_x,
            grid_y=grid_y,
            resolved_at=datetime.now(UTC),
        )
    )
    s.commit()
    return grid_id, grid_x, grid_y


def fetch_hourly_forecast(grid_id: str, grid_x: int, grid_y: int) -> list[dict]:
    """Fetch the hourly forecast from ``/gridpoints/{id}/{x},{y}/forecast/hourly``.

    Returns a list of ``{"start_at": datetime(UTC), "temp_c": int,
    "short_forecast": str}``. Returns ``[]`` on any error so the caller
    can fall back to whatever is already cached in ``weather_hourly``.
    """
    url = f"https://api.weather.gov/gridpoints/{grid_id}/{grid_x},{grid_y}/forecast/hourly"
    try:
        data = _get_json(url)
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        OSError,
        KeyError,
        ValueError,
        json.JSONDecodeError,
    ) as e:
        log.warning("NWS hourly fetch failed for %s/%d,%d: %s", grid_id, grid_x, grid_y, e)
        return []

    out: list[dict] = []
    for p in data.get("properties", {}).get("periods", []) or []:
        try:
            start_raw = p["startTime"]
            start_at = datetime.fromisoformat(start_raw.replace("Z", "+00:00")).astimezone(UTC)
            temp_c = round(_to_celsius(p["temperature"], p.get("temperatureUnit", "C")))
            short = (p.get("shortForecast") or "").strip()
        except (KeyError, ValueError, TypeError):
            continue
        out.append({"start_at": start_at, "temp_c": temp_c, "short_forecast": short})
    return out


def cache_hourly_forecast(s: Session, coords: str, periods: list[dict]) -> int:
    """Upsert ``periods`` (output of :func:`fetch_hourly_forecast`) into
    ``weather_hourly`` and prune rows older than the retention window.

    Returns the number of rows written. Idempotent: re-running with the
    same periods re-stamps ``fetched_at`` but does not duplicate rows."""
    from app.db import WeatherHourly

    if not periods:
        # Still prune even when the fetch failed — keeps the table bounded
        # even during long NWS outages.
        _prune_hourly(s, coords)
        return 0

    now = datetime.now(UTC)
    written = 0
    for p in periods:
        existing = s.get(WeatherHourly, (coords, p["start_at"]))
        if existing is None:
            s.add(
                WeatherHourly(
                    coords=coords,
                    start_at=p["start_at"],
                    temp_c=p["temp_c"],
                    short_forecast=p["short_forecast"],
                    fetched_at=now,
                )
            )
        else:
            existing.temp_c = p["temp_c"]
            existing.short_forecast = p["short_forecast"]
            existing.fetched_at = now
        written += 1

    _prune_hourly(s, coords)
    s.commit()
    return written


def _prune_hourly(s: Session, coords: str) -> None:
    """Delete rows older than ``_HOURLY_RETENTION_DAYS`` for ``coords``."""
    from app.db import WeatherHourly

    cutoff = datetime.now(UTC) - timedelta(days=_HOURLY_RETENTION_DAYS)
    s.execute(
        delete(WeatherHourly).where(
            WeatherHourly.coords == coords,
            WeatherHourly.start_at < cutoff,
        ),
        execution_options={"synchronize_session": False},
    )


def _local_tz():
    """Return the configured local timezone — imported lazily to avoid a
    circular import via ``app.settings``."""
    from app.settings import LOCAL_TZ

    return LOCAL_TZ


def _day_window_utc(local_day: date) -> tuple[datetime, datetime]:
    """UTC ``[start, end)`` interval covering the 7 AM–10 PM local-time
    daytime window for ``local_day``."""
    tz = _local_tz()
    start = datetime.combine(local_day, time(_DAY_START_HOUR, 0), tzinfo=tz)
    end = datetime.combine(local_day, time(_DAY_END_HOUR_EXCL, 0), tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def _night_window_utc(local_day: date) -> tuple[datetime, datetime]:
    """UTC ``[start, end)`` interval covering the 10 PM–7 AM local-time
    overnight window that follows ``local_day``."""
    tz = _local_tz()
    start = datetime.combine(local_day, time(_DAY_END_HOUR_EXCL, 0), tzinfo=tz)
    end = datetime.combine(local_day + timedelta(days=1), time(_DAY_START_HOUR, 0), tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def _hours_in(s: Session, coords: str, start: datetime, end: datetime) -> list:
    """Return ``WeatherHourly`` rows for ``coords`` whose ``start_at`` is in
    ``[start, end)``, ordered by time."""
    from app.db import WeatherHourly

    return list(
        s.execute(
            select(WeatherHourly)
            .where(
                WeatherHourly.coords == coords,
                WeatherHourly.start_at >= start,
                WeatherHourly.start_at < end,
            )
            .order_by(WeatherHourly.start_at.asc())
        ).scalars()
    )


def summarize_forecast(s: Session, coords: str, today: date) -> dict:
    """Build the today + tomorrow summary dict consumed by
    :func:`build_weather_strip` and :func:`app.weather_prose.render_prose_html`.

    Reads exclusively from the ``weather_hourly`` cache, so it can rebuild a
    past day's strip after the fact provided the rows were upserted during
    that day's generation run. Returns ``{}`` when nothing is cached.

    Shape (all keys optional — present only when data exists)::

        {
            "today_h", "today_l", "today_em", "today_short",
            "tomorrow_h", "tomorrow_l", "tomorrow_em", "tomorrow_short",
            "today_night_l", "today_night_severe",
            "tomorrow_night_l", "tomorrow_night_severe",
        }
    """
    out: dict = {}
    for label, day in (("today", today), ("tomorrow", today + timedelta(days=1))):
        day_rows = _hours_in(s, coords, *_day_window_utc(day))
        if day_rows:
            temps = [r.temp_c for r in day_rows]
            shorts = [r.short_forecast for r in day_rows]
            bkt = _dominant_bucket(shorts)
            rep_short = next(
                (sf for sf in shorts if bucket(sf) == bkt),
                shorts[0],
            )
            out[f"{label}_h"] = max(temps)
            out[f"{label}_l"] = min(temps)
            out[f"{label}_em"] = _BUCKET_EMOJI[bkt]
            out[f"{label}_short"] = rep_short

        night_rows = _hours_in(s, coords, *_night_window_utc(day))
        if night_rows:
            out[f"{label}_night_l"] = min(r.temp_c for r in night_rows)
            severe = next(
                (
                    bucket(r.short_forecast)
                    for r in night_rows
                    if bucket(r.short_forecast) in _NIGHT_SEVERE_BUCKETS
                ),
                None,
            )
            if severe is not None:
                out[f"{label}_night_severe"] = severe

    return out


def refresh_and_summarize(s: Session, coords: str, today: date) -> dict:
    """One-call entry point used by the generation pipeline.

    Resolves (and caches) the NWS grid, fetches the hourly forecast,
    upserts it into ``weather_hourly``, then returns the today/tomorrow
    summary. Tolerates partial failure: if the network is down but cached
    rows exist for the requested days, the summary still renders."""
    grid = resolve_grid(s, coords)
    if grid is not None:
        grid_id, grid_x, grid_y = grid
        periods = fetch_hourly_forecast(grid_id, grid_x, grid_y)
        cache_hourly_forecast(s, coords, periods)
    return summarize_forecast(s, coords, today)


# ───────────────── Active alerts (NWS) ─────────────────


def fetch_alerts(lat_lon: str) -> list[str]:
    """Return short headlines for active NWS alerts at the given point.

    Each entry looks like ``"Wind Advisory until 6 PM"``. Returns ``[]`` on
    any error or when no alerts are active.
    """
    try:
        lat, lon = [p.strip() for p in lat_lon.split(",", 1)]
        url = "https://api.weather.gov/alerts/active?" + urllib.parse.urlencode(
            {"point": f"{lat},{lon}"}
        )
        data = _get_json(url)
        out: list[str] = []
        for feat in data.get("features", []):
            props = feat.get("properties") or {}
            event = (props.get("event") or "").strip()
            if not event:
                continue
            ends = props.get("ends") or props.get("expires") or ""
            suffix = ""
            if ends:
                try:
                    dt = datetime.fromisoformat(ends.replace("Z", "+00:00"))
                    h = dt.hour % 12 or 12
                    suffix = f" until {h} {'AM' if dt.hour < 12 else 'PM'}"
                except ValueError:
                    pass
            out.append(f"{event}{suffix}")
        return out
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        OSError,
        KeyError,
        ValueError,
        json.JSONDecodeError,
    ) as e:
        log.warning("NWS alerts fetch failed: %s", e)
        return []


# ───────────────── 'Now' DB cache ─────────────────


def get_now_cached(
    s: Session,
    coords: str,
    max_age: timedelta = timedelta(hours=1),
) -> str:
    """Return the cached 'Now' string, refreshing if older than ``max_age``.

    On NWS failure: keep returning the stale cached value rather than blank
    (better than nothing). With no cached value at all, return ``""``.
    """
    from app.db import WeatherNow

    now = datetime.now(UTC)
    row = s.get(WeatherNow, coords)
    if row is not None:
        observed = row.observed_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        if now - observed < max_age:
            return row.now_text

    fresh = fetch_current_now(coords)
    if not fresh:
        # Fallback: stale cache is better than empty.
        return row.now_text if row is not None else ""

    if row is None:
        s.add(WeatherNow(coords=coords, now_text=fresh, observed_at=now))
    else:
        row.now_text = fresh
        row.observed_at = now
    s.commit()
    return fresh


# ───────────────── Strip renderer ─────────────────


def build_refreshed_span(refreshed_at: datetime | None) -> str:
    """Render the ``REFRESHED AT HH:MM TZ`` badge as an inline span.

    Returns ``""`` when no timestamp is provided. Shared between the live
    one-line strip and the prose-paragraph layout."""
    if refreshed_at is None:
        return ""
    from app.settings import LOCAL_TZ

    ts = refreshed_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    ts = ts.astimezone(LOCAL_TZ)
    tz_abbrev = ts.tzname() or "EST"
    return (
        f'<span class="weather-refreshed">Refreshed at '
        f"{ts.hour:02d}:{ts.minute:02d} {tz_abbrev}</span>"
    )


def build_weather_strip(
    now: str,
    forecast: dict,
    alerts: list[str],
    refreshed_at: datetime | None = None,
) -> str:
    """Assemble the one-line weather strip HTML.

    Format::
        <div class="weather-strip">
          <span class="weather-main">Now 12°C 🌤 · Today H 14° / L 7° ☀️
            · Tomorrow H 16° / L 9° ☁️ · ⚠ Wind advisory until 6 PM</span>
          <span class="weather-refreshed">Refreshed at 14:00 EDT</span>
        </div>

    The CSS pins ``.weather-refreshed`` to the right edge of the row,
    mirroring the masthead-corner refreshed label in the PDF.
    """

    # Threshold for showing the strip's "L overnight N°" tail — same rule as
    # the prose paragraph (evaluated in Celsius regardless of display unit).
    NIGHT_TAIL_THRESHOLD_C = 5

    def _period_parts(label: str, key: str) -> list[str]:
        h = forecast.get(f"{key}_h")
        low = forecast.get(f"{key}_l")
        em = forecast.get(f"{key}_em", "")
        night_l = forecast.get(f"{key}_night_l")
        night_severe = forecast.get(f"{key}_night_severe")

        out: list[str] = []
        if h is not None or low is not None:
            bits = [label]
            if h is not None:
                bits.append(f"H {h}°")
            if low is not None:
                bits.append(f"/ L {low}°")
            if em:
                bits.append(em)
            out.append(" ".join(bits))
        if night_l is not None and int(night_l) <= NIGHT_TAIL_THRESHOLD_C:
            out.append(f"L overnight {int(night_l)}°")
        if night_severe == "thunderstorm":
            out.append("⛈ Storms overnight")
        elif night_severe == "snow":
            out.append("❄️ Snow overnight")
        return out

    parts: list[str] = []
    if now:
        parts.append(f"Now {now}")
    if forecast:
        parts.extend(_period_parts("Today", "today"))
        parts.extend(_period_parts("Tomorrow", "tomorrow"))
    for a in alerts or []:
        parts.append(f"⚠ {a}")
    inner = " · ".join(p for p in parts if p)
    refreshed_html = build_refreshed_span(refreshed_at)
    return (
        f'<div class="weather-strip">'
        f'<span class="weather-main">{inner}</span>'
        f"{refreshed_html}"
        f"</div>"
    )
