from __future__ import annotations

import contextlib
import logging
import os
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


def html_to_pdf(html: str) -> bytes:
    """Render print-styled HTML to a single-page US Letter PDF using WeasyPrint.

    Falls back to a tiny placeholder PDF if WeasyPrint's native libs aren't
    installed (typical on a Windows dev box)."""
    try:
        _ensure_dll_path()
        from weasyprint import CSS, HTML
    except (OSError, ImportError) as e:
        log.warning("WeasyPrint native libs unavailable, using placeholder PDF: %s", e)
        return _PLACEHOLDER_PDF

    # Wikimedia / Wikipedia reject default Python User-Agent strings with a
    # 400, so the lead-story image won't load. Plumb through a real UA via
    # WeasyPrint's url_fetcher hook.
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
            # Older WeasyPrint signature without `headers` kwarg — patch via
            # urllib.
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310
                return {
                    "string": r.read(),
                    "mime_type": r.headers.get_content_type(),
                    "redirected_url": r.url,
                }

    def _make_css(base_pt: float) -> CSS:
        return CSS(string=f"""
        @page {{ size: Letter; margin: 0.4in; }}
        html, body {{ font-family: "Times New Roman", Georgia, serif; }}
        body {{ font-size: {base_pt:.2f}pt !important; line-height: 1.15 !important; }}
        h1 {{ font-size: {base_pt * 3.75:.2f}pt !important; margin: 0 0 2pt !important;
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

    # Render at progressively smaller font sizes until the PDF fits one page.
    _FONT_STEPS = [8.0, 7.5, 7.0, 6.5, 6.0, 5.5, 5.0]
    pdf_bytes = _PLACEHOLDER_PDF
    for font_pt in _FONT_STEPS:
        try:
            pdf_bytes = HTML(string=html, url_fetcher=_url_fetcher).write_pdf(
                stylesheets=[_make_css(font_pt)]
            )
        except Exception as e:  # noqa: BLE001
            log.exception("WeasyPrint render failed at %.1fpt: %s", font_pt, e)
            return _PLACEHOLDER_PDF
        pages = page_count(pdf_bytes)
        if pages <= 1:
            if font_pt < _FONT_STEPS[0]:
                log.info("PDF fitted to 1 page at %.1fpt (was overflowing at 8pt)", font_pt)
            return pdf_bytes
        log.info("PDF overflowed (%d pages) at %.1fpt — retrying smaller", pages, font_pt)

    log.warning("Could not fit PDF to 1 page even at %.1fpt — returning %d-page PDF",
                _FONT_STEPS[-1], page_count(pdf_bytes))
    return pdf_bytes


def page_count(pdf_bytes: bytes) -> int:
    """Cheap page-count from the raw PDF bytes (used by tests)."""
    return pdf_bytes.count(b"/Type /Page") + pdf_bytes.count(b"/Type/Page")
