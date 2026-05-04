from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from app import weather
from app.db import WeatherNow


def test_build_weather_strip_full():
    out = weather.build_weather_strip(
        "12°C ⛅ · Wind NW 10 mph",
        {
            "today_h": 14,
            "today_l": 7,
            "today_em": "☀️",
            "tomorrow_h": 16,
            "tomorrow_l": 9,
            "tomorrow_em": "☁️",
        },
        ["Wind Advisory until 6 PM"],
    )
    assert out.startswith('<div class="weather-strip">')
    assert out.endswith("</div>")
    assert '<span class="weather-main">' in out
    assert "Now 12°C ⛅ · Wind NW 10 mph" in out
    assert "Today H 14° / L 7° ☀️" in out
    assert "Tomorrow H 16° / L 9° ☁️" in out
    assert "⚠ Wind Advisory until 6 PM" in out
    # No refreshed_at supplied → no badge.
    assert "weather-refreshed" not in out


def test_build_weather_strip_with_refreshed_at():
    from datetime import UTC, datetime

    refreshed = datetime(2026, 5, 2, 18, 0, tzinfo=UTC)  # 14:00 EDT
    out = weather.build_weather_strip(
        "0°C ❄️",
        {},
        [],
        refreshed_at=refreshed,
    )
    assert '<span class="weather-refreshed">Refreshed at ' in out
    # 18:00 UTC = 14:00 EDT in May.
    assert "14:00 EDT" in out


def test_build_weather_strip_no_alerts_no_forecast():
    out = weather.build_weather_strip("0°C ❄️", {}, [])
    assert out == '<div class="weather-strip"><span class="weather-main">Now 0°C ❄️</span></div>'


def test_build_weather_strip_partial_forecast():
    # Only today data — the tomorrow piece must be omitted, not rendered blank.
    out = weather.build_weather_strip(
        "5°C ⛅",
        {"today_h": 10, "today_l": 2, "today_em": "🌤"},
        [],
    )
    assert "Tomorrow" not in out
    assert "Today H 10° / L 2° 🌤" in out


def test_get_now_cached_uses_cache_when_fresh(db_session):
    db_session.add(
        WeatherNow(
            coords="41.0,-74.0",
            now_text="11°C ⛅",
            observed_at=datetime.now(UTC) - timedelta(minutes=10),
        )
    )
    db_session.commit()
    with patch.object(weather, "fetch_current_now") as fake:
        out = weather.get_now_cached(db_session, "41.0,-74.0")
    assert out == "11°C ⛅"
    fake.assert_not_called()


def test_get_now_cached_refreshes_when_stale(db_session):
    db_session.add(
        WeatherNow(
            coords="41.0,-74.0",
            now_text="OLD",
            observed_at=datetime.now(UTC) - timedelta(hours=2),
        )
    )
    db_session.commit()
    with patch.object(weather, "fetch_current_now", return_value="22°C ☀️"):
        out = weather.get_now_cached(db_session, "41.0,-74.0")
    assert out == "22°C ☀️"
    row = db_session.get(WeatherNow, "41.0,-74.0")
    assert row.now_text == "22°C ☀️"


def test_get_now_cached_falls_back_to_stale_on_failure(db_session):
    db_session.add(
        WeatherNow(
            coords="41.0,-74.0",
            now_text="STALE-BUT-OK",
            observed_at=datetime.now(UTC) - timedelta(hours=2),
        )
    )
    db_session.commit()
    with patch.object(weather, "fetch_current_now", return_value=""):
        out = weather.get_now_cached(db_session, "41.0,-74.0")
    assert out == "STALE-BUT-OK"


def test_get_now_cached_empty_when_no_row_and_fetch_fails(db_session):
    with patch.object(weather, "fetch_current_now", return_value=""):
        out = weather.get_now_cached(db_session, "41.0,-74.0")
    assert out == ""


def test_get_now_cached_inserts_when_missing(db_session):
    with patch.object(weather, "fetch_current_now", return_value="3°C ❄️"):
        out = weather.get_now_cached(db_session, "41.0,-74.0")
    assert out == "3°C ❄️"
    row = db_session.get(WeatherNow, "41.0,-74.0")
    assert row is not None
    assert row.now_text == "3°C ❄️"


def test_fetch_forecast_parses_periods():
    fake = {
        "properties": {
            "forecast": "https://api.weather.gov/gridpoints/OKX/33,38/forecast",
        },
    }
    fake_forecast = {
        "properties": {
            "periods": [
                {
                    "isDaytime": True,
                    "temperature": 14,
                    "temperatureUnit": "C",
                    "shortForecast": "Sunny",
                },
                {
                    "isDaytime": False,
                    "temperature": 7,
                    "temperatureUnit": "C",
                    "shortForecast": "Clear",
                },
                {
                    "isDaytime": True,
                    "temperature": 16,
                    "temperatureUnit": "C",
                    "shortForecast": "Cloudy",
                },
                {
                    "isDaytime": False,
                    "temperature": 9,
                    "temperatureUnit": "C",
                    "shortForecast": "Mostly Cloudy",
                },
            ],
        },
    }
    seq = iter([fake, fake_forecast])
    with patch.object(weather, "_get_json", side_effect=lambda _u: next(seq)):
        out = weather.fetch_forecast("41.0,-74.0")
    assert out["today_h"] == 14
    assert out["today_l"] == 7
    assert out["tomorrow_h"] == 16
    assert out["tomorrow_l"] == 9
    assert out["today_em"] == "☀️"
    assert "🌥" in out["tomorrow_em"] or out["tomorrow_em"] == "☁️"


def test_fetch_forecast_converts_fahrenheit():
    fake_point = {"properties": {"forecast": "https://x/f"}}
    fake_forecast = {
        "properties": {
            "periods": [
                {
                    "isDaytime": True,
                    "temperature": 50,
                    "temperatureUnit": "F",
                    "shortForecast": "Sunny",
                },
                {
                    "isDaytime": False,
                    "temperature": 32,
                    "temperatureUnit": "F",
                    "shortForecast": "Clear",
                },
            ],
        },
    }
    seq = iter([fake_point, fake_forecast])
    with patch.object(weather, "_get_json", side_effect=lambda _u: next(seq)):
        out = weather.fetch_forecast("41.0,-74.0")
    assert out["today_h"] == 10  # 50°F -> 10°C
    assert out["today_l"] == 0  # 32°F -> 0°C


def test_fetch_alerts_returns_event_strings():
    fake = {
        "features": [
            {"properties": {"event": "Wind Advisory", "ends": "2026-05-02T18:00:00+00:00"}},
            {"properties": {"event": "Flood Watch", "ends": ""}},
        ],
    }
    with patch.object(weather, "_get_json", return_value=fake):
        out = weather.fetch_alerts("41.0,-74.0")
    assert any("Wind Advisory" in s for s in out)
    assert any("Flood Watch" in s for s in out)


def test_fetch_alerts_empty_when_none_active():
    with patch.object(weather, "_get_json", return_value={"features": []}):
        out = weather.fetch_alerts("41.0,-74.0")
    assert out == []
