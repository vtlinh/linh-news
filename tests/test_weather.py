from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
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


def _hourly_period(start_at: datetime, temp_f: int, short: str) -> dict:
    """Build a fake NWS hourly-forecast period in the API's wire shape."""
    return {
        "startTime": start_at.isoformat(),
        "temperature": temp_f,
        "temperatureUnit": "F",
        "shortForecast": short,
    }


def _seed_day(
    s,
    coords: str,
    local_day,
    *,
    daytime: list[tuple[int, int, str]] | None = None,
    nighttime: list[tuple[int, int, str]] | None = None,
):
    """Insert ``weather_hourly`` rows for a single local date.

    Each tuple is ``(local_hour, temp_c, short_forecast)``. Local hours are
    converted to UTC via ``LOCAL_TZ`` so the summarize_forecast windowing
    matches what the generation pipeline produces."""
    from datetime import datetime as _dt
    from datetime import time as _t
    from datetime import timedelta as _td

    from app.db import WeatherHourly
    from app.settings import LOCAL_TZ

    fetched = datetime.now(UTC)
    for hour, temp_c, short in daytime or []:
        local = _dt.combine(local_day, _t(hour, 0), tzinfo=LOCAL_TZ)
        s.add(
            WeatherHourly(
                coords=coords,
                start_at=local.astimezone(UTC),
                temp_c=temp_c,
                short_forecast=short,
                fetched_at=fetched,
            )
        )
    for hour, temp_c, short in nighttime or []:
        # Hours < 7 belong to the next morning of the same overnight window.
        day = local_day if hour >= 22 else local_day + _td(days=1)
        local = _dt.combine(day, _t(hour, 0), tzinfo=LOCAL_TZ)
        s.add(
            WeatherHourly(
                coords=coords,
                start_at=local.astimezone(UTC),
                temp_c=temp_c,
                short_forecast=short,
                fetched_at=fetched,
            )
        )
    s.commit()


def test_resolve_grid_caches_after_first_call(db_session):
    """First call hits /points; second call short-circuits to the cached row."""
    from app.db import WeatherGrid

    points_response = {
        "properties": {"gridId": "OKX", "gridX": 29, "gridY": 56},
    }
    with patch.object(weather, "_get_json", return_value=points_response) as fake:
        out1 = weather.resolve_grid(db_session, "41.0,-74.0")
        out2 = weather.resolve_grid(db_session, "41.0,-74.0")
    assert out1 == ("OKX", 29, 56)
    assert out2 == ("OKX", 29, 56)
    fake.assert_called_once()
    row = db_session.get(WeatherGrid, "41.0,-74.0")
    assert row is not None and row.grid_id == "OKX"


def test_resolve_grid_returns_none_on_network_error(db_session):
    with patch.object(weather, "_get_json", side_effect=TimeoutError("boom")):
        assert weather.resolve_grid(db_session, "41.0,-74.0") is None


def test_fetch_hourly_forecast_converts_fahrenheit(db_session):  # noqa: ARG001
    """NWS hourly always returns °F — we round-convert to integer °C."""
    fake = {
        "properties": {
            "periods": [
                _hourly_period(datetime(2026, 5, 14, 11, 0, tzinfo=UTC), 50, "Sunny"),
                _hourly_period(datetime(2026, 5, 14, 12, 0, tzinfo=UTC), 32, "Clear"),
            ]
        }
    }
    with patch.object(weather, "_get_json", return_value=fake):
        out = weather.fetch_hourly_forecast("OKX", 29, 56)
    assert len(out) == 2
    assert out[0]["temp_c"] == 10  # 50°F → 10°C
    assert out[1]["temp_c"] == 0  # 32°F → 0°C
    assert out[0]["short_forecast"] == "Sunny"
    assert out[0]["start_at"] == datetime(2026, 5, 14, 11, 0, tzinfo=UTC)


def test_cache_hourly_forecast_upserts_and_prunes(db_session):
    from app.db import WeatherHourly

    # Old row that should get pruned (older than retention window).
    db_session.add(
        WeatherHourly(
            coords="41,-74",
            start_at=datetime.now(UTC) - timedelta(days=30),
            temp_c=99,
            short_forecast="ancient",
            fetched_at=datetime.now(UTC) - timedelta(days=30),
        )
    )
    db_session.commit()
    periods = [
        {
            "start_at": datetime(2026, 5, 14, 11, 0, tzinfo=UTC),
            "temp_c": 15,
            "short_forecast": "Sunny",
        }
    ]
    n = weather.cache_hourly_forecast(db_session, "41,-74", periods)
    assert n == 1
    # Re-upsert with a new temp → row updated in place, no duplicate.
    periods[0]["temp_c"] = 16
    weather.cache_hourly_forecast(db_session, "41,-74", periods)
    rows = db_session.query(WeatherHourly).filter_by(coords="41,-74").all()
    assert len(rows) == 1
    assert rows[0].temp_c == 16
    # Old row was pruned.
    ancient = (
        db_session.query(WeatherHourly).filter(WeatherHourly.short_forecast == "ancient").first()
    )
    assert ancient is None


def test_summarize_forecast_picks_dominant_bucket(db_session):
    """A day with mostly partly-cloudy + a couple of thunderstorm hours
    summarizes as a thunderstorm day."""
    today = date(2026, 5, 14)
    _seed_day(
        db_session,
        "X",
        today,
        daytime=[
            (7, 10, "Partly Cloudy"),
            (8, 11, "Partly Cloudy"),
            (9, 12, "Partly Cloudy"),
            (14, 18, "Chance Showers And Thunderstorms"),
            (15, 18, "Chance Showers And Thunderstorms"),
            (21, 14, "Partly Cloudy"),
        ],
    )
    out = weather.summarize_forecast(db_session, "X", today)
    assert out["today_h"] == 18
    assert out["today_l"] == 10
    assert out["today_em"] == "⛈"
    # Representative shortForecast must classify back to the same bucket.
    assert "Thunderstorm" in out["today_short"]
    # No tomorrow data → tomorrow keys absent.
    assert "tomorrow_h" not in out


def test_summarize_forecast_uses_only_daytime_window(db_session):
    """Hours outside 7 AM – 10 PM local must not influence high/low."""
    today = date(2026, 5, 14)
    _seed_day(
        db_session,
        "X",
        today,
        daytime=[(7, 10, "Sunny"), (21, 12, "Sunny")],
        # Pre-7am and 10pm-onward hours: extreme temps that would otherwise
        # dominate. The 10pm hour itself is night by our rule.
        nighttime=[(22, 99, "Clear"), (23, -10, "Clear")],
    )
    out = weather.summarize_forecast(db_session, "X", today)
    assert out["today_h"] == 12  # 99° from 10 PM excluded
    assert out["today_l"] == 10  # -10° from 11 PM excluded


def test_summarize_forecast_overnight_low_and_severe(db_session):
    """night_l is the min of 10 PM–7 AM; severe set only when storm/snow
    appears in that window."""
    today = date(2026, 5, 14)
    _seed_day(
        db_session,
        "X",
        today,
        daytime=[(10, 18, "Sunny")],
        nighttime=[
            (22, 8, "Mostly Cloudy"),
            (23, 5, "Mostly Cloudy"),
            (0, 2, "Thunderstorms"),
            (4, 4, "Mostly Cloudy"),
        ],
    )
    out = weather.summarize_forecast(db_session, "X", today)
    assert out["today_night_l"] == 2
    assert out["today_night_severe"] == "thunderstorm"


def test_summarize_forecast_no_severe_when_clear_night(db_session):
    today = date(2026, 5, 14)
    _seed_day(
        db_session,
        "X",
        today,
        daytime=[(10, 18, "Sunny")],
        nighttime=[(22, 8, "Clear"), (3, 5, "Mostly Cloudy")],
    )
    out = weather.summarize_forecast(db_session, "X", today)
    assert out["today_night_l"] == 5
    assert "today_night_severe" not in out


def test_build_weather_strip_overnight_tail_cold(db_session):  # noqa: ARG001
    """night_l ≤ 5°C → strip gets an ``L overnight N°`` segment."""
    out = weather.build_weather_strip(
        "",
        {
            "today_h": 8,
            "today_l": 2,
            "today_em": "☁️",
            "today_night_l": 3,
        },
        [],
    )
    assert "L overnight 3°" in out


def test_build_weather_strip_no_overnight_tail_when_mild(db_session):  # noqa: ARG001
    out = weather.build_weather_strip(
        "",
        {
            "today_h": 18,
            "today_l": 11,
            "today_em": "☀️",
            "today_night_l": 9,
        },
        [],
    )
    assert "overnight" not in out


def test_build_weather_strip_severe_overnight_clause(db_session):  # noqa: ARG001
    out = weather.build_weather_strip(
        "",
        {
            "today_h": 14,
            "today_l": 8,
            "today_em": "⛈",
            "today_night_severe": "thunderstorm",
        },
        [],
    )
    assert "Storms overnight" in out


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
