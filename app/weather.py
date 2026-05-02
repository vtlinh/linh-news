"""Real-time current-conditions fetch from the NWS (National Weather Service) API.

NWS is free, no API key required, and authoritative for US locations.
Only the 'Now' observation (temperature + condition + wind) comes from here.
Today/tomorrow forecast high/low is still handled by Claude via web_search.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

_UA = "Linh-News/1.0 (vtlinh87+linhnews@gmail.com)"
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


def _wind_dir(degrees: float) -> str:
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
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
    Returns '' on any error — the caller should then let Claude fall back to
    web_search for the current observation.
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
        obs = _get_json(
            f"https://api.weather.gov/stations/{station_id}/observations/latest"
        )
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

    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            KeyError, ValueError, json.JSONDecodeError) as e:
        log.warning("NWS current-conditions fetch failed: %s", e)
        return ""


# ───────────────── Forecast (today + tomorrow H/L) ─────────────────


def _to_celsius(value: float, unit: str) -> float:
    """NWS may return F or C depending on `units`. Normalise to C."""
    u = (unit or "").lower()
    if u in ("f", "fahrenheit", "wmounit:degf", "wmounit:degree_(fahrenheit)"):
        return (float(value) - 32) * 5 / 9
    return float(value)


def fetch_forecast(lat_lon: str) -> dict:
    """Return today/tomorrow high/low + condition emoji.

    Shape::
        {"today_h": 14, "today_l": 7, "today_em": "☀️",
         "tomorrow_h": 16, "tomorrow_l": 9, "tomorrow_em": "☁️"}

    Returns ``{}`` on any error so the caller can render a partial strip.
    """
    try:
        lat, lon = [p.strip() for p in lat_lon.split(",", 1)]

        point = _get_json(f"https://api.weather.gov/points/{lat},{lon}")
        forecast_url = point["properties"]["forecast"]
        # Request SI so the temperature comes back in °C without conversion.
        if "?" in forecast_url:
            forecast_url += "&units=si"
        else:
            forecast_url += "?units=si"

        data = _get_json(forecast_url)
        periods = data.get("properties", {}).get("periods", [])
        if not periods:
            log.warning("NWS forecast: no periods returned for %s", lat_lon)
            return {}

        # NWS periods alternate day/night; first 4 = today day/night + tomorrow
        # day/night (or starting from tomorrow if it's already evening).
        today_day = next((p for p in periods if p.get("isDaytime")), None)
        nights = [p for p in periods if not p.get("isDaytime")]
        days = [p for p in periods if p.get("isDaytime")]

        out: dict = {}
        if today_day:
            out["today_h"] = round(_to_celsius(
                today_day["temperature"], today_day.get("temperatureUnit", "C")
            ))
            out["today_em"] = _emoji(today_day.get("shortForecast", ""))
        if nights:
            out["today_l"] = round(_to_celsius(
                nights[0]["temperature"], nights[0].get("temperatureUnit", "C")
            ))
        if len(days) >= 2:
            out["tomorrow_h"] = round(_to_celsius(
                days[1]["temperature"], days[1].get("temperatureUnit", "C")
            ))
            out["tomorrow_em"] = _emoji(days[1].get("shortForecast", ""))
        if len(nights) >= 2:
            out["tomorrow_l"] = round(_to_celsius(
                nights[1]["temperature"], nights[1].get("temperatureUnit", "C")
            ))
        return out

    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            KeyError, ValueError, json.JSONDecodeError) as e:
        log.warning("NWS forecast fetch failed: %s", e)
        return {}


# ───────────────── Active alerts (NWS) ─────────────────


def fetch_alerts(lat_lon: str) -> list[str]:
    """Return short headlines for active NWS alerts at the given point.

    Each entry looks like ``"Wind Advisory until 6 PM"``. Returns ``[]`` on
    any error or when no alerts are active.
    """
    try:
        lat, lon = [p.strip() for p in lat_lon.split(",", 1)]
        url = (
            "https://api.weather.gov/alerts/active?"
            + urllib.parse.urlencode({"point": f"{lat},{lon}"})
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
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            KeyError, ValueError, json.JSONDecodeError) as e:
        log.warning("NWS alerts fetch failed: %s", e)
        return []


# ───────────────── 'Now' DB cache ─────────────────


def get_now_cached(
    s: Session, coords: str, max_age: timedelta = timedelta(hours=1),
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


def build_weather_strip(
    now: str, forecast: dict, alerts: list[str],
) -> str:
    """Assemble the one-line weather strip HTML.

    Format::
        <div class="weather-strip">Now 12°C 🌤 · Today H 14° / L 7° ☀️
            · Tomorrow H 16° / L 9° ☁️ · ⚠ Wind advisory until 6 PM</div>
    """
    parts: list[str] = []
    if now:
        parts.append(f"Now {now}")
    if forecast:
        today_h = forecast.get("today_h")
        today_l = forecast.get("today_l")
        today_em = forecast.get("today_em", "")
        if today_h is not None or today_l is not None:
            bits = ["Today"]
            if today_h is not None:
                bits.append(f"H {today_h}°")
            if today_l is not None:
                bits.append(f"/ L {today_l}°")
            if today_em:
                bits.append(today_em)
            parts.append(" ".join(bits))
        tom_h = forecast.get("tomorrow_h")
        tom_l = forecast.get("tomorrow_l")
        tom_em = forecast.get("tomorrow_em", "")
        if tom_h is not None or tom_l is not None:
            bits = ["Tomorrow"]
            if tom_h is not None:
                bits.append(f"H {tom_h}°")
            if tom_l is not None:
                bits.append(f"/ L {tom_l}°")
            if tom_em:
                bits.append(tom_em)
            parts.append(" ".join(bits))
    for a in alerts or []:
        parts.append(f"⚠ {a}")
    inner = " · ".join(p for p in parts if p)
    return f'<div class="weather-strip">{inner}</div>'
