"""Real-time current-conditions fetch from the NWS (National Weather Service) API.

NWS is free, no API key required, and authoritative for US locations.
Only the 'Now' observation (temperature + condition + wind) comes from here.
Today/tomorrow forecast high/low is still handled by Claude via web_search.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

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
