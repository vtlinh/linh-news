from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import patch

from app.db import Edition, GoogleOAuth, HiddenMovie, UserSettings

ADMIN = "vtlinh87@gmail.com"


def _seed_edition(db_session, day: date) -> None:
    db_session.add(
        Edition(
            date=day,
            email=ADMIN,
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
            email=ADMIN,
            html="<!-- WEATHER_PLACEHOLDER --><div>x</div>",
            pdf=b"%PDF-1.4 fake",
            generated_at=datetime.now(UTC),
            weather_forecast_json={
                "today_h": 14,
                "today_l": 7,
                "today_em": "☀️",
                "tomorrow_h": 16,
                "tomorrow_l": 9,
                "tomorrow_em": "☁️",
            },
            weather_alerts_json=["Wind Advisory until 6 PM"],
        )
    )
    db_session.commit()

    login_as("friend@example.com")
    with (
        patch("app.settings.local_today", return_value=date(2026, 5, 2)),
        patch.object(weather, "get_now_cached", return_value="12°C ⛅"),
    ):
        r = client.get("/d/2026-05-02")
    assert r.status_code == 200
    assert "<!-- WEATHER_PLACEHOLDER -->" not in r.text
    assert "Now 12°C ⛅" in r.text
    assert "Today H 14° / L 7° ☀️" in r.text
    assert "⚠ Wind Advisory until 6 PM" in r.text
    # The view-time injector passes the edition's generated_at so the
    # "Refreshed at HH:MM TZ" badge renders on the right of the strip.
    assert 'class="weather-refreshed"' in r.text


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
    edition = db_session.get(Edition, (date(2026, 4, 30), ADMIN))
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
    assert db_session.get(HiddenMovie, (ADMIN, "Frozen 4")) is not None


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
    assert "app.generate" in cmd
    assert "refresh" in cmd
    assert cmd[cmd.index("--email") + 1] == "vtlinh87@gmail.com"


def test_refresh_returns_409_when_lock_held(client, login_as):
    login_as("vtlinh87@gmail.com")
    with (
        patch("app.main.cache.begin_edition_refresh", return_value=False),
        patch("app.main.subprocess.Popen") as popen,
    ):
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


def _set_admin_token(db_session, token: str) -> None:
    """Overwrite the admin's auto-generated pdf_token so tests can predict it."""
    row = db_session.get(UserSettings, ADMIN)
    assert row is not None, "conftest must seed the admin user_settings row"
    row.pdf_token = token
    db_session.commit()


def _seed_latest(db_session, day: date) -> None:
    db_session.add(
        Edition(
            date=day,
            email=ADMIN,
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

    _set_admin_token(db_session, "s3cret")

    r = client.get("/pdf/latest?token=s3cret")
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")

    r = client.get("/pdf/latest?token=wrong")
    assert r.status_code == 401

    r = client.get("/pdf/latest")
    assert r.status_code == 401


def test_pdf_latest_bearer_header(client, db_session, monkeypatch):
    _seed_latest(db_session, date(2026, 4, 30))

    _set_admin_token(db_session, "s3cret")

    r = client.get("/pdf/latest", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")

    r = client.get("/pdf/latest", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_pdf_day_with_token_query(client, db_session, monkeypatch):
    _seed_edition(db_session, date(2026, 4, 30))

    _set_admin_token(db_session, "s3cret")

    client.cookies.clear()
    r = client.get("/pdf/2026-04-30?token=s3cret", follow_redirects=False)
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")


def test_pdf_day_with_bearer_header(client, db_session, monkeypatch):
    _seed_edition(db_session, date(2026, 4, 30))

    _set_admin_token(db_session, "s3cret")

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

    _set_admin_token(db_session, "s3cret")

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


# ── /admin/users ──────────────────────────────────────────────────────────


def _seed_oauth(db_session, email: str, *, personalized: bool = False) -> None:
    db_session.add(
        GoogleOAuth(
            email=email,
            refresh_token="rt",
            client_id="cid",
            client_secret="cs",
            created_at=datetime.now(UTC),
        )
    )
    if personalized:
        row = db_session.get(UserSettings, email)
        if row is not None:
            row.personalized_enabled = True
    db_session.commit()


def test_admin_users_get_admin_only(client, login_as):
    login_as("friend@example.com")
    assert client.get("/admin/users").status_code == 403


def test_admin_users_get_lists_users(client, login_as, db_session):
    _seed_oauth(db_session, "friend@example.com", personalized=True)
    login_as(ADMIN)
    r = client.get("/admin/users")
    assert r.status_code == 200
    assert "friend@example.com" in r.text
    assert ADMIN in r.text


def test_admin_users_add_creates_row(client, login_as, db_session):
    login_as(ADMIN)
    r = client.post(
        "/admin/users/add", json={"email": "New@Example.com", "name": "Newbie"}
    )
    assert r.status_code == 200
    row = db_session.get(UserSettings, "new@example.com")
    assert row is not None
    assert row.display_name == "Newbie"


def test_admin_users_add_rejects_invalid_email(client, login_as):
    login_as(ADMIN)
    r = client.post("/admin/users/add", json={"email": "not-an-email"})
    assert r.status_code == 400


def test_admin_users_add_rejects_duplicate(client, login_as):
    login_as(ADMIN)
    r = client.post("/admin/users/add", json={"email": "friend@example.com"})
    assert r.status_code == 409


def test_admin_users_set_name_blocked_after_signin(client, login_as, db_session):
    _seed_oauth(db_session, "friend@example.com")
    login_as(ADMIN)
    r = client.post(
        "/admin/users/friend@example.com/name", json={"name": "Override"}
    )
    assert r.status_code == 403


def test_admin_users_set_name_works_before_signin(client, login_as, db_session):
    login_as(ADMIN)
    r = client.post(
        "/admin/users/friend@example.com/name", json={"name": "Buddy"}
    )
    assert r.status_code == 200
    assert db_session.get(UserSettings, "friend@example.com").display_name == "Buddy"


def test_admin_users_delete_protects_admin(client, login_as):
    login_as(ADMIN)
    r = client.post(f"/admin/users/{ADMIN}/delete")
    assert r.status_code == 400


def test_admin_users_delete_removes_user_and_oauth(client, login_as, db_session):
    _seed_oauth(db_session, "friend@example.com")
    login_as(ADMIN)
    r = client.post("/admin/users/friend@example.com/delete")
    assert r.status_code == 200
    assert db_session.get(UserSettings, "friend@example.com") is None
    assert db_session.get(GoogleOAuth, "friend@example.com") is None


def test_admin_users_personalized_protects_admin(client, login_as):
    login_as(ADMIN)
    r = client.post(
        f"/admin/users/{ADMIN}/personalized", json={"enabled": False}
    )
    assert r.status_code == 400


def test_admin_users_personalized_works_before_signin(client, login_as, db_session):
    login_as(ADMIN)
    r = client.post(
        "/admin/users/friend@example.com/personalized", json={"enabled": True}
    )
    assert r.status_code == 200
    db_session.expire_all()
    assert (
        db_session.get(UserSettings, "friend@example.com").personalized_enabled is True
    )


def test_admin_users_personalized_unknown_user_404(client, login_as):
    login_as(ADMIN)
    r = client.post(
        "/admin/users/nobody@example.com/personalized", json={"enabled": True}
    )
    assert r.status_code == 404


def test_admin_users_personalized_toggles(client, login_as, db_session):
    _seed_oauth(db_session, "friend@example.com", personalized=False)
    login_as(ADMIN)
    r = client.post(
        "/admin/users/friend@example.com/personalized", json={"enabled": True}
    )
    assert r.status_code == 200
    db_session.expire_all()
    assert (
        db_session.get(UserSettings, "friend@example.com").personalized_enabled is True
    )


def test_admin_users_refresh_requires_signin(client, login_as):
    login_as(ADMIN)
    r = client.post("/admin/users/friend@example.com/refresh")
    assert r.status_code == 404


def test_admin_users_refresh_requires_personalized(client, login_as, db_session):
    _seed_oauth(db_session, "friend@example.com", personalized=False)
    login_as(ADMIN)
    r = client.post("/admin/users/friend@example.com/refresh")
    assert r.status_code == 400


def test_admin_users_refresh_requires_sections(client, login_as, db_session):
    """Personalized + signed-in but no sections configured → 400."""
    _seed_oauth(db_session, "friend@example.com", personalized=True)
    login_as(ADMIN)
    r = client.post("/admin/users/friend@example.com/refresh")
    assert r.status_code == 400


def test_admin_users_refresh_spawns_subprocess(client, login_as, db_session):
    _seed_oauth(db_session, "friend@example.com", personalized=True)
    # Configure at least one section so the refresh guard passes.
    db_session.get(UserSettings, "friend@example.com").sections_json = [
        {"key": "politics", "title": "Politics", "subsection_count": 2}
    ]
    db_session.commit()
    login_as(ADMIN)
    with patch("app.main.subprocess.Popen") as popen:
        popen.return_value.pid = 12345
        r = client.post("/admin/users/friend@example.com/refresh")
    assert r.status_code == 200
    popen.assert_called_once()
    cmd = popen.call_args[0][0]
    assert cmd[-4:] == ["app.generate", "refresh", "--email", "friend@example.com"]


def test_pdf_latest_does_not_require_login(client, db_session, monkeypatch):
    """Sanity check: hitting /pdf/latest with the right token works without
    any session cookie — confirms no Google login redirect on this route."""
    _seed_latest(db_session, date(2026, 4, 30))

    _set_admin_token(db_session, "s3cret")

    # Make absolutely sure we have no session cookie set on the client.
    client.cookies.clear()
    r = client.get(
        "/pdf/latest",
        headers={"Authorization": "Bearer s3cret"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "/login" not in r.headers.get("location", "")
