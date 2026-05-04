from __future__ import annotations

import io
from unittest.mock import patch

from app import og_image


class _FakeResponse:
    def __init__(self, body: bytes, url: str):
        self._buf = io.BytesIO(body)
        self.url = url

    def read(self, n: int | None = None) -> bytes:
        return self._buf.read(n) if n is not None else self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._buf.close()


def _patch_urlopen(html: str, final_url: str = "https://site.example/article/1"):
    return patch.object(
        og_image.urllib.request,
        "urlopen",
        return_value=_FakeResponse(html.encode("utf-8"), final_url),
    )


def test_skips_homepage_url_without_fetching():
    """Homepage URLs (no path segments) should never trigger a network
    fetch — those og:image values are site banners, not story-specific."""
    with patch.object(og_image.urllib.request, "urlopen") as m:
        result = og_image.fetch_og_image("https://nytimes.com/")
    assert result is None
    assert m.call_count == 0


def test_skips_bare_domain_without_fetching():
    with patch.object(og_image.urllib.request, "urlopen") as m:
        result = og_image.fetch_og_image("https://nytimes.com")
    assert result is None
    assert m.call_count == 0


def test_returns_url_title_and_alt_for_article_page():
    html = """<html><head>
        <title>Fallback Title</title>
        <meta property="og:image" content="https://cdn.example/img.jpg">
        <meta property="og:title" content="Senate Passes Climate Bill">
        <meta property="og:image:alt" content="Senators voting in chamber">
        </head><body>x</body></html>"""
    with _patch_urlopen(html):
        og = og_image.fetch_og_image("https://site.example/article/1")
    assert og is not None
    assert og.url == "https://cdn.example/img.jpg"
    assert og.page_title == "Senate Passes Climate Bill"
    assert og.alt == "Senators voting in chamber"


def test_falls_back_to_html_title_tag_when_no_og_title():
    html = """<html><head>
        <title>Fallback Title</title>
        <meta property="og:image" content="https://cdn.example/img.jpg">
        </head><body>x</body></html>"""
    with _patch_urlopen(html):
        og = og_image.fetch_og_image("https://site.example/article/1")
    assert og is not None
    assert og.page_title == "Fallback Title"
    assert og.alt == ""


def test_resolves_relative_image_url_against_final_url():
    html = """<html><head>
        <meta property="og:image" content="/static/hero.jpg">
        </head></html>"""
    with _patch_urlopen(html, final_url="https://site.example/news/123"):
        og = og_image.fetch_og_image("https://site.example/news/123")
    assert og is not None
    assert og.url == "https://site.example/static/hero.jpg"


def test_returns_none_when_no_og_image():
    html = "<html><head><title>x</title></head></html>"
    with _patch_urlopen(html):
        og = og_image.fetch_og_image("https://site.example/article/1")
    assert og is None


def test_accepts_twitter_image_as_fallback():
    html = """<html><head>
        <meta name="twitter:image" content="https://cdn.example/t.jpg">
        </head></html>"""
    with _patch_urlopen(html):
        og = og_image.fetch_og_image("https://site.example/article/1")
    assert og is not None
    assert og.url == "https://cdn.example/t.jpg"
