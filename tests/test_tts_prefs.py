from __future__ import annotations

import pytest

from app import user_settings
from app.db import UserSettings


@pytest.fixture()
def email() -> str:
    return "vtlinh87@gmail.com"


def test_get_returns_defaults_for_existing_user_without_prefs(db_session, client, email):
    out = user_settings.get(db_session, email)
    assert out["tts_prefs"] == {
        "voice_uri": None,
        "voice_name": None,
        "engine": None,
        "rate": 1.0,
        "pitch": 1.0,
        "volume": 1.0,
    }


def test_save_tts_prefs_clamps_out_of_range(db_session, client, email):
    saved = user_settings.save_tts_prefs(
        db_session,
        email,
        {"rate": 5.0, "pitch": -1.0, "volume": 2.0},
    )
    assert saved["rate"] == 2.0  # clamped to max
    assert saved["pitch"] == 0.0  # clamped to min
    assert saved["volume"] == 1.0  # clamped to max
    assert saved["voice_uri"] is None
    assert saved["voice_name"] is None
    assert saved["engine"] is None


def test_save_tts_prefs_persists_voice_and_engine(db_session, client, email):
    saved = user_settings.save_tts_prefs(
        db_session,
        email,
        {
            "voice_uri": "com.apple.voice.alex",
            "voice_name": "Alex",
            "engine": "Apple",
            "rate": 1.25,
            "pitch": 0.9,
            "volume": 0.8,
        },
    )
    assert saved["voice_uri"] == "com.apple.voice.alex"
    assert saved["voice_name"] == "Alex"
    assert saved["engine"] == "Apple"
    assert saved["rate"] == pytest.approx(1.25)
    assert saved["pitch"] == pytest.approx(0.9)
    assert saved["volume"] == pytest.approx(0.8)

    # Reload through ORM and confirm it stuck.
    row = db_session.get(UserSettings, email)
    assert row.tts_prefs_json["voice_name"] == "Alex"
    assert row.tts_prefs_json["engine"] == "Apple"


def test_save_tts_prefs_invalid_input_falls_back_to_defaults(db_session, client, email):
    saved = user_settings.save_tts_prefs(db_session, email, "not a dict")
    assert saved["rate"] == 1.0
    assert saved["pitch"] == 1.0
    assert saved["volume"] == 1.0


def test_route_requires_auth(client):
    r = client.post("/tts/prefs", json={"rate": 1.5})
    assert r.status_code in (401, 403)


def test_route_saves_prefs(client, login_as, email):
    login_as(email)
    r = client.post(
        "/tts/prefs",
        json={"voice_name": "Daniel", "rate": 1.4, "pitch": 1.1, "volume": 0.7},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["tts_prefs"]["voice_name"] == "Daniel"
    assert data["tts_prefs"]["rate"] == pytest.approx(1.4)


def test_route_round_trip_via_get(client, login_as, db_session, email):
    login_as(email)
    client.post("/tts/prefs", json={"rate": 1.75, "voice_name": "Karen"})
    out = user_settings.get(db_session, email)
    assert out["tts_prefs"]["rate"] == pytest.approx(1.75)
    assert out["tts_prefs"]["voice_name"] == "Karen"


def test_data_save_does_not_clobber_tts_prefs(client, login_as, db_session, email):
    """Saving section/profile data must preserve previously saved TTS prefs."""
    login_as(email)
    client.post("/tts/prefs", json={"rate": 1.3, "voice_name": "Samantha"})
    # Drive the data tab save endpoint with a minimal but valid payload.
    payload = {
        "display_name": "Linh",
        "address": "",
        "temperature_unit": "F",
        "sections": [
            {
                "key": "general",
                "title": "General",
                "description": "Anything",
                "subsection_count": 3,
                "preferred_sources": [],
                "use_global_sources": True,
                "can_be_headline": False,
            }
        ],
        "children": [],
    }
    r = client.post("/data/save", json=payload)
    assert r.status_code == 200, r.text
    # tts_prefs_json still set
    row = db_session.get(UserSettings, email)
    assert row.tts_prefs_json is not None
    assert row.tts_prefs_json["voice_name"] == "Samantha"
    assert row.tts_prefs_json["rate"] == pytest.approx(1.3)
