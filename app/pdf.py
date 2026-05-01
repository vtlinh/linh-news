from __future__ import annotations

import contextlib
import logging
import os
import re
import sys

log = logging.getLogger(__name__)


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

    Algorithm:
    1. Binary-search [5, 14]pt in 0.5pt steps for the LARGEST font that fits
       one page — this maximises font size, keeping empty space ≤ ~4%.
    2. If even 5pt overflows, drop the lowest-priority section (per news.pr
       spec) and retry — repeat until the content fits or nothing remains.
    3. Falls back to a tiny placeholder PDF if WeasyPrint's native libs aren't
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
        try:
            return default_url_fetcher(
                url, *args, headers={"User-Agent": ua}, **kwargs
            )
        except TypeError:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310
                return {
                    "string": r.read(),
                    "mime_type": r.headers.get_content_type(),
                    "redirected_url": r.url,
                }

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

    # Font-size candidates: 5.0 → 14.0 in 0.5pt steps (19 values, ~5 renders).
    # At the top end adjacent steps differ by ~3.6%, so the largest fitting
    # font leaves ≤ ~4% empty space — well within the 10% requirement.
    _SIZES = [round(5.0 + i * 0.5, 1) for i in range(19)]  # 5.0 … 14.0

    def _best_fit(content: str) -> tuple[bytes, float] | None:
        """Binary-search for the largest font in _SIZES that fits one page.
        Returns (pdf_bytes, font_pt) or None if even the smallest doesn't fit."""
        best_bytes: bytes | None = None
        best_pt: float = _SIZES[0]
        lo, hi = 0, len(_SIZES) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            pt = _SIZES[mid]
            try:
                pdf_bytes = HTML(string=content, url_fetcher=_url_fetcher).write_pdf(
                    stylesheets=[_make_css(pt)]
                )
            except Exception as e:  # noqa: BLE001
                log.exception("WeasyPrint render failed at %.1fpt: %s", pt, e)
                return None
            if page_count(pdf_bytes) <= 1:
                best_bytes, best_pt = pdf_bytes, pt
                lo = mid + 1
            else:
                hi = mid - 1
        return (best_bytes, best_pt) if best_bytes is not None else None

    # First attempt on full content.
    result = _best_fit(html)
    if result:
        best_bytes, best_pt = result
        if best_pt != 8.0:
            log.info("PDF auto-sized to %.1fpt", best_pt)
        return best_bytes

    # Content overflows even at 5pt — drop sections one at a time.
    current_html = html
    for attempt in range(10):
        trimmed = _drop_one_section(current_html)
        if trimmed is None:
            log.warning("PDF: no more sections to drop — giving up")
            break
        current_html = trimmed
        result = _best_fit(current_html)
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
