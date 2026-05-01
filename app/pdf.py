from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import sys

log = logging.getLogger(__name__)

# ── Word-count → fitting-font cache ─────────────────────────────────────
# A small list of (word_count, font_pt) samples persisted in kv_cache. We
# linearly interpolate between the two nearest samples to predict the font
# size that should fit a new render. The cache is bounded so we don't grow
# without limit.
_FONT_CACHE_KEY = "linh_news:pdf_font_cache"
_FONT_CACHE_MAX_SAMPLES = 30
_FONT_MIN = 5.0
_FONT_MAX = 24.0
_FONT_STEP = 0.5  # render fonts at 0.5pt resolution
_DEFAULT_FONT_GUESS = 12.0
_BLANK_TARGET = 0.20  # tolerate up to 20% blank space


def _round_half(x: float) -> float:
    """Round to nearest 0.5pt (used for the bumped/fallback attempts)."""
    return round(x / _FONT_STEP) * _FONT_STEP


def _floor_half(x: float) -> float:
    """Round DOWN to nearest 0.5pt — used for the first attempt so the
    'predicted - 0.2pt' conservative margin isn't lost to rounding."""
    return math.floor(x / _FONT_STEP) * _FONT_STEP


def _count_words(html: str) -> int:
    """Word count of the body text only (strip HTML tags first)."""
    text = re.sub(r"<[^>]+>", " ", html)
    return len(text.split())


def _load_font_samples() -> list[tuple[int, float]]:
    try:
        from app import cache
        raw = cache._get_backend().get(_FONT_CACHE_KEY)  # noqa: SLF001
        if not raw:
            return []
        items = json.loads(raw)
        out: list[tuple[int, float]] = []
        for it in items:
            try:
                w, f = int(it[0]), float(it[1])
                if w > 0 and _FONT_MIN <= f <= _FONT_MAX:
                    out.append((w, f))
            except (TypeError, ValueError, IndexError):
                continue
        return out
    except Exception:  # noqa: BLE001 — cache read should never block PDF gen
        log.exception("Could not load PDF font cache; starting from empty")
        return []


def _save_font_sample(word_count: int, font_pt: float) -> None:
    try:
        from app import cache
        samples = _load_font_samples()
        # Bucket by 100 words: latest sample in each bucket wins. Keeps
        # entries diverse without unbounded growth.
        bucket = (word_count // 100) * 100
        samples = [s for s in samples if (s[0] // 100) * 100 != bucket]
        samples.append((word_count, font_pt))
        samples.sort(key=lambda s: s[0])
        if len(samples) > _FONT_CACHE_MAX_SAMPLES:
            samples = samples[-_FONT_CACHE_MAX_SAMPLES:]
        cache._get_backend().set(  # noqa: SLF001
            _FONT_CACHE_KEY, json.dumps(samples)
        )
    except Exception:  # noqa: BLE001
        log.exception("Could not save PDF font cache sample")


def _predict_font(word_count: int, samples: list[tuple[int, float]]) -> float:
    """Linear-interpolate font size from cached samples."""
    if not samples:
        return _DEFAULT_FONT_GUESS
    s = sorted(samples, key=lambda x: x[0])
    if word_count <= s[0][0]:
        return s[0][1]
    if word_count >= s[-1][0]:
        return s[-1][1]
    for i in range(len(s) - 1):
        wa, fa = s[i]
        wb, fb = s[i + 1]
        if wa <= word_count <= wb:
            t = (word_count - wa) / max(wb - wa, 1)
            return fa + (fb - fa) * t
    return s[-1][1]


def _ensure_dll_path() -> None:
    """On Windows, Python 3.8+ does not load DLLs from PATH. WeasyPrint needs
    Pango/Cairo/etc. — explicitly add common GTK runtime locations so cffi
    can resolve them. No-op on non-Windows."""
    if sys.platform != "win32":
        return
    candidates = [
        r"C:\msys64\ucrt64\bin",
        r"C:\msys64\mingw64\bin",
        r"C:\Program Files\GTK3-Runtime Win64\bin",
    ]
    for d in candidates:
        if os.path.isdir(d):
            with contextlib.suppress(OSError, FileNotFoundError):
                os.add_dll_directory(d)


# Minimal valid one-page PDF used as a placeholder when the real renderer is
# unavailable (e.g. local dev on Windows without GTK/Pango). Production
# always has WeasyPrint's native deps installed via the Dockerfile.
_PLACEHOLDER_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj <<>> endobj\n"
    b"2 0 obj << /Type /Catalog /Pages 3 0 R >> endobj\n"
    b"3 0 obj << /Type /Pages /Count 1 /Kids [4 0 R] >> endobj\n"
    b"4 0 obj << /Type /Page /Parent 3 0 R /MediaBox [0 0 612 792] >> endobj\n"
    b"trailer << /Root 2 0 R >>\n%%EOF\n"
)

# Per news.pr spec: sections to drop first when content is too dense.
# Each entry is a regex matched against the <h2> text of the section.
# Lowest priority first.
_DROP_PRIORITY = [
    re.compile(r"movie|film",                    re.IGNORECASE),
    re.compile(r"dorchester|school|elementary",  re.IGNORECASE),
    re.compile(r"financial|finance",             re.IGNORECASE),
    re.compile(r"\bai\b|artificial intelligence|coding ai", re.IGNORECASE),
    re.compile(r"nj|new jersey|new york",        re.IGNORECASE),
    re.compile(r"us\s+political|united states",  re.IGNORECASE),
    re.compile(r"global|world|international",    re.IGNORECASE),
]

# Matches a complete <section>...</section> block (handles nested tags crudely
# but well enough for the flat structures Claude typically produces).
_SECTION_RE = re.compile(
    r"<section(?:\s[^>]*)?>.*?</section\s*>",
    re.IGNORECASE | re.DOTALL,
)
_H2_TEXT_RE = re.compile(r"<h2[^>]*>(.*?)</h2\s*>", re.IGNORECASE | re.DOTALL)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")


def _drop_one_section(html: str) -> str | None:
    """Remove the single lowest-priority droppable section from the PDF HTML.
    Returns the trimmed HTML, or None if nothing was dropped."""
    sections = list(_SECTION_RE.finditer(html))
    if not sections:
        return None

    def _h2_text(sec_html: str) -> str:
        m = _H2_TEXT_RE.search(sec_html)
        return _TAG_STRIP_RE.sub("", m.group(1)).strip().lower() if m else ""

    # Try each drop-priority pattern in order.
    for pattern in _DROP_PRIORITY:
        for sec in reversed(sections):  # last matching section wins (keep lead)
            if pattern.search(_h2_text(sec.group())):
                log.info("PDF: dropping section %r to fit one page",
                         _h2_text(sec.group())[:60])
                return html[: sec.start()] + html[sec.end():]

    # No pattern matched — drop the very last section as a last resort.
    last = sections[-1]
    log.info("PDF: dropping last section (no priority match) to fit one page")
    return html[: last.start()] + html[last.end():]


def html_to_pdf(html: str) -> bytes:
    """Render print-styled HTML to a single-page 12×22 in PDF using WeasyPrint.

    Smart cache-driven algorithm (typical: 1 render, sometimes 2):
    1. Count words; look up the (words → font-size) cache and linearly
       interpolate a predicted font that should fit on one page.
    2. Render at ``predicted - 0.2pt`` (slightly conservative).
       - If the result spills to >1 page, fall back to the legacy binary
         search to find any fitting font.
    3. Measure how much of the first page is blank.
       - If blank ≤ 20%, accept this render. Save the sample.
       - If blank > 20%, estimate how much bigger the font should be
         (fill ratio scales ≈ font², so new = old / sqrt(1 - blank)),
         render once more.
    4. If the second (bigger) render still fits AND has lower blank than
       the first, use it. Otherwise fall back to the first render.
    5. If the predicted font overflows AND no smaller font fits either,
       drop the lowest-priority section (news.pr spec) and retry.

    Falls back to a tiny placeholder PDF if WeasyPrint's native libs aren't
    installed (typical on a Windows dev box).
    """
    try:
        _ensure_dll_path()
        from weasyprint import CSS, HTML
    except (OSError, ImportError) as e:
        log.warning("WeasyPrint native libs unavailable, using placeholder PDF: %s", e)
        return _PLACEHOLDER_PDF

    from weasyprint import default_url_fetcher

    ua = (
        "Linh-News/1.0 (https://github.com/vtlinh/linh-news; "
        "vtlinh87+linhnews@gmail.com)"
    )

    def _url_fetcher(url, *args, **kwargs):
        # Log every external resource WeasyPrint asks for — the lead-image
        # failures we've been seeing usually look like a silent fetch error
        # here, so we want a trace for every attempt and outcome.
        is_data = url.startswith("data:")
        if not is_data:
            log.info("WeasyPrint fetch: %s", url[:200])
        try:
            try:
                result = default_url_fetcher(
                    url, *args, headers={"User-Agent": ua}, **kwargs
                )
            except TypeError:
                import urllib.request
                req = urllib.request.Request(url, headers={"User-Agent": ua})
                with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310
                    result = {
                        "string": r.read(),
                        "mime_type": r.headers.get_content_type(),
                        "redirected_url": r.url,
                    }
            if not is_data:
                size = len(result.get("string", b"")) if "string" in result else "stream"
                log.info("WeasyPrint fetch ok: %s [%s, %s bytes]",
                         result.get("redirected_url", url)[:200],
                         result.get("mime_type"), size)
            return result
        except Exception as e:  # noqa: BLE001
            if not is_data:
                log.warning("WeasyPrint fetch FAILED: %s — %s", url[:200], e)
            raise

    def _make_css(base_pt: float) -> CSS:
        # Cap masthead independently so it stays readable even at large body sizes.
        h1_pt = min(base_pt * 3.75, 36.0)
        return CSS(string=f"""
        @page {{ size: 12in 22in; margin: 0.4in; }}
        html, body {{ font-family: "Times New Roman", Georgia, serif; }}
        body {{ font-size: {base_pt:.2f}pt !important; line-height: 1.15 !important; }}
        h1 {{ font-size: {h1_pt:.2f}pt !important; margin: 0 0 2pt !important;
              text-align: center; font-weight: bold;
              font-family: "Times New Roman", Georgia, serif; }}
        h2 {{ font-size: {base_pt * 1.375:.2f}pt !important; margin: 4pt 0 2pt !important;
              font-weight: bold; }}
        h3, h4, h5, h6 {{ font-size: {base_pt * 1.125:.2f}pt !important;
              margin: 3pt 0 1pt !important; font-weight: bold; }}
        p, li {{ margin: 0 0 3pt !important; font-size: {base_pt:.2f}pt !important;
                 line-height: 1.15 !important; }}
        small {{ font-size: {base_pt * 0.875:.2f}pt !important; }}
        hr {{ display: none !important; }}
        br + br {{ display: none !important; }}
        img {{ max-width: 100% !important; }}
        section, article, header, footer, div {{ margin: 0 0 3pt !important; }}
        """)

    # ── render-and-measure helpers ──────────────────────────────────────
    def _render_and_measure(
        content: str, font_pt: float
    ) -> tuple[bytes, int, float]:
        """Render at ``font_pt`` and return (pdf_bytes, page_count, fill_ratio).

        ``fill_ratio`` is the fraction of the first page covered by content
        (0..1). 1.0 means full page; 0.5 means half empty. For >1 page outputs
        the ratio is reported as 1.0 (overflow == "full and then some").
        """
        css = _make_css(font_pt)
        doc = HTML(string=content, url_fetcher=_url_fetcher).render(stylesheets=[css])
        pdf_bytes = doc.write_pdf()
        n_pages = len(doc.pages)
        if n_pages == 0:
            return pdf_bytes, 0, 0.0
        if n_pages > 1:
            return pdf_bytes, n_pages, 1.0
        page = doc.pages[0]
        page_h = float(getattr(page, "height", 0) or 0)
        if page_h <= 0:
            return pdf_bytes, n_pages, 0.0
        # WeasyPrint's `_page_box` is the root box of the page; walking its
        # children gives every laid-out box's position+height. The deepest
        # bottom edge is our content extent.
        root = getattr(page, "_page_box", None)
        if root is None:
            return pdf_bytes, n_pages, 0.0

        deepest = 0.0

        def _walk(box: object) -> None:
            nonlocal deepest
            try:
                y = float(getattr(box, "position_y", 0) or 0)
                h = float(getattr(box, "height", 0) or 0)
                bottom = y + h
                if bottom > deepest:
                    deepest = bottom
            except (TypeError, ValueError):
                pass
            for child in getattr(box, "children", ()) or ():
                _walk(child)

        _walk(root)
        return pdf_bytes, n_pages, max(0.0, min(1.0, deepest / page_h))

    def _bump_for_blank(current_pt: float, blank: float) -> float:
        """How much bigger the font should be to consume the blank space.

        Text area scales ≈ font², so to fill the page we want
        new/old = sqrt(1/(1-blank)). Clamp so we don't make wild jumps.
        """
        if blank <= 0:
            return 0.0
        target = current_pt / max(1e-3, (1.0 - blank)) ** 0.5
        return max(0.5, min(2.5, target - current_pt))

    def _legacy_search(content: str) -> tuple[bytes, float] | None:
        """Used as fallback only — binary-search [5, 24]pt in 0.5pt steps."""
        sizes = [round(_FONT_MIN + i * _FONT_STEP, 1)
                 for i in range(int((_FONT_MAX - _FONT_MIN) / _FONT_STEP) + 1)]
        best_bytes: bytes | None = None
        best_pt: float = sizes[0]
        lo, hi = 0, len(sizes) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            pt = sizes[mid]
            try:
                pdf_bytes, n_pages, _ = _render_and_measure(content, pt)
            except Exception as e:  # noqa: BLE001
                log.exception("Fallback render failed at %.1fpt: %s", pt, e)
                return None
            if n_pages <= 1:
                best_bytes, best_pt = pdf_bytes, pt
                lo = mid + 1
            else:
                hi = mid - 1
        return (best_bytes, best_pt) if best_bytes is not None else None

    # ── main flow: cache → predict → render → maybe bump ────────────────
    word_count = _count_words(html)
    samples = _load_font_samples()
    predicted = _predict_font(word_count, samples)
    first_pt = _floor_half(max(_FONT_MIN, min(_FONT_MAX, predicted - 0.2)))

    log.info(
        "PDF font: %d words, %d cached samples → predicted %.2fpt → trying %.1fpt",
        word_count, len(samples), predicted, first_pt,
    )

    try:
        first_bytes, first_pages, first_fill = _render_and_measure(html, first_pt)
    except Exception:
        log.exception("First PDF render failed; falling back to binary search")
        first_bytes, first_pages, first_fill = b"", 99, 0.0

    if first_pages == 1:
        first_blank = 1.0 - first_fill
        log.info(
            "First render: 1 page at %.1fpt, fill=%.1f%% (blank=%.1f%%)",
            first_pt, first_fill * 100, first_blank * 100,
        )

        if first_blank <= _BLANK_TARGET:
            log.info("Within %.0f%% blank target — using first render at %.1fpt",
                     _BLANK_TARGET * 100, first_pt)
            _save_font_sample(word_count, first_pt)
            return first_bytes

        # Try a bigger font to fill the blank space.
        bump = _bump_for_blank(first_pt, first_blank)
        second_pt = _round_half(min(_FONT_MAX, first_pt + bump))
        if second_pt <= first_pt:
            _save_font_sample(word_count, first_pt)
            return first_bytes

        log.info("Blank %.1f%% > %.0f%% — bumping %.1f → %.1fpt to fill",
                 first_blank * 100, _BLANK_TARGET * 100, first_pt, second_pt)
        try:
            second_bytes, second_pages, second_fill = _render_and_measure(html, second_pt)
        except Exception as e:  # noqa: BLE001
            log.warning("Second render failed (%s); using first.", e)
            _save_font_sample(word_count, first_pt)
            return first_bytes

        if second_pages == 1 and second_fill >= first_fill:
            log.info("Second render at %.1fpt fits (fill=%.1f%%) — using it.",
                     second_pt, second_fill * 100)
            _save_font_sample(word_count, second_pt)
            return second_bytes

        log.info(
            "Second render at %.1fpt did not improve (pages=%d, fill=%.1f%%) — "
            "using first render at %.1fpt.",
            second_pt, second_pages, second_fill * 100, first_pt,
        )
        _save_font_sample(word_count, first_pt)
        return first_bytes

    # First render overflowed → cache lied (or this is a first run with empty
    # cache and the default guess is too big). Fall back to binary search.
    log.warning("First render overflowed (%d pages at %.1fpt) — running binary search",
                first_pages, first_pt)
    result = _legacy_search(html)
    if result:
        best_bytes, best_pt = result
        log.info("Fallback search found %.1fpt", best_pt)
        _save_font_sample(word_count, best_pt)
        return best_bytes

    # Still doesn't fit at minimum font — drop sections.
    current_html = html
    for attempt in range(10):
        trimmed = _drop_one_section(current_html)
        if trimmed is None:
            log.warning("PDF: no more sections to drop — giving up")
            break
        current_html = trimmed
        result = _legacy_search(current_html)
        if result:
            best_bytes, best_pt = result
            log.info(
                "PDF fitted to 1 page at %.1fpt after %d section drop(s)",
                best_pt, attempt + 1,
            )
            return best_bytes

    log.error("PDF: could not fit content into 1 page — returning placeholder")
    return _PLACEHOLDER_PDF


def page_count(pdf_bytes: bytes) -> int:
    """Cheap page-count from the raw PDF bytes (used by tests)."""
    return pdf_bytes.count(b"/Type /Page") + pdf_bytes.count(b"/Type/Page")
