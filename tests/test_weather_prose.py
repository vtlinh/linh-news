from __future__ import annotations

import random

import pytest

from app import weather, weather_prose
from app.db import WeatherPhrase

# ─────────────────────── bucket classification ───────────────────────


@pytest.mark.parametrize(
    "short,expected",
    [
        ("Sunny", "sunny"),
        ("Fair", "sunny"),
        ("Clear", "sunny"),
        ("Mostly Sunny", "mostly_sunny"),
        ("Mostly Clear", "mostly_sunny"),
        ("Partly Cloudy", "partly_cloudy"),
        ("Partly Sunny", "partly_cloudy"),
        ("Mostly Cloudy", "cloudy"),
        ("Cloudy", "cloudy"),
        ("Overcast", "cloudy"),
        ("Showers and Thunderstorms", "thunderstorm"),
        ("Slight Chance Showers", "rain"),
        ("Light Rain", "rain"),
        ("Drizzle", "rain"),
        ("Snow", "snow"),
        ("Sleet", "snow"),
        ("Freezing Rain", "snow"),
        ("Patchy Fog", "fog"),
        ("Areas of Haze", "fog"),
        ("Windy", "windy"),
        ("Breezy", "windy"),
    ],
)
def test_bucket_maps_known_conditions(short: str, expected: str) -> None:
    assert weather.bucket(short) == expected


def test_bucket_unknown_falls_back_to_cloudy() -> None:
    assert weather.bucket("Mysterious Sky Phenomenon") == "cloudy"
    assert weather.bucket("") == "cloudy"


# ─────────────────────── seed library shape ───────────────────────


def test_seed_library_has_360_entries() -> None:
    assert weather_prose.count_seed_rows() == 360
    rows = weather_prose.all_seed_rows()
    assert len(rows) == 360
    # All buckets and periods present, 20 per (period, bucket).
    pairs: dict[tuple[str, str], int] = {}
    for r in rows:
        pairs[r["period"], r["bucket"]] = pairs.get((r["period"], r["bucket"]), 0) + 1
    for b in weather_prose.BUCKETS:
        for p in weather_prose.PERIODS:
            assert pairs[p, b] == 20, f"{p}/{b} has {pairs.get((p, b))} rows"


def test_seed_phrases_have_required_slots() -> None:
    for b in weather_prose.BUCKETS:
        for p in weather_prose.PERIODS:
            for text in weather_prose.PHRASES[b][p]:
                assert "{h}" in text, f"missing {{h}} in {b}/{p}: {text}"
                assert "{l}" in text, f"missing {{l}} in {b}/{p}: {text}"
                # Format substitution must succeed and leave no braces behind.
                rendered = text.format(h=21, l=9)
                assert "{" not in rendered and "}" not in rendered


def test_seed_phrases_bold_keyword_present() -> None:
    import re

    # {l} is the 7 AM–10 PM daytime low, so phrases no longer reference
    # tonight / overnight here — only the day's own period word is bolded.
    pat = re.compile(r"<b>(Today|Tomorrow)</b>", re.IGNORECASE)
    for b in weather_prose.BUCKETS:
        for p in weather_prose.PERIODS:
            for text in weather_prose.PHRASES[b][p]:
                assert pat.search(text), f"no bold keyword in {b}/{p}: {text}"


def test_seed_phrases_avoid_night_wording() -> None:
    """Daytime phrases must not say "tonight" or "overnight" — that's the
    night clause's job. Catches regressions if someone re-introduces the
    old wording while editing the seed library."""
    for b in weather_prose.BUCKETS:
        for p in weather_prose.PERIODS:
            for text in weather_prose.PHRASES[b][p]:
                assert "tonight" not in text.lower(), f"'tonight' in {b}/{p}: {text}"
                assert "overnight" not in text.lower(), f"'overnight' in {b}/{p}: {text}"


# ─────────────────────── render_prose_html ───────────────────────


def _seed(s) -> None:
    """Load every seed row (today/tomorrow + night) into the in-memory test DB."""
    for r in weather_prose.all_seed_rows():
        s.add(WeatherPhrase(period=r["period"], bucket=r["bucket"], text=r["text"]))
    for r in weather_prose.night_seed_rows():
        s.add(WeatherPhrase(period=r["period"], bucket=r["bucket"], text=r["text"]))
    s.commit()


def test_night_seed_library_shape() -> None:
    rows = weather_prose.night_seed_rows()
    assert len(rows) == 40
    counts: dict[str, int] = {}
    for r in rows:
        assert r["period"] == "night"
        assert r["bucket"] in weather_prose.NIGHT_BUCKETS
        counts[r["bucket"]] = counts.get(r["bucket"], 0) + 1
    for bkt in weather_prose.NIGHT_BUCKETS:
        assert counts[bkt] == 20


def test_render_prose_html_contains_today_and_tomorrow(db_session) -> None:
    _seed(db_session)
    forecast = {
        "today_h": 21,
        "today_l": 9,
        "today_short": "Sunny",
        "tomorrow_h": 18,
        "tomorrow_l": 10,
        "tomorrow_short": "Showers",
    }
    out = weather_prose.render_prose_html(
        forecast,
        [],
        db_session,
        rng=random.Random(42),
    )
    assert out.startswith('<div class="weather-prose">')
    assert out.endswith("</div>")
    # Bold keywords present, slots filled with the high/low integers.
    assert "21" in out and "9" in out and "18" in out and "10" in out
    assert "<b>" in out  # Today/Tomorrow/Tonight got bolded somewhere.


def test_render_prose_html_seeded_rng_is_deterministic(db_session) -> None:
    _seed(db_session)
    forecast = {
        "today_h": 21,
        "today_l": 9,
        "today_short": "Sunny",
        "tomorrow_h": 18,
        "tomorrow_l": 10,
        "tomorrow_short": "Showers",
    }
    a = weather_prose.render_prose_html(forecast, [], db_session, rng=random.Random(7))
    b = weather_prose.render_prose_html(forecast, [], db_session, rng=random.Random(7))
    assert a == b


def test_render_prose_html_appends_alerts(db_session) -> None:
    _seed(db_session)
    forecast = {
        "today_h": 14,
        "today_l": 7,
        "today_short": "Sunny",
        "tomorrow_h": 16,
        "tomorrow_l": 9,
        "tomorrow_short": "Cloudy",
    }
    out = weather_prose.render_prose_html(
        forecast,
        ["Wind Advisory until 6 PM"],
        db_session,
        rng=random.Random(0),
    )
    assert "⚠ Wind Advisory until 6 PM" in out


def test_render_prose_html_picks_correct_bucket(db_session) -> None:
    """Today=Snow should select from the snow bucket, regardless of tomorrow."""
    db_session.add(
        WeatherPhrase(
            period="today",
            bucket="snow",
            text="UNIQUE_SNOW_TODAY <b>Today</b> {h}°C/{l}°C.",
        )
    )
    db_session.add(
        WeatherPhrase(
            period="tomorrow",
            bucket="sunny",
            text="UNIQUE_SUN_TOMORROW <b>Tomorrow</b> {h}°C/{l}°C.",
        )
    )
    db_session.commit()
    forecast = {
        "today_h": 2,
        "today_l": -4,
        "today_short": "Snow",
        "tomorrow_h": 6,
        "tomorrow_l": 0,
        "tomorrow_short": "Sunny",
    }
    out = weather_prose.render_prose_html(forecast, [], db_session)
    assert "UNIQUE_SNOW_TODAY" in out
    assert "UNIQUE_SUN_TOMORROW" in out
    assert "2°C" in out and "-4°C" in out and "6°C" in out and "0°C" in out


def test_render_prose_html_empty_when_no_phrases(db_session) -> None:
    """Empty seed table → empty string. Caller falls back to legacy strip."""
    forecast = {
        "today_h": 21,
        "today_l": 9,
        "today_short": "Sunny",
        "tomorrow_h": 18,
        "tomorrow_l": 10,
        "tomorrow_short": "Showers",
    }
    out = weather_prose.render_prose_html(forecast, [], db_session)
    assert out == ""


def test_render_prose_html_appends_overnight_low_when_cold(db_session) -> None:
    """night_l ≤ 5°C → today clause gets an "; overnight low N°C" tail."""
    _seed(db_session)
    forecast = {
        "today_h": 8,
        "today_l": 2,
        "today_short": "Cloudy",
        "today_night_l": 3,
    }
    out = weather_prose.render_prose_html(forecast, [], db_session, rng=random.Random(1))
    assert "overnight low 3°C" in out


def test_render_prose_html_no_overnight_tail_when_mild(db_session) -> None:
    _seed(db_session)
    forecast = {
        "today_h": 22,
        "today_l": 14,
        "today_short": "Cloudy",
        "today_night_l": 12,
    }
    out = weather_prose.render_prose_html(forecast, [], db_session, rng=random.Random(1))
    assert "overnight low" not in out


def test_render_prose_html_appends_severe_night_clause(db_session) -> None:
    """today_night_severe → a night-period phrase from that bucket is
    appended to the today clause."""
    _seed(db_session)
    forecast = {
        "today_h": 18,
        "today_l": 12,
        "today_short": "Partly Cloudy",
        "today_night_severe": "thunderstorm",
    }
    out = weather_prose.render_prose_html(forecast, [], db_session, rng=random.Random(2))
    # At least one thunderstorm-night phrase keyword appears.
    assert any(kw in out.lower() for kw in ("thunder", "storm", "lightning"))


def test_render_prose_html_severe_clause_follows_overnight_tail(db_session) -> None:
    """When both a cold-overnight tail and severe-night clause apply, the
    night clause sits after the tail, not before it."""
    _seed(db_session)
    forecast = {
        "today_h": -1,
        "today_l": -5,
        "today_short": "Snow",
        "today_night_l": -8,
        "today_night_severe": "snow",
    }
    out = weather_prose.render_prose_html(forecast, [], db_session, rng=random.Random(3))
    tail_idx = out.find("overnight low")
    # The night-clause phrases mention "snow" (case-insensitive).
    night_kw_idx = out.lower().rfind("snow")
    assert tail_idx != -1
    assert night_kw_idx > tail_idx


def test_render_prose_html_skips_periods_with_missing_data(db_session) -> None:
    _seed(db_session)
    # Missing tomorrow_l → tomorrow clause must be omitted; today still renders.
    forecast = {
        "today_h": 21,
        "today_l": 9,
        "today_short": "Sunny",
        "tomorrow_h": 18,
        "tomorrow_short": "Showers",
    }
    out = weather_prose.render_prose_html(forecast, [], db_session, rng=random.Random(1))
    assert out.startswith('<div class="weather-prose">')
    # No braces left over from a partially formatted clause.
    assert "{" not in out and "}" not in out
