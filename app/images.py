"""Pick, download, and resize a single image from a list of LLM-supplied URLs.

The LLM (via web_search) gives us 0..N candidate image URLs per subsection.
We pick one — landscape preferred — download it, decode it, resize so the
longest side fits within ``MAX_WIDTH``, re-encode as JPEG (or keep PNG/WebP
if the source had transparency), and return the bytes. Persistence to the
``subsection_images`` table happens in the caller.

If the chosen URL fails (404, decode error, etc.) we try the next candidate.
"""

from __future__ import annotations

import io
import logging
import random
import urllib.error
import urllib.request
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

log = logging.getLogger(__name__)

MAX_WIDTH = 400
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


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:  # noqa: S310
        return r.read()


def _resize(img: Image.Image) -> Image.Image:
    if img.width <= MAX_WIDTH:
        return img
    ratio = MAX_WIDTH / img.width
    new_size = (MAX_WIDTH, max(1, round(img.height * ratio)))
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


def _try_one(url: str) -> tuple[Image.Image, bool] | None:
    """Download + decode a single URL. Returns (image, is_landscape) or None
    on any failure."""
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
    return img, img.width >= img.height


def fetch_one(urls: list[str]) -> FetchedImage | None:
    """Return one resized image for the subsection, or None if every
    candidate failed.

    Strategy: shuffle the candidates so repeated runs vary; then iterate.
    Prefer landscape — if the first successful decode is portrait, keep
    trying the rest looking for a landscape one. Fall back to the portrait
    candidate if no landscape candidate succeeds.
    """
    if not urls:
        return None
    candidates = list(urls)
    random.shuffle(candidates)

    portrait_fallback: Image.Image | None = None
    chosen: Image.Image | None = None
    for url in candidates:
        result = _try_one(url)
        if result is None:
            continue
        img, is_landscape = result
        if is_landscape:
            chosen = img
            break
        if portrait_fallback is None:
            portrait_fallback = img
    if chosen is None:
        chosen = portrait_fallback
    if chosen is None:
        return None

    chosen = _resize(chosen)
    data, mime = _encode(chosen)
    return FetchedImage(
        bytes_=data,
        mime_type=mime,
        width=chosen.width,
        height=chosen.height,
    )


def _resize_to(img: Image.Image, max_w: int) -> Image.Image:
    if img.width <= max_w:
        return img
    new_size = (max_w, max(1, round(img.height * max_w / img.width)))
    return img.resize(new_size, Image.Resampling.LANCZOS)


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
