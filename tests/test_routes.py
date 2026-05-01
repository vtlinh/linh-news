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


def test_refresh_runs_pipeline(client, login_as):
    login_as("friend@example.com")
    today = date.today()
    with patch("app.main.generate.run", return_value=today) as run:
        r = client.post("/refresh")
    assert r.status_code == 200
    run.assert_called_once()
