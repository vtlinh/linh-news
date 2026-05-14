"""Dev helper: re-render today's edition from the cached LLM response
without calling the LLM.

Reads ``editions.content_json`` for the given date+email and pipes it
back through ``generate.run(linhnews_override=...)``. Handy for iterating
on PDF/HTML layout work — image-fetch, calendar, weather, and
WeasyPrint render all execute, but no Anthropic call.

Usage:
    uv run python -m scripts.rerender_today \\
        --email vtlinh87@gmail.com
    uv run python -m scripts.rerender_today \\
        --email vtlinh87@gmail.com --date 2026-05-13
"""

from __future__ import annotations

import argparse
import copy
import logging
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from app import generate
from app.db import Edition, session_factory
from app.settings import local_today

log = logging.getLogger(__name__)


def _load_content_json(day: date, email: str) -> dict:
    Maker = session_factory()
    with Maker() as s:
        row = s.get(Edition, (day, email))
        if row is None:
            raise SystemExit(
                f"No editions row for ({day}, {email}). Run a normal "
                f"generation first so we have a cached content_json to reuse."
            )
        if not row.content_json:
            raise SystemExit(
                f"editions row for ({day}, {email}) has empty content_json. "
                f"Was this row created before the structured-response pipeline?"
            )
        return copy.deepcopy(dict(row.content_json))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--email", required=True, help="User email whose edition to re-render.")
    p.add_argument(
        "--date",
        default=None,
        help="ISO date (YYYY-MM-DD). Defaults to today.",
    )
    args = p.parse_args(argv)

    day = date.fromisoformat(args.date) if args.date else local_today()
    email = args.email.lower()

    log.info("Loading cached content_json for (%s, %s)", day, email)
    linhnews = _load_content_json(day, email)
    if linhnews.get("headline"):
        log.info("content_json has a headline (%d words body)",
                 len((linhnews["headline"].get("text") or "").split()))
    else:
        log.info("content_json has NO headline; pipeline will render without one")

    log.info("Re-running post-LLM pipeline (no LLM call)…")
    started = datetime.now(UTC).timestamp()
    generate.run(
        "refresh",
        today=day,
        email=email,
        linhnews_override=linhnews,
        debug_generated_at_override=datetime.now(UTC),
    )

    snap_dir = Path(__file__).resolve().parent.parent / "logs"
    candidates = [
        p for p in snap_dir.glob("pdf-refresh-*.pdf")
        if p.stat().st_mtime >= started - 1
    ]
    if candidates:
        latest = max(candidates, key=lambda p: p.stat().st_mtime)
        log.info("PDF written: %s", latest)
        print(f"\nPDF:  {latest}")
        assembled = snap_dir / "pdf-assembled-latest.html"
        if assembled.exists():
            print(f"HTML: {assembled}")
    else:
        log.warning(
            "No new PDF snapshot found in %s — generation may have used the "
            "placeholder PDF (WeasyPrint libs missing?).",
            snap_dir,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
