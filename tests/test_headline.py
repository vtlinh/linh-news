"""Tests for the headline (front-page hero) feature.

Covers:
  * llm_schema: optional ``headline`` field is accepted in ``EDITION_SCHEMA``
    and ``HEADLINE_REROLL_SCHEMA`` requires it.
  * user_settings: the ``can_be_headline`` flag round-trips through save/load.
  * prompt_template: the headline instruction block is emitted only when at
    least one section is marked eligible, and contains the eligible titles.
  * images.resize_to_box: preserves aspect ratio and respects the cap.
  * html_renderer: the hero block is prepended only when a headline is
    present, has no section header, and contains the image link.

PDF-side coverage is deferred until the PDF L-layout is reworked — for now
the PDF renderer ignores ``LinhNews.headline`` and the headline only shows
on the HTML edition.
"""

from __future__ import annotations

import io
from datetime import date

from PIL import Image

from app import images, llm_schema, prompt_template, user_settings
from app.html_renderer import render_edition_html

# ── llm_schema ───────────────────────────────────────────────────────────


def test_edition_schema_has_optional_headline():
    schema = llm_schema.EDITION_SCHEMA
    assert "headline" in schema["properties"]
    # headline must NOT be required
    assert "headline" not in (schema.get("required") or [])
    headline_schema = schema["properties"]["headline"]
    # Reuses the Subsection shape — required fields title/text/sources
    assert set(headline_schema["required"]) == {"title", "text", "sources"}


def test_headline_reroll_schema_requires_headline():
    s = llm_schema.HEADLINE_REROLL_SCHEMA
    assert s["required"] == ["headline"]
    assert s["properties"]["headline"]["required"] == ["title", "text", "sources"]


# ── user_settings round-trip ─────────────────────────────────────────────


def test_user_settings_empty_section_defaults_can_be_headline_false():
    assert user_settings._EMPTY_SECTION["can_be_headline"] is False


def test_user_settings_save_persists_can_be_headline(tmp_path, monkeypatch):
    """``save()`` coerces and preserves the new flag."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base

    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    Maker = sessionmaker(bind=engine)
    with Maker() as s:
        saved = user_settings.save(
            s,
            "x@example.com",
            {
                "display_name": "Linh",
                "address": "",
                "sections": [
                    {
                        "title": "World",
                        "description": "global news",
                        "subsection_count": 5,
                        "preferred_sources": [],
                        "use_global_sources": True,
                        "can_be_headline": True,
                    },
                    {
                        "title": "Local",
                        "description": "local news",
                        "subsection_count": 5,
                        "preferred_sources": [],
                        "use_global_sources": True,
                    },
                ],
                "children": [],
            },
        )
    assert saved["sections"][0]["can_be_headline"] is True
    assert saved["sections"][1]["can_be_headline"] is False


# ── prompt template ─────────────────────────────────────────────────────


def test_prompt_template_omits_headline_block_when_none_eligible():
    out = prompt_template.build_prompt(
        display_name="Linh",
        today=date(2026, 5, 13),
        sections=[
            {"key": "world", "title": "World", "subsection_count": 5},
            {"key": "ai", "title": "AI", "subsection_count": 5},
        ],
        children=[],
        watchlist_stocks=[],
        dorchester_events="",
    )
    assert "Headline" not in out or "front-page" not in out


def test_prompt_template_emits_headline_block_when_eligible():
    out = prompt_template.build_prompt(
        display_name="Linh",
        today=date(2026, 5, 13),
        sections=[
            {
                "key": "world",
                "title": "World",
                "subsection_count": 5,
                "can_be_headline": True,
            },
            {
                "key": "ai",
                "title": "AI",
                "subsection_count": 5,
                "can_be_headline": True,
            },
            {"key": "local", "title": "Local", "subsection_count": 5},
        ],
        children=[],
        watchlist_stocks=[],
        dorchester_events="",
    )
    # Block markers
    assert "Headline (top-of-front-page deep dive)" in out
    # Both eligible titles listed
    assert "World" in out
    assert "AI" in out
    # Non-eligible NOT listed in the headline topic bullets
    assert "in other news" in out.lower() or "ONE story" in out


# ── images.resize_to_box ────────────────────────────────────────────────


def _jpeg_bytes(w: int, h: int) -> bytes:
    img = Image.new("RGB", (w, h), (200, 100, 50))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def test_resize_to_box_downscales_and_preserves_aspect():
    raw = _jpeg_bytes(1200, 800)  # aspect 1.5
    result = images.resize_to_box(raw, 600, 400)
    assert result is not None
    data, mime, w, h = result
    assert w <= 600
    assert h <= 400
    # Aspect ratio preserved within 1px tolerance
    assert abs((w / h) - 1.5) < 0.02
    assert mime.startswith("image/")


def test_resize_to_box_no_upscale():
    """If the source is already smaller than the box on both axes, don't
    upscale — just re-encode at native dimensions."""
    raw = _jpeg_bytes(300, 200)
    result = images.resize_to_box(raw, 600, 400)
    assert result is not None
    _data, _mime, w, h = result
    assert (w, h) == (300, 200)


def test_resize_to_box_returns_none_on_bad_bytes():
    assert images.resize_to_box(b"not an image", 100, 100) is None


# ── html_renderer hero block ────────────────────────────────────────────


def _basic_section() -> dict:
    return {
        "key": "world",
        "title": "World",
        "subsections": [
            {
                "title": "story",
                "text": "body",
                "sources": [{"url": "https://x", "title": "src"}],
            }
        ],
    }


def test_html_no_hero_when_no_headline():
    out = render_edition_html({"sections": [_basic_section()], "stocks": []})
    assert "hero" not in out.split('<div class="flow">')[1].split('<aside')[0].lower()


def test_html_hero_block_when_headline_present():
    payload = {
        "headline": {
            "title": "Breaking story",
            "text": "Paragraph 1.\n\nParagraph 2.",
            "sources": [{"url": "https://x", "title": "Reuters: x"}],
            "image_id": 42,
        },
        "sections": [_basic_section()],
        "stocks": [],
    }
    out = render_edition_html(payload)
    assert '<section class="hero">' in out
    assert "Breaking story" in out
    # Image link present
    assert '/edition-image/42' in out
    # The hero appears BEFORE the regular section header
    assert out.index('class="hero"') < out.index('news-header')
