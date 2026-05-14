"""Pick, download, and resize a single image from a list of LLM-supplied URLs.

The LLM (via web_search) gives us 0..N candidate image URLs per subsection.
We pick one — landscape preferred — download it, decode it, resize so the
longest side fits within ``MAX_WIDTH``, re-encode as JPEG (or keep PNG/WebP
if the source had transparency), and return the bytes. Persistence to the
``subsection_images`` table happens in the caller.

If the chosen URL fails (404, decode error, etc.) we try the next candidate.
"""

from __future__ import annotations

import hashlib
import io
import logging
import random
import urllib.error
import urllib.request
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

log = logging.getLogger(__name__)

MAX_WIDTH = 400
# Larger cap for the front-page headline image: the PDF can place it at up
# to half the news-area width (≈5.95in at 96 PPI ≈ 572px), so we need
# enough source pixels to scale down crisply without artifacts.
HEADLINE_MAX_WIDTH = 1200
TIMEOUT_SECONDS = 8
_USER_AGENT = "Linh-News/1.0 (vtlinh87+linhnews@gmail.com)"

# Width of a single news-flow column in the PDF (15.296in page − 0.8in
# margins − 2.4in rail − 0.194in content gap = 11.902in flow width;
# 4 cols with 14pt gaps = 2.83in per col). Resizing every PDF-bound
# image to this max width — instead of letting WeasyPrint shrink via
# CSS width:100% — keeps WeasyPrint's intrinsic-sizing from inflating
# the rail (a 300px backdrop sometimes wins over the rail's 2.4in flex
# basis, which over-trims movies in Phase 1).
PDF_COLUMN_WIDTH_PX = 272  # 2.83in × 96 PPI

# WebP and animated formats convert to a single JPEG frame; transparency-bearing
# PNGs keep PNG to preserve alpha. Anything else encodes as JPEG.
_KEEP_FORMATS = {"PNG"}


@dataclass(frozen=True)
class FetchedImage:
    bytes_: bytes
    mime_type: str
    width: int
    height: int
    sha256: str
    # 64-bit perceptual hash (difference hash) of the decoded image. Used to
    # detect visually-identical images that have different raw bytes (e.g. the
    # same photo served by two different CDNs / re-encoded by the publisher),
    # which raw-byte sha256 can't catch.
    phash: int


def _dhash(img: Image.Image, size: int = 8) -> int:
    """8x8 difference hash. Two images with hamming distance 0 are visually
    identical for our purposes; small distances (≤5) usually indicate the
    same image with minor re-encoding or cropping differences."""
    g = img.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    px = list(g.getdata())
    bits = 0
    for r in range(size):
        for c in range(size):
            i = r * (size + 1) + c
            bits = (bits << 1) | (1 if px[i] > px[i + 1] else 0)
    return bits


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:  # noqa: S310
        return r.read()


def _resize(img: Image.Image, max_width: int = MAX_WIDTH) -> Image.Image:
    if img.width <= max_width:
        return img
    ratio = max_width / img.width
    new_size = (max_width, max(1, round(img.height * ratio)))
    return img.resize(new_size, Image.Resampling.LANCZOS)


def _encode(img: Image.Image) -> tuple[bytes, str]:
    """Re-encode the (possibly resized) image. Returns (bytes, mime_type)."""
    keep_png = (img.format in _KEEP_FORMATS) and (img.mode in ("RGBA", "LA", "P"))
    buf = io.BytesIO()
    if keep_png:
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), "image/png"
    # JPEG can't carry alpha — flatten on white.
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    img.save(buf, format="JPEG", quality=82, optimize=True, progressive=True)
    return buf.getvalue(), "image/jpeg"


def _try_one(url: str) -> tuple[Image.Image, bool, str, int] | None:
    """Download + decode a single URL. Returns (image, is_landscape, sha256,
    phash) — sha256 is of the *raw* downloaded bytes (stable for cross-day
    dedup across Pillow versions); phash is a perceptual dhash of the decoded
    image (catches visually-identical images served as different bytes).
    Returns None on any failure."""
    if not url:
        return None
    try:
        raw = _download(url)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.info("Image download failed (%s): %s", url, e)
        return None
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()  # decode now so corrupt files fail here, not later
    except (UnidentifiedImageError, OSError) as e:
        log.info("Image decode failed (%s): %s", url, e)
        return None
    return img, img.width >= img.height, hashlib.sha256(raw).hexdigest(), _dhash(img)


def fetch_one(
    urls: list[str],
    reject_hashes: set[str] | None = None,
    *,
    max_width: int = MAX_WIDTH,
    prefer_widest: bool = False,
    reject_phashes: set[int] | None = None,
    phash_threshold: int = 5,
) -> FetchedImage | None:
    """Return one resized image for the subsection, or None if every
    candidate failed.

    Strategy: shuffle the candidates so repeated runs vary; then iterate
    looking for a landscape-or-square image. Portrait candidates are
    skipped — they consume too much vertical space in the PDF flow and
    have caused single-page overflow even after maximum news trimming.

    ``reject_hashes`` (sha256 of raw downloaded bytes) skips candidates
    whose bytes match any prior edition's image — used to suppress generic
    site banners that recur day after day.

    ``reject_phashes`` (perceptual dhash ints) skips candidates that are
    visually identical (or near-identical, within ``phash_threshold``
    hamming bits) to any hash in the set — used to avoid two subsections
    in the same edition showing the same photo when it's served by
    different CDNs / re-encoded slightly.
    """
    if not urls:
        return None
    candidates = list(urls)
    random.shuffle(candidates)

    def _phash_dup(ph: int) -> bool:
        if not reject_phashes:
            return False
        return any(_hamming(ph, prev) <= phash_threshold for prev in reject_phashes)

    chosen: tuple[Image.Image, str, int] | None = None
    if prefer_widest:
        # Try ALL candidates, collect every landscape one that isn't a
        # reject-hash dup, then pick the widest (highest aspect = w/h).
        # Used by the headline image so the hero gets a wide cinematic
        # crop instead of a near-square thumbnail.
        viable: list[tuple[float, Image.Image, str, int]] = []
        for url in candidates:
            result = _try_one(url)
            if result is None:
                continue
            img, is_landscape, sha, ph = result
            if not is_landscape:
                log.info("Image rejected — portrait orientation: %s", url)
                continue
            if reject_hashes and sha in reject_hashes:
                log.info(
                    "Image rejected — hash seen on prior day (%s): %s",
                    sha[:12], url,
                )
                continue
            if _phash_dup(ph):
                log.info("Image rejected — perceptually duplicates a prior pick: %s", url)
                continue
            aspect = img.width / max(1, img.height)
            viable.append((aspect, img, sha, ph))
        if viable:
            viable.sort(key=lambda v: -v[0])  # widest first
            _aspect, img, sha, ph = viable[0]
            log.info(
                "Image picked: aspect %.2f (best of %d landscape candidates)",
                _aspect, len(viable),
            )
            chosen = (img, sha, ph)
    else:
        for url in candidates:
            result = _try_one(url)
            if result is None:
                continue
            img, is_landscape, sha, ph = result
            if not is_landscape:
                log.info("Image rejected — portrait orientation: %s", url)
                continue
            if reject_hashes and sha in reject_hashes:
                log.info("Image rejected — hash seen on prior day (%s): %s", sha[:12], url)
                continue
            if _phash_dup(ph):
                log.info("Image rejected — perceptually duplicates a prior pick: %s", url)
                continue
            chosen = (img, sha, ph)
            break
    if chosen is None:
        return None

    img, sha, ph = chosen
    img = _resize(img, max_width=max_width)
    data, mime = _encode(img)
    return FetchedImage(
        bytes_=data,
        mime_type=mime,
        width=img.width,
        height=img.height,
        sha256=sha,
        phash=ph,
    )


def _resize_to(img: Image.Image, max_w: int) -> Image.Image:
    if img.width <= max_w:
        return img
    new_size = (max_w, max(1, round(img.height * max_w / img.width)))
    return img.resize(new_size, Image.Resampling.LANCZOS)


def resize_to_box(
    image_bytes: bytes, max_w_px: int, max_h_px: int
) -> tuple[bytes, str, int, int] | None:
    """Decode → downscale to fit inside ``(max_w_px × max_h_px)`` preserving
    aspect ratio → re-encode. Returns ``(bytes, mime, width, height)`` or
    ``None`` when Pillow can't decode the source.

    Never upscales: if the image is already smaller than the box on both
    axes, returns the re-encoded original at its native size.
    """
    if max_w_px <= 0 or max_h_px <= 0:
        return None
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        log.info("Image decode failed in resize_to_box: %s", e)
        return None
    w, h = img.width, img.height
    if w == 0 or h == 0:
        return None
    scale = min(max_w_px / w, max_h_px / h, 1.0)
    if scale < 1.0:
        new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
        img = img.resize(new_size, Image.Resampling.LANCZOS)
    data, mime = _encode(img)
    return data, mime, img.width, img.height


def resize_for_pdf(
    image_bytes: bytes, max_width_px: int = PDF_COLUMN_WIDTH_PX
) -> tuple[bytes, str] | None:
    """Decode → downscale → re-encode an image for PDF embedding.

    Returns ``(bytes, mime_type)`` capped at ``max_width_px`` wide. Returns
    ``None`` if Pillow can't decode the bytes."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        log.info("PDF image decode failed: %s", e)
        return None
    return _encode(_resize_to(img, max_width_px))


def fetch_for_pdf(url: str, max_width_px: int = PDF_COLUMN_WIDTH_PX) -> tuple[bytes, str] | None:
    """Download ``url`` and return ``(bytes, mime_type)`` resized for PDF
    embedding. Returns ``None`` on any download/decode failure."""
    try:
        raw = _download(url)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.info("PDF image download failed (%s): %s", url, e)
        return None
    return resize_for_pdf(raw, max_width_px)


def to_data_uri(image_bytes: bytes, mime_type: str) -> str:
    """Encode ``image_bytes`` as a base64 ``data:`` URL."""
    import base64
    return f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
