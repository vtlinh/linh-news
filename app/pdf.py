from __future__ import annotations


def html_to_pdf(html: str) -> bytes:
    """Render print-styled HTML to a single-page US Letter PDF using WeasyPrint.

    Imported lazily because WeasyPrint pulls in native libs that may not be
    installed in every dev environment (it is required for production runs)."""
    from weasyprint import CSS, HTML

    page_css = CSS(
        string="""
        @page { size: Letter; margin: 0.5in; }
        body { font-family: "Times New Roman", Georgia, serif; }
        """
    )
    return HTML(string=html).write_pdf(stylesheets=[page_css])


def page_count(pdf_bytes: bytes) -> int:
    """Cheap page-count from the raw PDF bytes (used by tests)."""
    return pdf_bytes.count(b"/Type /Page") + pdf_bytes.count(b"/Type/Page")
