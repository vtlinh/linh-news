from __future__ import annotations

from datetime import date
from unittest.mock import patch

from app import generate
from app.db import Edition


def test_run_upserts_latest_wins(db_session, monkeypatch, tmp_path):
    # Patch news.pr to a tiny stub so we don't read the real one
    pr = tmp_path / "news.pr"
    pr.write_text("Date={{DATE}} Slot={{SLOT}}", encoding="utf-8")
    monkeypatch.setattr(generate.get_settings(), "news_pr_path", pr, raising=False)

    # Stub Claude + WeasyPrint + calendar
    fake_claude = patch.object(
        generate.claude_client,
        "generate_edition",
        return_value={"html": "<p>hi v1</p>", "pdf_html": "<p>pdf v1</p>"},
    )
    fake_pdf = patch.object(generate.pdf, "html_to_pdf", return_value=b"%PDF-v1")
    fake_list = patch.object(generate.calendar_oauth, "list_calendars", return_value=[])
    fake_fetch = patch.object(generate.calendar_oauth, "fetch_events", return_value=[])
    fake_movies = patch.object(
        generate.movies_mod, "get_or_fetch_movies", return_value=[],
    )
    today = date(2026, 4, 30)

    with fake_claude, fake_pdf, fake_list, fake_fetch, fake_movies:
        generate.run("morning", today=today)
        row = db_session.get(Edition, today)
        assert row.html == "<p>hi v1</p>"
        assert row.pdf == b"%PDF-v1"

    fake_claude2 = patch.object(
        generate.claude_client,
        "generate_edition",
        return_value={"html": "<p>hi v2</p>", "pdf_html": "<p>pdf v2</p>"},
    )
    fake_pdf2 = patch.object(generate.pdf, "html_to_pdf", return_value=b"%PDF-v2")
    with fake_claude2, fake_pdf2, fake_list, fake_fetch, fake_movies:
        generate.run("evening", today=today)

    db_session.expire_all()
    row = db_session.get(Edition, today)
    assert row.html == "<p>hi v2</p>"
    assert row.pdf == b"%PDF-v2"


def test_build_context_includes_overlays(db_session, monkeypatch):
    today = date(2026, 4, 30)
    with patch.object(generate.calendar_oauth, "list_calendars", return_value=[]), \
         patch.object(generate.calendar_oauth, "fetch_events", return_value=[]), \
         patch.object(
             generate.movies_mod, "get_or_fetch_movies", return_value=[],
         ):
        ctx = generate._build_context(db_session, today, "evening")
    assert ctx["DATE"] == "2026-04-30"
    assert ctx["WATCHLIST_STOCKS"] == []
    assert ctx["WEATHER_COORDS"]
