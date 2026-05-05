"""Backfill ``editions.png`` for rows where it is NULL by re-rasterizing
the existing PDF. One-shot script — safe to re-run; only NULL rows are
touched. Each PNG is committed individually so a mid-run failure leaves
the rows already done in place.

Usage::

    uv run python -m scripts.backfill_edition_png
"""

from __future__ import annotations

import logging
import sys

from sqlalchemy import select

from app.db import Edition, session_factory
from app.pdf import pdf_to_png

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_png")


def main() -> int:
    Maker = session_factory()
    with Maker() as s:
        keys = s.execute(
            select(Edition.date, Edition.email).where(Edition.png.is_(None))
        ).all()
    log.info("Backfill candidates: %d edition(s) without PNG", len(keys))
    if not keys:
        return 0

    ok = 0
    skipped = 0
    failed = 0
    for i, (day, email) in enumerate(keys, 1):
        with Maker() as s:
            row = s.get(Edition, (day, email))
            if row is None or row.png is not None:
                # Re-checked under fresh session in case a parallel run beat us.
                skipped += 1
                continue
            pdf_bytes = row.pdf
            if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
                log.warning("[%d/%d] %s %s: no valid PDF — skipping",
                            i, len(keys), day, email)
                skipped += 1
                continue
            png_bytes = pdf_to_png(pdf_bytes)
            if not png_bytes:
                log.error("[%d/%d] %s %s: PNG render returned None",
                          i, len(keys), day, email)
                failed += 1
                continue
            row.png = png_bytes
            s.commit()
            ok += 1
            log.info("[%d/%d] %s %s: PNG=%d bytes ✓",
                     i, len(keys), day, email, len(png_bytes))

    log.info("Backfill done — ok=%d skipped=%d failed=%d", ok, skipped, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
