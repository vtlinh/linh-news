from __future__ import annotations

from app import main

# ── No-auth access ─────────────────────────────────────────────────────────


def test_sample_home_no_login_required(client):
    """The /sample page must render without a session and never bounce to
    /login — it's the public preview entry point."""
    client.cookies.clear()
    r = client.get("/sample", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 200
    assert "/login" not in r.headers.get("location", "")
    # Frozen-snapshot calendar replacement + sample banner are present.
    assert "Sample calendar events" in r.text
    assert "Edits aren't saved" in r.text


def test_sample_pdf_serves_committed_pdf(client):
    client.cookies.clear()
    r = client.get("/sample/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")


def test_sample_movies_page_and_data(client):
    client.cookies.clear()
    assert client.get("/sample/movies").status_code == 200
    data = client.get("/sample/movies/data").json()
    assert "movies" in data
    assert isinstance(data["movies"], list)


def test_sample_stocks_page(client):
    client.cookies.clear()
    assert client.get("/sample/stocks").status_code == 200


def test_sample_data_page(client):
    client.cookies.clear()
    assert client.get("/sample/data").status_code == 200


# ── Snapshot content invariants ────────────────────────────────────────────


def test_sample_data_has_no_children_and_blank_address():
    """Per the sample spec, the Data tab shows an empty children list and a
    blank address while keeping the section list intact."""
    settings = main._sample_json("data.json")
    assert settings["children"] == []
    assert settings["address"] == ""
    assert settings["sections"], "sections should be pre-populated in the snapshot"


# ── Missing snapshot files degrade to 404, not 500 ─────────────────────────


def test_sample_routes_404_when_not_built(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_SAMPLE_DIR", tmp_path)
    client.cookies.clear()
    assert client.get("/sample").status_code == 404
    assert client.get("/sample/pdf").status_code == 404
    assert client.get("/sample/movies").status_code == 404
