from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import patch

from app.db import Edition, HiddenMovie


def _seed_edition(db_session, day: date) -> None:
    db_session.add(
        Edition(
            date=day,
            html="<p>hello</p>",
            pdf=b"%PDF-1.4 fake",
            generated_at=datetime.now(UTC),
        )
    )
    db_session.commit()


def test_home_redirects_unauthenticated(client):
    r = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code in (307, 302)
    assert "/login" in r.headers["location"]


def test_home_works_for_viewer(client, login_as):
    login_as("friend@example.com")
    r = client.get("/")
    assert r.status_code == 200
    assert "Refresh" in r.text


def test_home_substitutes_weather_placeholder(client, login_as, db_session):
    """The view-time `_inject_weather` injector must replace the placeholder
    using the per-edition forecast/alerts JSON and the cached 'Now' string."""
    from app import weather
    db_session.add(
        Edition(
            date=date(2026, 5, 2),
            html='<!-- WEATHER_PLACEHOLDER --><div>x</div>',
            pdf=b"%PDF-1.4 fake",
            generated_at=datetime.now(UTC),
            weather_forecast_json={
                "today_h": 14, "today_l": 7, "today_em": "☀️",
                "tomorrow_h": 16, "tomorrow_l": 9, "tomorrow_em": "☁️",
            },
            weather_alerts_json=["Wind Advisory until 6 PM"],
        )
    )
    db_session.commit()

    login_as("friend@example.com")
    with patch("app.settings.local_today", return_value=date(2026, 5, 2)), \
            patch.object(weather, "get_now_cached", return_value="12°C ⛅"):
        r = client.get("/d/2026-05-02")
    assert r.status_code == 200
    assert "<!-- WEATHER_PLACEHOLDER -->" not in r.text
    assert "Now 12°C ⛅" in r.text
    assert "Today H 14° / L 7° ☀️" in r.text
    assert "⚠ Wind Advisory until 6 PM" in r.text


def test_pdf_404_when_missing(client, login_as):
    login_as("vtlinh87@gmail.com")
    r = client.get("/pdf/2026-04-30")
    assert r.status_code == 404


def test_pdf_streams_when_present(client, login_as, db_session):
    _seed_edition(db_session, date(2026, 4, 30))
    login_as("friend@example.com")
    r = client.get("/pdf/2026-04-30")
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")
    # Download filename includes a 10-hex-char content-hash suffix so each
    # newly generated edition gets a unique name.
    import hashlib
    expected = hashlib.sha256(r.content).hexdigest()[:10]
    cd = r.headers["content-disposition"]
    assert f"linh-times-2026-04-30-{expected}.pdf" in cd


def test_pdf_filename_changes_when_content_changes(client, login_as, db_session):
    """Two editions with different bytes must produce different filenames."""
    _seed_edition(db_session, date(2026, 4, 30))
    login_as("friend@example.com")
    r1 = client.get("/pdf/2026-04-30")
    fn1 = r1.headers["content-disposition"]
    # Mutate the stored bytes and re-download.
    edition = db_session.get(Edition, date(2026, 4, 30))
    edition.pdf = b"%PDF-1.4 different-bytes"
    db_session.commit()
    r2 = client.get("/pdf/2026-04-30")
    fn2 = r2.headers["content-disposition"]
    assert fn1 != fn2


def test_hide_movie_admin_only(client, login_as, db_session):
    login_as("friend@example.com")
    r = client.post("/hide-movie", json={"title": "Frozen 4"})
    assert r.status_code == 403

    login_as("vtlinh87@gmail.com")
    r = client.post("/hide-movie", json={"title": "Frozen 4"})
    assert r.status_code == 200
    assert db_session.get(HiddenMovie, "Frozen 4") is not None


def test_admin_pages_blocked_for_viewer(client, login_as):
    login_as("friend@example.com")
    assert client.get("/admin/events").status_code == 403


def test_refresh_spawns_detached_subprocess(client, login_as):
    login_as("vtlinh87@gmail.com")
    with patch("app.main.subprocess.Popen") as popen:
        r = client.post("/refresh")
    assert r.status_code == 202
    assert r.json()["in_progress"] is True
    popen.assert_called_once()
    args, kwargs = popen.call_args
    cmd = args[0]
    assert cmd[-2:] == ["app.generate", "refresh"]


def test_refresh_returns_409_when_lock_held(client, login_as):
    login_as("vtlinh87@gmail.com")
    with patch("app.main.cache.begin_edition_refresh", return_value=False), \
         patch("app.main.subprocess.Popen") as popen:
        r = client.post("/refresh")
    assert r.status_code == 409
    popen.assert_not_called()


def test_refresh_forbidden_for_non_admin(client, login_as):
    login_as("friend@example.com")
    with patch("app.main.subprocess.Popen") as popen:
        r = client.post("/refresh")
    assert r.status_code == 403
    popen.assert_not_called()


# ── /pdf/latest: public, gated by shared secret ───────────────────────────


def _seed_latest(db_session, day: date) -> None:
    db_session.add(
        Edition(
            date=day,
            html="<p>hi</p>",
            pdf=b"%PDF-1.4 fake-latest",
            generated_at=datetime.now(UTC),
        )
    )
    db_session.commit()


def test_pdf_latest_401_when_token_unset(client, db_session):
    _seed_latest(db_session, date(2026, 4, 30))
    # Default settings have an empty PDF_LATEST_TOKEN — must always 401.
    r = client.get("/pdf/latest?token=anything")
    assert r.status_code == 401


def test_pdf_latest_query_param(client, db_session, monkeypatch):
    _seed_latest(db_session, date(2026, 4, 30))
    from app.settings import get_settings
    monkeypatch.setattr(get_settings(), "pdf_latest_token", "s3cret", raising=False)

    r = client.get("/pdf/latest?token=s3cret")
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")

    r = client.get("/pdf/latest?token=wrong")
    assert r.status_code == 401

    r = client.get("/pdf/latest")
    assert r.status_code == 401


def test_pdf_latest_bearer_header(client, db_session, monkeypatch):
    _seed_latest(db_session, date(2026, 4, 30))
    from app.settings import get_settings
    monkeypatch.setattr(get_settings(), "pdf_latest_token", "s3cret", raising=False)

    r = client.get("/pdf/latest", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")

    r = client.get("/pdf/latest", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_pdf_day_with_token_query(client, db_session, monkeypatch):
    _seed_edition(db_session, date(2026, 4, 30))
    from app.settings import get_settings
    monkeypatch.setattr(get_settings(), "pdf_latest_token", "s3cret", raising=False)

    client.cookies.clear()
    r = client.get("/pdf/2026-04-30?token=s3cret", follow_redirects=False)
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")


def test_pdf_day_with_bearer_header(client, db_session, monkeypatch):
    _seed_edition(db_session, date(2026, 4, 30))
    from app.settings import get_settings
    monkeypatch.setattr(get_settings(), "pdf_latest_token", "s3cret", raising=False)

    client.cookies.clear()
    r = client.get(
        "/pdf/2026-04-30",
        headers={"Authorization": "Bearer s3cret"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")


def test_pdf_day_wrong_token_falls_back_to_login(client, db_session, monkeypatch):
    _seed_edition(db_session, date(2026, 4, 30))
    from app.settings import get_settings
    monkeypatch.setattr(get_settings(), "pdf_latest_token", "s3cret", raising=False)

    client.cookies.clear()
    # Browser-style request → require_viewer redirects to /login.
    r = client.get(
        "/pdf/2026-04-30?token=wrong",
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code in (307, 302)
    assert "/login" in r.headers.get("location", "")

    # Non-browser request → 401.
    r = client.get("/pdf/2026-04-30?token=wrong", follow_redirects=False)
    assert r.status_code == 401


def test_pdf_latest_does_not_require_login(client, db_session, monkeypatch):
    """Sanity check: hitting /pdf/latest with the right token works without
    any session cookie — confirms no Google login redirect on this route."""
    _seed_latest(db_session, date(2026, 4, 30))
    from app.settings import get_settings
    monkeypatch.setattr(get_settings(), "pdf_latest_token", "s3cret", raising=False)

    # Make absolutely sure we have no session cookie set on the client.
    client.cookies.clear()
    r = client.get(
        "/pdf/latest", headers={"Authorization": "Bearer s3cret"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "/login" not in r.headers.get("location", "")
