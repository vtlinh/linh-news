from __future__ import annotations

import io
from unittest.mock import patch

from PIL import Image

from app import images


def _png_bytes(width: int, height: int, color=(255, 0, 0)) -> bytes:
    """Return raw PNG bytes for a solid-colour image of the given size."""
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_fetch_one_returns_none_for_empty_list():
    assert images.fetch_one([]) is None


def test_fetch_one_resizes_landscape_to_max_width():
    src = _png_bytes(1200, 600)  # landscape, > MAX_WIDTH
    with patch.object(images, "_download", return_value=src):
        out = images.fetch_one(["https://x/a.png"])
    assert out is not None
    assert out.width == images.MAX_WIDTH
    # Aspect ratio (2:1) preserved.
    assert out.height == images.MAX_WIDTH // 2


def test_fetch_one_keeps_small_images_unmodified_size():
    src = _png_bytes(200, 100)
    with patch.object(images, "_download", return_value=src):
        out = images.fetch_one(["https://x/a.png"])
    assert out is not None
    assert out.width == 200
    assert out.height == 100


def test_fetch_one_prefers_landscape_over_portrait():
    """Given one portrait + one landscape candidate, the landscape one wins
    even when shuffled. We pin the shuffle so the test is deterministic."""
    portrait = _png_bytes(100, 400, color=(0, 0, 255))
    landscape = _png_bytes(800, 200, color=(0, 255, 0))

    def fake_download(url: str) -> bytes:
        return portrait if "p.png" in url else landscape

    # Force the portrait URL first so the function MUST keep going to find
    # the landscape candidate (otherwise it would settle on portrait).
    with (
        patch.object(images.random, "shuffle", side_effect=lambda lst: lst),
        patch.object(images, "_download", side_effect=fake_download),
    ):
        out = images.fetch_one(["https://x/p.png", "https://x/l.png"])
    assert out is not None
    # Landscape ratio should survive — width > height after resize.
    assert out.width >= out.height


def test_fetch_one_falls_back_to_portrait_when_no_landscape():
    portrait = _png_bytes(100, 400)
    with (
        patch.object(images.random, "shuffle", side_effect=lambda lst: lst),
        patch.object(images, "_download", return_value=portrait),
    ):
        out = images.fetch_one(["https://x/only.png"])
    assert out is not None
    assert out.height > out.width


def test_fetch_one_skips_failed_download_and_tries_next():
    good = _png_bytes(800, 400)

    calls: list[str] = []

    def fake_download(url: str) -> bytes:
        calls.append(url)
        if url.endswith("bad"):
            raise OSError("network down")
        return good

    with (
        patch.object(images.random, "shuffle", side_effect=lambda lst: lst),
        patch.object(images, "_download", side_effect=fake_download),
    ):
        out = images.fetch_one(["https://x/bad", "https://x/good.png"])
    assert out is not None
    assert calls == ["https://x/bad", "https://x/good.png"]


def test_fetch_one_returns_none_when_all_fail():
    with patch.object(images, "_download", side_effect=OSError("nope")):
        out = images.fetch_one(["https://x/a", "https://x/b"])
    assert out is None
