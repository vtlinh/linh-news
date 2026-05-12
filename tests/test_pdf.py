from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app import pdf as pdf_mod
from app.pdf import (
    _FONT_CACHE_V1_KEY,
    _FONT_CACHE_V2_KEY,
    PdfSkipped,
    _drop_one_article,
    _drop_one_section,
    _load_font_samples,
    _predict_font,
    _save_font_sample,
    html_to_pdf_ex,
)
from app.pdf_renderer import build_pdf_parts


class _FakeBackend:
    """Minimal in-memory kv backend matching app.cache._get_backend()."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str):
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value


def _patch_backend(backend: _FakeBackend):
    """Patch the cache backend that pdf.py reaches into."""
    from app import cache as cache_mod

    return patch.object(cache_mod, "_get_backend", return_value=backend)


def test_font_cache_v1_migrates_to_v2_on_first_read():
    backend = _FakeBackend()
    # Seed an old-shape v1 cache (flat list of [words, pt] samples).
    backend.data[_FONT_CACHE_V1_KEY] = json.dumps([[100, 12.0], [500, 14.0], [1200, 16.5]])
    with _patch_backend(backend):
        # First read for any region triggers the migration.
        samples = _load_font_samples("upper-news")
    assert samples == [(100, 12.0), (500, 14.0), (1200, 16.5)]
    # v2 now exists and is keyed by all three regions.
    v2 = json.loads(backend.data[_FONT_CACHE_V2_KEY])
    assert set(v2.keys()) == {"upper-news", "lower-news", "rail"}
    for region in ("upper-news", "lower-news", "rail"):
        assert [list(t) for t in v2[region]] == [[100, 12.0], [500, 14.0], [1200, 16.5]]
    # v1 is tombstoned (set to empty string) so subsequent reads skip it.
    assert backend.data[_FONT_CACHE_V1_KEY] == ""


def test_font_cache_save_buckets_by_100_words_per_region():
    backend = _FakeBackend()
    with _patch_backend(backend):
        _save_font_sample("rail", 110, 12.0)
        _save_font_sample("rail", 195, 13.0)  # same 100-bucket → replaces 110
        _save_font_sample("upper-news", 110, 14.0)  # different region; coexists
        rail = _load_font_samples("rail")
        upper = _load_font_samples("upper-news")
    assert rail == [(195, 13.0)]
    assert upper == [(110, 14.0)]


def test_predict_font_interpolates_per_region():
    backend = _FakeBackend()
    with _patch_backend(backend):
        _save_font_sample("upper-news", 100, 10.0)
        _save_font_sample("upper-news", 1000, 18.0)
        # Midpoint: ~(550 - 100) / (1000 - 100) = 0.5 → 14pt
        guess = _predict_font("upper-news", 550)
    assert 13.5 <= guess <= 14.5
    with _patch_backend(backend):
        # No samples for rail → default guess.
        guess_rail = _predict_font("rail", 500)
    assert guess_rail == pdf_mod._DEFAULT_FONT_GUESS


# ── Drop logic (band-flat HTML) ───────────────────────────────────────────


def _band_html() -> str:
    return (
        '<section class="news-header"><h2>World</h2></section>'
        '<section class="news-story"><h3>A</h3><p>x</p></section>'
        '<div class="sep-story"></div>'
        '<section class="news-story"><h3>B</h3><p>y</p></section>'
        '<div class="sep-group"></div>'
        '<section class="news-header"><h2>Movies</h2></section>'
        '<section class="news-story"><h3>C</h3><p>z</p></section>'
    )


def test_drop_one_article_removes_last_story_of_largest_section():
    out = _drop_one_article(_band_html())
    assert out is not None
    # Should remove story "B" (last of the 2-story "World" section), along
    # with its preceding sep-story rule.
    assert "B" not in out
    assert "A" in out
    # The sep-story rule between A and B is gone too.
    assert out.count('class="sep-story"') == 0


def test_drop_one_article_returns_none_when_every_section_has_one_story():
    one_story = (
        '<section class="news-header"><h2>A</h2></section>'
        '<section class="news-story"><h3>a</h3><p>1</p></section>'
        '<div class="sep-group"></div>'
        '<section class="news-header"><h2>B</h2></section>'
        '<section class="news-story"><h3>b</h3><p>2</p></section>'
    )
    assert _drop_one_article(one_story) is None


def test_drop_one_section_kills_lowest_priority_first():
    # "Movies" matches the first drop-priority pattern → dropped first.
    out = _drop_one_section(_band_html())
    assert out is not None
    assert "Movies" not in out
    assert "World" in out
    # The sep-group rule preceding Movies is also gone.
    assert out.count('class="sep-group"') == 0


# ── Pipeline integration: 1-section raises PdfSkipped ────────────────────


def _one_section_parts():
    return build_pdf_parts(
        {
            "sections": [
                {
                    "key": "world",
                    "title": "World",
                    "subsections": [{"title": "h", "text": "body words", "sources": []}],
                }
            ],
            "stocks": [],
        },
        pdf_calendar_html="",
        pdf_movies_html="",
        weather_strip_html="",
        today=__import__("datetime").date(2026, 5, 3),
    )


def test_html_to_pdf_ex_skips_when_exactly_one_section():
    parts = _one_section_parts()
    with pytest.raises(PdfSkipped):
        html_to_pdf_ex(parts)
