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
_FONT_MIN = 7.0   # readable on a 31" 4K monitor
_FONT_MAX = 14.0  # newspaper sanity ceiling
_FONT_STEP = 0.5  # render fonts at 0.5pt resolution
_DEFAULT_FONT_GUESS = 10.0
_BLANK_TARGET = 0.10  # tolerate up to 10% blank space


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


# Children inside a <section> we consider "articles" — droppable items.
_ARTICLE_CHILD_RE = re.compile(
    r"<(article|li)(?:\s[^>]*)?>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)


def _drop_one_article(html: str) -> str | None:
    """Drop the LAST <article>/<li> from the lowest-priority section that
    still has more than one such child. Returns trimmed HTML, or None.

    Walks ``_DROP_PRIORITY`` (lowest priority first). The first matching
    section with ≥2 articles loses its last one.
    """
    sections = list(_SECTION_RE.finditer(html))
    if not sections:
        return None

    def _h2_text(sec_html: str) -> str:
        m = _H2_TEXT_RE.search(sec_html)
        return _TAG_STRIP_RE.sub("", m.group(1)).strip().lower() if m else ""

    for pattern in _DROP_PRIORITY:
        for sec in reversed(sections):
            if not pattern.search(_h2_text(sec.group())):
                continue
            sec_html = sec.group()
            articles = list(_ARTICLE_CHILD_RE.finditer(sec_html))
            if len(articles) < 2:
                continue
            last = articles[-1]
            new_sec = sec_html[: last.start()] + sec_html[last.end():]
            log.info(
                "PDF: dropping one article from section %r (%d → %d items)",
                _h2_text(sec_html)[:60], len(articles), len(articles) - 1,
            )
            return html[: sec.start()] + new_sec + html[sec.end():]
    return None


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

    def _fit(content: str) -> tuple[bytes, float, float] | None:
        """Two-sided binary search over [_FONT_MIN, _FONT_MAX] in 0.5pt steps.

        Returns (pdf_bytes, font_pt, blank_ratio) for the chosen render, or
        ``None`` if even ``_FONT_MIN`` overflows (caller should drop content).

        Step 1: shrink to fit (if seeded font overflows).
        Step 2: grow to fill (if blank > _BLANK_TARGET), keeping it on 1 page.
        """
        # Seed from the (words → font) cache.
        words = _count_words(content)
        samples = _load_font_samples()
        seed = max(_FONT_MIN, min(_FONT_MAX, _predict_font(words, samples)))
        seed = _round_half(seed)

        try:
            seed_bytes, seed_pages, seed_fill = _render_and_measure(content, seed)
        except Exception:
            log.exception("Seed render failed at %.1fpt", seed)
            seed_bytes, seed_pages, seed_fill = b"", 99, 0.0

        # ── Step 1: shrink-to-fit if the seed overflowed ───────────────
        if seed_pages > 1:
            try:
                lo_bytes, lo_pages, lo_fill = _render_and_measure(content, _FONT_MIN)
            except Exception:
                log.exception("Floor render failed at %.1fpt", _FONT_MIN)
                return None
            if lo_pages > 1:
                # Even at the readable floor it overflows — caller must drop.
                return None
            best_bytes, best_pt, best_fill = lo_bytes, _FONT_MIN, lo_fill
            lo, hi = _FONT_MIN, seed
            while hi - lo > _FONT_STEP:
                mid = _round_half((lo + hi) / 2)
                if mid <= lo or mid >= hi:
                    break
                try:
                    b, p, f = _render_and_measure(content, mid)
                except Exception:
                    log.exception("Shrink render failed at %.1fpt", mid)
                    break
                if p <= 1:
                    best_bytes, best_pt, best_fill = b, mid, f
                    lo = mid
                else:
                    hi = mid
            seed_bytes, seed, seed_fill, seed_pages = best_bytes, best_pt, best_fill, 1
            log.info("Shrink-to-fit: chose %.1fpt (fill=%.1f%%)",
                     seed, seed_fill * 100)

        # ── Step 2: grow-to-fill if too much blank ─────────────────────
        blank = 1.0 - seed_fill
        if seed_pages == 1 and blank > _BLANK_TARGET and seed < _FONT_MAX:
            best_bytes, best_pt, best_fill = seed_bytes, seed, seed_fill
            lo, hi = seed, _FONT_MAX
            while hi - lo > _FONT_STEP:
                mid = _round_half((lo + hi) / 2)
                if mid <= lo or mid >= hi:
                    break
                try:
                    b, p, f = _render_and_measure(content, mid)
                except Exception:
                    log.exception("Grow render failed at %.1fpt", mid)
                    break
                if p <= 1:
                    best_bytes, best_pt, best_fill = b, mid, f
                    lo = mid
                else:
                    hi = mid
            log.info("Grow-to-fill: chose %.1fpt (fill=%.1f%%, blank=%.1f%%)",
                     best_pt, best_fill * 100, (1.0 - best_fill) * 100)
            seed_bytes, seed, seed_fill = best_bytes, best_pt, best_fill

        _save_font_sample(_count_words(content), seed)
        return seed_bytes, seed, 1.0 - seed_fill

    # ── Driver: fit, then drop articles/sections only if floor overflows ──
    current = html
    for attempt in range(20):
        result = _fit(current)
        if result is not None:
            pdf_bytes, font_pt, blank = result
            log.info(
                "PDF fit: %.1fpt, blank=%.1f%% (after %d trim(s))",
                font_pt, blank * 100, attempt,
            )
            return pdf_bytes
        # Floor still overflowed → trim. Article-level first, section-level last.
        trimmed = _drop_one_article(current) or _drop_one_section(current)
        if trimmed is None:
            log.warning("PDF: nothing left to drop — shipping placeholder")
            return _PLACEHOLDER_PDF
        current = trimmed

    log.error("PDF: still overflowing after 20 trim attempts — placeholder")
    return _PLACEHOLDER_PDF


def page_count(pdf_bytes: bytes) -> int:
    """Cheap page-count from the raw PDF bytes (used by tests)."""
    return pdf_bytes.count(b"/Type /Page") + pdf_bytes.count(b"/Type/Page")
