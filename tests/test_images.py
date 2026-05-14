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


def test_fetch_one_rejects_portrait_only_candidates():
    """Portrait images consume too much vertical space in the PDF flow —
    the renderer drops them entirely rather than fall back."""
    portrait = _png_bytes(100, 400)
    with (
        patch.object(images.random, "shuffle", side_effect=lambda lst: lst),
        patch.object(images, "_download", return_value=portrait),
    ):
        out = images.fetch_one(["https://x/only.png"])
    assert out is None


def test_fetch_one_accepts_square_image():
    square = _png_bytes(300, 300)
    with (
        patch.object(images.random, "shuffle", side_effect=lambda lst: lst),
        patch.object(images, "_download", return_value=square),
    ):
        out = images.fetch_one(["https://x/sq.png"])
    assert out is not None
    assert out.width == out.height


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


def test_fetch_one_returns_sha256_of_raw_bytes():
    """The hash is computed from the raw downloaded bytes — not the
    re-encoded JPEG — so cross-day dedup stays stable across resize/format
    differences."""
    import hashlib

    src = _png_bytes(800, 400)
    expected = hashlib.sha256(src).hexdigest()
    with patch.object(images, "_download", return_value=src):
        out = images.fetch_one(["https://x/a.png"])
    assert out is not None
    assert out.sha256 == expected


def test_fetch_one_skips_rejected_hashes_and_tries_next():
    """A candidate whose raw-bytes sha256 is in ``reject_hashes`` is skipped
    even if it would otherwise be the best match."""
    import hashlib

    banner = _png_bytes(800, 400, color=(0, 0, 0))
    fresh = _png_bytes(900, 450, color=(255, 255, 255))
    banner_hash = hashlib.sha256(banner).hexdigest()

    def fake_download(url: str) -> bytes:
        return banner if "banner" in url else fresh

    with (
        patch.object(images.random, "shuffle", side_effect=lambda lst: lst),
        patch.object(images, "_download", side_effect=fake_download),
    ):
        out = images.fetch_one(
            ["https://x/banner.png", "https://x/fresh.png"],
            reject_hashes={banner_hash},
        )
    assert out is not None
    assert out.sha256 != banner_hash


def test_fetch_one_returns_none_when_only_candidate_is_rejected():
    import hashlib

    src = _png_bytes(800, 400)
    src_hash = hashlib.sha256(src).hexdigest()
    with patch.object(images, "_download", return_value=src):
        out = images.fetch_one(
            ["https://x/a.png"],
            reject_hashes={src_hash},
        )
    assert out is None


def _photo_bytes(width: int, height: int, seed: int = 0, fmt: str = "JPEG") -> bytes:
    """Return raw bytes for a non-trivial test photo. Two encodings of the
    *same* visual content (different JPEG quality, etc.) produce different
    sha256 but the same perceptual hash."""
    img = Image.new("RGB", (width, height))
    px = img.load()
    for y in range(height):
        for x in range(width):
            px[x, y] = ((x + seed) % 256, (y * 2) % 256, ((x + y) // 3) % 256)
    buf = io.BytesIO()
    if fmt == "JPEG":
        img.save(buf, format="JPEG", quality=90)
    else:
        img.save(buf, format=fmt)
    return buf.getvalue()


def test_fetch_one_skips_perceptually_duplicate_candidate():
    """Two candidates that are visually identical but have different raw
    bytes (e.g. same photo re-encoded by another CDN) should not both be
    picked: once one is taken, the other is rejected via phash."""
    a = _photo_bytes(64, 48, seed=0, fmt="JPEG")
    # Re-encode the same pixel layout at a different JPEG quality so raw
    # bytes differ but the perceptual content is the same.
    img = Image.open(io.BytesIO(a))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=60)
    b = buf.getvalue()
    assert a != b  # sanity: bytes differ

    with patch.object(images, "_download", return_value=a):
        first = images.fetch_one(["https://x/a.jpg"])
    assert first is not None

    with patch.object(images, "_download", return_value=b):
        second = images.fetch_one(
            ["https://x/b.jpg"],
            reject_phashes={first.phash},
        )
    assert second is None
