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
# Body font window: 10pt floor up to 20pt ceiling. The fit loop
# binary-searches inside this window and tries to grow the body font as
# much as 1-page layout allows. _BLANK_TARGET=0 makes the grow-to-fill
# step always run when the page isn't completely full, so we use the
# available 20pt ceiling whenever content permits.
_FONT_MIN = 10.0
_FONT_MAX = 20.0
_FONT_STEP = 0.1
_DEFAULT_FONT_GUESS = 14.0
_BLANK_TARGET = 0.01  # grow-to-fill until fill_ratio >= 0.99


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
                # Skip stale samples below the current floor — they would
                # otherwise pin the predicted seed to a too-small font.
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
    re.compile(r"movie|film", re.IGNORECASE),
    re.compile(r"dorchester|school|elementary", re.IGNORECASE),
    re.compile(r"financial|finance", re.IGNORECASE),
    re.compile(r"\bai\b|artificial intelligence|coding ai", re.IGNORECASE),
    re.compile(r"nj|new jersey|new york", re.IGNORECASE),
    re.compile(r"us\s+political|united states", re.IGNORECASE),
    re.compile(r"global|world|international", re.IGNORECASE),
]


def _parse(html: str):
    """Parse with BeautifulSoup using the stdlib parser (no extra deps)."""
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser")


def _drop_rank(title: str) -> int:
    """Lower rank = drop first. Sections not in _DROP_PRIORITY get the
    highest rank (drop last)."""
    title_l = title.lower()
    for i, p in enumerate(_DROP_PRIORITY):
        if p.search(title_l):
            return i
        if p.search(title):
            return i
    return len(_DROP_PRIORITY)


def _strip_news_flow(html: str) -> str:
    """Empty the contents of ``<div class="flow">…</div>``. Phase 1 fits
    rail + chrome alone; the flow is restored before Phase 2."""
    soup = _parse(html)
    flow = soup.find("div", class_="flow")
    if flow is None:
        return html
    flow.clear()
    return str(soup)


def _drop_one_movie(html: str) -> str | None:
    """Strip the last movie card from the rail. Returns None if there are
    no movie cards left. Each card has ``class="movie-card"``; its title
    sits in a child with ``class="movie-title"``."""
    soup = _parse(html)
    cards = soup.find_all("div", class_="movie-card")
    if not cards:
        return None
    last = cards[-1]
    title_el = last.find(class_="movie-title")
    title = (title_el.get_text(strip=True) if title_el else "?")[:60]
    log.info("PDF: dropping movie %r (%d → %d cards)", title, len(cards), len(cards) - 1)
    last.decompose()
    return str(soup)


def _drop_one_calendar_event(html: str) -> str | None:
    """Strip the last calendar event row (``class="cal-event"``) from the
    rail. Returns None if there are none left."""
    soup = _parse(html)
    events = soup.find_all("div", class_="cal-event")
    if not events:
        return None
    last = events[-1]
    text = last.get_text(strip=True)[:60]
    log.info("PDF: dropping calendar event %r (%d → %d events)",
             text, len(events), len(events) - 1)
    last.decompose()
    return str(soup)


def _section_groups(soup) -> list[tuple[object, list[object], str]]:
    """Bucket the news flow's flat ``<section>`` siblings into groups —
    each ``news-header`` collects the following ``news-story`` siblings.

    Returns ``[(header_section, [story_sections], title), …]``.
    """
    flow = soup.find("div", class_="flow")
    if flow is None:
        return []
    groups: list[tuple[object, list[object], str]] = []
    current_header = None
    current_stories: list[object] = []
    current_title = ""
    for sec in flow.find_all("section", recursive=False):
        classes = sec.get("class") or []
        if "news-header" in classes:
            if current_header is not None:
                groups.append((current_header, current_stories, current_title))
            current_header = sec
            current_stories = []
            h2 = sec.find("h2")
            current_title = h2.get_text(strip=True) if h2 else ""
        elif "news-story" in classes:
            current_stories.append(sec)
    if current_header is not None:
        groups.append((current_header, current_stories, current_title))
    return groups


def _drop_one_article(html: str) -> str | None:
    """Drop one news story from the section with the most stories
    (tiebreak by ``_DROP_PRIORITY``, lowest priority first). The chosen
    section's *last* story is the one removed. Always keeps at least 1
    story per section so headers don't end up orphaned."""
    soup = _parse(html)
    groups = _section_groups(soup)
    candidates = [g for g in groups if len(g[1]) >= 2]
    if not candidates:
        return None
    candidates.sort(key=lambda g: (-len(g[1]), _drop_rank(g[2])))
    _header, stories, title = candidates[0]
    last = stories[-1]
    log.info("PDF: dropping last story of %r (%d → %d stories)",
             title[:60], len(stories), len(stories) - 1)
    last.decompose()
    return str(soup)


def _drop_one_section(html: str) -> str | None:
    """Last-resort: drop an entire news category (header + all stories)
    by ``_DROP_PRIORITY``. Also removes the preceding ``sep-group`` rule
    so the flow doesn't end with a doubled separator."""
    soup = _parse(html)
    groups = _section_groups(soup)
    if not groups:
        return None

    def _kill(group_idx: int, reason: str) -> str:
        header, stories, title = groups[group_idx]
        # Drop the preceding sep-group div so we don't end up with two
        # separators in a row.
        prev = header.find_previous_sibling()
        if prev is not None and "sep-group" in (prev.get("class") or []):
            prev.decompose()
        log.info("PDF: dropping category %r (header + %d stories) — %s",
                 title[:60], len(stories), reason)
        for s in stories:
            s.decompose()
        header.decompose()
        return str(soup)

    for pattern in _DROP_PRIORITY:
        for i in range(len(groups) - 1, -1, -1):
            if pattern.search(groups[i][2].lower()) or pattern.search(groups[i][2]):
                return _kill(i, "matched drop-priority pattern")
    return _kill(len(groups) - 1, "last-resort: no priority match")


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

    ua = "Linh-News/1.0 (https://github.com/vtlinh/linh-news; vtlinh87+linhnews@gmail.com)"

    # Per-call URL cache. The fit loop renders the same document many times
    # at different font sizes, and re-runs after every drop. Without this
    # cache, each render re-downloads the masthead font + every movie
    # backdrop, adding ~9 HTTPS round-trips per render — minutes of
    # latency over a typical 20-render fit. Cache only successful fetches
    # so transient failures still get retried.
    fetch_cache: dict[str, dict] = {}

    def _url_fetcher(url, *args, **kwargs):
        is_data = url.startswith("data:")
        if not is_data and url in fetch_cache:
            return fetch_cache[url]
        if not is_data:
            log.info("WeasyPrint fetch: %s", url[:200])
        try:
            try:
                result = default_url_fetcher(url, *args, headers={"User-Agent": ua}, **kwargs)
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
                log.info(
                    "WeasyPrint fetch ok: %s [%s, %s bytes]",
                    result.get("redirected_url", url)[:200],
                    result.get("mime_type"),
                    size,
                )
                fetch_cache[url] = result
            return result
        except Exception as e:  # noqa: BLE001
            if not is_data:
                log.warning("WeasyPrint fetch FAILED: %s — %s", url[:200], e)
            raise

    def _make_css(base_pt: float) -> CSS:
        # Masthead size: 12x base, cap 115.2pt (-20% from the previous 15x/144pt).
        h1_pt = min(base_pt * 12.0, 115.2)
        # 2560 × 1440 px portrait at 94.14 PPI ⇒ 15.296in × 27.193in.
        # Stays in portrait orientation (taller than wide).
        # Append "Noto Color Emoji" (COLRv1) as the last fallback on every
        # body/heading rule so emoji codepoints render in color via the
        # Debian fonts-noto-color-emoji package installed in the image,
        # rather than falling back to a monochrome glyph from DejaVu.
        return CSS(
            string=f"""
        @page {{ size: 15.296in 27.193in; margin: 0.4in; }}
        /* The pdf_renderer's embedded style block declares a "LinhEmoji"
           @font-face with unicode-range covering emoji blocks — that's
           what handles colour glyphs. Putting "Noto Color Emoji" or any
           emoji family directly in this body stack causes WeasyPrint to
           use the emoji font for ALL text (Times New Roman never gets
           applied, bold collapses), so we keep it serif-only here. */
        html, body {{ font-family: "Times New Roman", Georgia, serif; }}
        body {{ font-size: {base_pt:.2f}pt !important; line-height: 1.15 !important; }}
        /* No emoji families here — WeasyPrint drops @font-face Chomsky
           when emoji families share the stack. Masthead text has no
           emoji glyphs. */
        h1 {{ font-size: {h1_pt:.2f}pt !important; margin: 0 0 2pt !important;
              text-align: center; font-weight: normal;
              font-family: "Linh Times Masthead", "Times New Roman",
                           Georgia, serif; }}
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
        /* Crush spacing in the news flow only — applying this globally
           also slammed every div in the rail (every calendar row, every
           movie card sub-div) and bloated the rail by 1-2in of phantom
           margin, which made Phase 1 over-trim. */
        .flow section, .flow article, .flow header, .flow footer, .flow div {{
            margin: 0 0 3pt !important;
        }}
        """
        )

    # ── render-and-measure helpers ──────────────────────────────────────
    def _render_and_measure(content: str, font_pt: float) -> tuple[bytes, int, float]:
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

    def _fit_at_min_then_grow(content: str) -> tuple[bytes, float, float] | None:
        """Per the user's spec:

        1. Render at the MIN font. If it still overflows → return ``None``
           so the caller can trim content and retry.
        2. If MIN fits → binary-search ``[MIN, MAX]`` for the LARGEST font
           that still fits one page (grow-to-fill).

        Returns ``(pdf_bytes, font_pt, blank_ratio)`` of the chosen render.
        Logs every font size tried and whether it fit.
        """
        tried: list[str] = []
        try:
            min_bytes, min_pages, min_fill = _render_and_measure(content, _FONT_MIN)
        except Exception:
            log.exception("MIN-font render failed")
            return None
        tried.append(f"{_FONT_MIN:.1f}{'✓' if min_pages <= 1 else '✗'}")
        if min_pages > 1:
            log.info("Font search: %s — MIN doesn't fit, trim needed", " ".join(tried))
            return None

        # MIN font fits. Binary-search upward for the biggest font that
        # still fits — gives us the densest one-page layout.
        best_bytes, best_pt, best_fill = min_bytes, _FONT_MIN, min_fill
        lo, hi = _FONT_MIN, _FONT_MAX
        while hi - lo > _FONT_STEP:
            mid = _round_half((lo + hi) / 2)
            if mid <= lo or mid >= hi:
                break
            try:
                b, p, f = _render_and_measure(content, mid)
            except Exception:
                log.exception("Grow render failed at %.1fpt", mid)
                break
            tried.append(f"{mid:.1f}{'✓' if p <= 1 else '✗'}")
            if p <= 1:
                best_bytes, best_pt, best_fill = b, mid, f
                lo = mid
            else:
                hi = mid

        _save_font_sample(_count_words(content), best_pt)
        log.info(
            "Font search: %s → chose %.1fpt (blank=%.1f%%)",
            " ".join(tried), best_pt, (1.0 - best_fill) * 100,
        )
        return best_bytes, best_pt, 1.0 - best_fill

    # ── Phase 1: rail-only fit (no news flow yet) ──
    # Render the document with the news flow content stripped — only the
    # masthead/dateline/rail/stocks-footer compete for vertical space. If
    # this rail+chrome layout doesn't fit at MIN font, drop a movie (last
    # first); if no movies left, drop a calendar event. Repeat until MIN
    # fits OR the rail is empty. Phase 1 never returns the rail-only PDF
    # — the trimmed `current` (which still has news) is what feeds Phase
    # 2.
    import time as _time
    phase1_t0 = _time.monotonic()
    log.info("PDF: ── phase 1 begin (rail-only fit; news flow stripped) ──")
    current = html
    for _ in range(40):
        rail_only = _strip_news_flow(current)
        result = _fit_at_min_then_grow(rail_only)
        if result is not None:
            log.info(
                "PDF: ── phase 1 end in %.2fs (rail+chrome fits at MIN) ──",
                _time.monotonic() - phase1_t0,
            )
            break
        trimmed = _drop_one_movie(current) or _drop_one_calendar_event(current)
        if trimmed is None:
            log.info(
                "PDF: ── phase 1 end in %.2fs (rail emptied; rail+chrome "
                "still overflows — Phase 2 will drop news on the full doc) ──",
                _time.monotonic() - phase1_t0,
            )
            break
        current = trimmed

    # ── Phase 2: full content (rail-locked from Phase 1 + news flow) ──
    # Same MIN-then-grow strategy, but on overflow we drop news content:
    # balance subsection counts first, then last-resort whole-section drop.
    phase2_t0 = _time.monotonic()
    log.info("PDF: ── phase 2 begin (news trim + font fit on full doc) ──")
    for attempt in range(20):
        result = _fit_at_min_then_grow(current)
        if result is not None:
            pdf_bytes, font_pt, blank = result
            log.info(
                "PDF fit (phase 2): %.1fpt, blank=%.1f%% (after %d news trim(s)) in %.2fs",
                font_pt, blank * 100, attempt, _time.monotonic() - phase2_t0,
            )
            return pdf_bytes
        trimmed = _drop_one_article(current) or _drop_one_section(current)
        if trimmed is None:
            log.warning("PDF: nothing left to drop — shipping placeholder")
            return _PLACEHOLDER_PDF
        current = trimmed

    log.error("PDF: still overflowing after 20 news-trim attempts — placeholder")
    return _PLACEHOLDER_PDF


def page_count(pdf_bytes: bytes) -> int:
    """Cheap page-count from the raw PDF bytes (used by tests)."""
    return pdf_bytes.count(b"/Type /Page") + pdf_bytes.count(b"/Type/Page")
