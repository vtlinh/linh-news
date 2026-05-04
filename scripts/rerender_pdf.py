"""Re-render today's PDF from stored pdf_html (or a snapshot file).

Use this to debug missing images / overflow without re-running the slow
Claude pipeline. The verbose URL fetcher in app/pdf.py logs every image
fetch attempt so you can see exactly which one(s) WeasyPrint can't load.

Examples:

    # Re-render today's edition's pdf_html (Edition.pdf_html column)
    uv run python scripts/rerender_pdf.py

    # Re-render a specific date
    uv run python scripts/rerender_pdf.py --date 2026-05-01

    # Re-render the most recent logs/pdf-html-*.html snapshot
    uv run python scripts/rerender_pdf.py --snapshot latest

    # Re-render an explicit snapshot file
    uv run python scripts/rerender_pdf.py --snapshot logs/pdf-html-refresh-1777648000.html

The output PDF is written to logs/repro-<date>-<ts>.pdf with a side-by-side
report of <img> tag count (HTML) and embedded image count (rendered PDF).
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import date as date_cls
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("rerender")


def _load_from_snapshot(arg: str) -> tuple[str, str]:
    """Return (label, pdf_html) for a snapshot file or 'latest'."""
    snap_dir = REPO_ROOT / "logs"
    if arg == "latest":
        files = sorted(
            snap_dir.glob("pdf-html-*.html"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not files:
            raise SystemExit(f"No snapshots in {snap_dir}/pdf-html-*.html")
        path = files[0]
    else:
        path = Path(arg)
        if not path.is_absolute():
            path = REPO_ROOT / path
    log.info("Loading snapshot: %s", path)
    return path.name, path.read_text(encoding="utf-8")


def _load_from_db(day: date_cls) -> tuple[str, str]:
    from app.db import Edition, session_factory

    Maker = session_factory()
    with Maker() as s:
        e = s.get(Edition, day)
        if not e:
            raise SystemExit(f"No edition row for {day}")
        if not e.pdf_html:
            raise SystemExit(
                f"Edition {day} has no pdf_html stored (column likely added "
                f"after this row was generated). Re-run a refresh first, or "
                f"use --snapshot latest if logs/pdf-html-*.html exists."
            )
        log.info(
            "Loaded edition %s (generated_at=%s, pdf_html len=%d)",
            day,
            e.generated_at,
            len(e.pdf_html),
        )
        return f"db-{day.isoformat()}", e.pdf_html


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--date", default=None, help="Edition date to load from DB (YYYY-MM-DD). Default: today."
    )
    p.add_argument(
        "--snapshot", default=None, help="Path to a logs/pdf-html-*.html snapshot, or 'latest'."
    )
    args = p.parse_args()

    if args.snapshot:
        label, pdf_html = _load_from_snapshot(args.snapshot)
    else:
        from app.settings import local_today

        target = date_cls.fromisoformat(args.date) if args.date else local_today()
        label, pdf_html = _load_from_db(target)

    img_tags = re.findall(r"<img\b[^>]*>", pdf_html, re.IGNORECASE)
    log.info("pdf_html: %d bytes, %d <img> tags", len(pdf_html), len(img_tags))
    for i, tag in enumerate(img_tags[:10], 1):
        m = re.search(r'\bsrc\s*=\s*["\']([^"\']+)', tag, re.IGNORECASE)
        log.info("  img[%d] src=%s", i, (m.group(1) if m else "(unparseable)")[:200])

    from app import pdf as pdf_mod

    log.info("Calling pdf.html_to_pdf — every WeasyPrint fetch will be logged below")
    t0 = time.monotonic()
    pdf_bytes = pdf_mod.html_to_pdf(pdf_html)
    log.info("html_to_pdf done in %.2fs (%d bytes)", time.monotonic() - t0, len(pdf_bytes))

    out = REPO_ROOT / "logs" / f"repro-{label}-{int(time.time())}.pdf"
    out.write_bytes(pdf_bytes)
    embedded = pdf_bytes.count(b"/Subtype /Image") + pdf_bytes.count(b"/Subtype/Image")
    pages = pdf_mod.page_count(pdf_bytes)
    log.info("=" * 60)
    log.info("Output: %s", out)
    log.info("HTML <img> tags : %d", len(img_tags))
    log.info(
        "PDF image XObjects: %d  (these are the images WeasyPrint actually embedded)", embedded
    )
    log.info("PDF pages       : %d", pages)
    if embedded < len(img_tags):
        log.warning(
            "MISMATCH — %d HTML images but only %d in PDF. "
            "Look for 'WeasyPrint fetch FAILED' lines above.",
            len(img_tags),
            embedded,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
