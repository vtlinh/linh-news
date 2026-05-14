"""Print-styled HTML → single-page broadsheet PDF.

The PDF is composed in five fixed regions (see ``app.pdf_renderer`` for the
diagram). Per-region behavior:

* **Top** (masthead + dateline) — rendered at a fixed body font (11pt).
  Height is *measured* once per refresh by laying it out in a very tall
  page and reading its natural content height.
* **Stocks footer** — same: fixed 10pt font, natural height measured once.
* **Side rail** — own font binary-searched in ``[FONT_MIN, FONT_MAX]`` at
  0.1pt; when even the floor doesn't fit, drop a movie card, then a
  calendar event, retry. If the caller passed a ``cached_rail`` (same-day
  refresh) we skip this entirely and use the cached inner HTML + font.
* **Upper news band** — 70% of the body height, 4 cols, own font binary
  search. Drops one article (then one whole section) on unfittable MIN.
* **Lower news band** — 30% of the body height, otherwise identical.

Each band/region is fitted *independently* in its own minimal WeasyPrint
render where the page size equals the region's allocated box. After all
five regions have settled, ``app.pdf_renderer.assemble_final_html`` puts
them back together with absolute heights + per-region font sizes and we
render once more to produce the actual PDF.

The 1-section case is special: the 70/30 split is meaningless with one
section, so we raise ``PdfSkipped`` rather than producing a degenerate
document. The caller stores the HTML edition but skips the PDF.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import sys
import time
from dataclasses import dataclass

from app.pdf_renderer import (
    AssemblyLayout,
    PdfParts,
    _esc,
    assemble_final_html,
    build_single_region_html,
    headline_body_region_css,
    headline_title_region_css,
    news_region_css,
    rail_region_css,
    stocks_region_css,
    top_region_css,
)

log = logging.getLogger(__name__)


# On Windows, WeasyPrint dlopens libgobject / libpango / libcairo / libharfbuzz
# at import time. Python 3.8+ uses safe DLL search, which means PATH alone
# does NOT make transitive DLL deps discoverable -- we have to register the
# directory explicitly via ``os.add_dll_directory``. The repo's local install
# uses MSYS2's UCRT64 packages; override via ``WEASYPRINT_DLL_DIR`` env var
# if the libs live elsewhere.
if sys.platform == "win32":
    _candidate = os.environ.get("WEASYPRINT_DLL_DIR") or r"C:\msys64\ucrt64\bin"
    if os.path.isdir(_candidate):
        try:
            os.add_dll_directory(_candidate)
            log.info("Registered WeasyPrint DLL directory: %s", _candidate)
        except (OSError, AttributeError) as e:
            log.warning(
                "Could not register WeasyPrint DLL directory %s: %s",
                _candidate,
                e,
            )


# ── Page geometry (broadsheet) ────────────────────────────────────────────
# 2560 × 1440 px portrait at 94.14 PPI.
_PAGE_W_IN = 15.296
_PAGE_H_IN = 27.193
_PAGE_MARGIN_IN = 0.4
_RAIL_W_IN = 2.4
_CONTENT_GAP_IN = 14 / 72  # 14pt → ≈ 0.194in
_UPPER_BAND_RATIO = 0.70
# Fixed body fonts for the masthead/dateline and stocks ribbon. These
# don't participate in the binary search — their *heights* are measured
# at these fonts so the news/rail regions know how much vertical space
# they get.
_TOP_FONT_PT = 11.0
_STOCKS_FONT_PT = 10.0
_PX_PER_IN = 96.0  # CSS pixels — used to convert WeasyPrint's box-tree
# coordinates (CSS px) back to inches.


# ── Font window ──────────────────────────────────────────────────────────
_FONT_MIN = 10.0
_FONT_MAX = 20.0
_FONT_STEP = 0.1
_DEFAULT_FONT_GUESS = 14.0


# ── Cache (namespaced per region, JSON in kv_cache) ──────────────────────
# Old single-key schema (v1) is read once, replicated into v2 under each
# region name, then deleted. Subsequent runs read v2 directly.
_FONT_CACHE_V1_KEY = "linh_news:pdf_font_cache"
_FONT_CACHE_V2_KEY = "linh_news:pdf_font_cache_v2"
_FONT_CACHE_REGIONS = ("upper-news", "lower-news", "rail")
_FONT_CACHE_MAX_SAMPLES = 30


def _round_step(x: float) -> float:
    return round(x / _FONT_STEP) * _FONT_STEP


def _floor_step(x: float) -> float:
    return math.floor(x / _FONT_STEP) * _FONT_STEP


def _count_words(html: str) -> int:
    text = re.sub(r"<[^>]+>", " ", html)
    return len(text.split())


def _migrate_cache_v1_if_needed(backend) -> None:
    """If a v1 cache exists (flat ``[[w, pt], …]`` under the old key)
    and v2 does not, seed v2 with the v1 samples for every region and
    delete v1. No-op on subsequent runs.
    """
    try:
        raw_v2 = backend.get(_FONT_CACHE_V2_KEY)
        if raw_v2:
            return
        raw_v1 = backend.get(_FONT_CACHE_V1_KEY)
        if not raw_v1:
            return
        items = json.loads(raw_v1)
        legacy: list[tuple[int, float]] = []
        for it in items:
            try:
                w, f = int(it[0]), float(it[1])
                if w > 0 and _FONT_MIN <= f <= _FONT_MAX:
                    legacy.append([w, f])  # type: ignore[arg-type]
            except (TypeError, ValueError, IndexError):
                continue
        if not legacy:
            backend.set(_FONT_CACHE_V2_KEY, json.dumps({}))
            backend.set(_FONT_CACHE_V1_KEY, "")
            return
        v2 = {region: list(legacy) for region in _FONT_CACHE_REGIONS}
        backend.set(_FONT_CACHE_V2_KEY, json.dumps(v2))
        backend.set(_FONT_CACHE_V1_KEY, "")  # tombstone
        log.info(
            "PDF font cache: migrated %d v1 samples into v2 under %d regions",
            len(legacy),
            len(_FONT_CACHE_REGIONS),
        )
    except Exception:  # noqa: BLE001
        log.exception("PDF font cache migration v1→v2 failed; starting fresh")


def _load_font_samples(region: str) -> list[tuple[int, float]]:
    try:
        from app import cache

        backend = cache._get_backend()  # noqa: SLF001
        _migrate_cache_v1_if_needed(backend)
        raw = backend.get(_FONT_CACHE_V2_KEY)
        if not raw:
            return []
        store = json.loads(raw)
        if not isinstance(store, dict):
            return []
        out: list[tuple[int, float]] = []
        for it in store.get(region, []) or []:
            try:
                w, f = int(it[0]), float(it[1])
                if w > 0 and _FONT_MIN <= f <= _FONT_MAX:
                    out.append((w, f))
            except (TypeError, ValueError, IndexError):
                continue
        return out
    except Exception:  # noqa: BLE001
        log.exception("Could not load PDF font cache region=%s", region)
        return []


def _save_font_sample(region: str, word_count: int, font_pt: float) -> None:
    try:
        from app import cache

        backend = cache._get_backend()  # noqa: SLF001
        _migrate_cache_v1_if_needed(backend)
        raw = backend.get(_FONT_CACHE_V2_KEY)
        store = json.loads(raw) if raw else {}
        if not isinstance(store, dict):
            store = {}
        samples = store.get(region) or []
        bucket = (word_count // 100) * 100
        samples = [s for s in samples if (int(s[0]) // 100) * 100 != bucket]
        samples.append([word_count, font_pt])
        samples.sort(key=lambda s: int(s[0]))
        if len(samples) > _FONT_CACHE_MAX_SAMPLES:
            samples = samples[-_FONT_CACHE_MAX_SAMPLES:]
        store[region] = samples
        backend.set(_FONT_CACHE_V2_KEY, json.dumps(store))
    except Exception:  # noqa: BLE001
        log.exception("Could not save PDF font cache region=%s", region)


def _predict_font(region: str, word_count: int) -> float:
    s = _load_font_samples(region)
    if not s:
        return _DEFAULT_FONT_GUESS
    s = sorted(s, key=lambda x: x[0])
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


# ── Placeholder + native lib loader ──────────────────────────────────────


def _ensure_dll_path() -> None:
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


_PLACEHOLDER_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj <<>> endobj\n"
    b"2 0 obj << /Type /Catalog /Pages 3 0 R >> endobj\n"
    b"3 0 obj << /Type /Pages /Count 1 /Kids [4 0 R] >> endobj\n"
    b"4 0 obj << /Type /Page /Parent 3 0 R /MediaBox [0 0 612 792] >> endobj\n"
    b"trailer << /Root 2 0 R >>\n%%EOF\n"
)


class PdfSkipped(Exception):
    """Raised when the structured response is shaped such that producing
    a meaningful PDF isn't possible (currently: exactly 1 news section,
    which has no defensible 70/30 split). The caller catches this and
    stores the HTML edition without a PDF column."""


# ── Drop logic (per-band news + per-rail) ────────────────────────────────


# Per news.pr spec: sections to drop first when content is too dense.
# Each entry is a regex matched against the <h2> text of a section.
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
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser")


def _drop_rank(title: str) -> int:
    title_l = title.lower()
    for i, p in enumerate(_DROP_PRIORITY):
        if p.search(title_l):
            return i
        if p.search(title):
            return i
    return len(_DROP_PRIORITY)


def _band_section_groups(soup):
    """Group a band's flat ``<section>`` siblings into header + stories
    buckets. The band root has no wrapper div (it's the inner HTML of the
    band) — siblings are the direct children of the parsed fragment."""
    groups: list[tuple[object, list[object], str]] = []
    current_header = None
    current_stories: list[object] = []
    current_title = ""
    for sec in soup.find_all("section", recursive=True):
        # Only top-level news-header / news-story (subsections aren't
        # nested as <section>, so this works.)
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


def _drop_one_article(band_html: str) -> str | None:
    """Drop the last story from the section with the most stories (tiebreak
    by ``_DROP_PRIORITY``, lowest priority first). Keeps at least 1 story
    per section so headers don't end up orphaned. Returns None if no
    section has ≥ 2 stories."""
    soup = _parse(band_html)
    groups = _band_section_groups(soup)
    candidates = [g for g in groups if len(g[1]) >= 2]
    if not candidates:
        return None
    candidates.sort(key=lambda g: (-len(g[1]), _drop_rank(g[2])))
    _header, stories, title = candidates[0]
    last = stories[-1]
    log.info(
        "PDF: dropping last story of %r (%d → %d stories)",
        title[:60],
        len(stories),
        len(stories) - 1,
    )
    prev = last.find_previous_sibling()
    if prev is not None and "sep-story" in (prev.get("class") or []):
        prev.decompose()
    last.decompose()
    return str(soup)


def _drop_one_section(band_html: str) -> str | None:
    """Last-resort: drop an entire section (header + all stories) by
    ``_DROP_PRIORITY``. Also removes the preceding ``sep-group`` rule so
    the band doesn't start with a doubled separator."""
    soup = _parse(band_html)
    groups = _band_section_groups(soup)
    if not groups:
        return None

    def _kill(group_idx: int, reason: str) -> str:
        header, stories, title = groups[group_idx]
        prev = header.find_previous_sibling()
        if prev is not None and "sep-group" in (prev.get("class") or []):
            prev.decompose()
        log.info(
            "PDF: dropping category %r (header + %d stories) — %s", title[:60], len(stories), reason
        )
        for s in stories:
            s.decompose()
        header.decompose()
        return str(soup)

    for pattern in _DROP_PRIORITY:
        for i in range(len(groups) - 1, -1, -1):
            if pattern.search(groups[i][2].lower()) or pattern.search(groups[i][2]):
                return _kill(i, "matched drop-priority pattern")
    return _kill(len(groups) - 1, "last-resort: no priority match")


def _drop_one_movie(rail_html: str) -> str | None:
    soup = _parse(rail_html)
    cards = soup.find_all("div", class_="movie-card")
    if not cards:
        return None
    last = cards[-1]
    title_el = last.find(class_="movie-title")
    title = (title_el.get_text(strip=True) if title_el else "?")[:60]
    log.info("PDF: dropping movie %r (%d → %d cards)", title, len(cards), len(cards) - 1)
    last.decompose()
    return str(soup)


def _drop_one_calendar_event(rail_html: str) -> str | None:
    soup = _parse(rail_html)
    events = soup.find_all("div", class_="cal-event")
    if not events:
        return None
    last = events[-1]
    text = last.get_text(strip=True)[:60]
    log.info("PDF: dropping calendar event %r (%d → %d events)", text, len(events), len(events) - 1)
    last.decompose()
    return str(soup)


def _extract_rail_blocks(rail_inner_html: str) -> dict[str, str]:
    """Pull calendar/movies inner HTML out of a (possibly trimmed) rail
    fragment. Used to persist the rail that *actually fit* so a same-day
    refresh can skip the rail fit entirely."""
    soup = _parse(rail_inner_html)
    out = {"calendar_html": "", "movies_html": ""}
    for block in soup.find_all("div", class_="rail-block"):
        is_movies = block.find(class_="movie-card") is not None
        is_calendar = (
            block.find(class_="cal-event") is not None or block.find(class_="cal-date") is not None
        )
        inner = "".join(str(c) for c in block.children)
        if is_movies and not is_calendar:
            out["movies_html"] = inner
        elif is_calendar and not is_movies:
            out["calendar_html"] = inner
    return out


# ── Region fit primitive ─────────────────────────────────────────────────


@dataclass
class _FitResult:
    inner_html: str
    font_pt: float
    blank_ratio: float


def _make_url_fetcher(fetch_cache: dict[str, dict]):
    """Build a WeasyPrint url_fetcher that caches successful fetches for the
    duration of one PDF render call. Each region's fit binary-search
    re-renders the same content many times — caching avoids re-downloading
    masthead font + movie backdrops on every render.
    """
    from weasyprint import default_url_fetcher

    ua = "Linh-News/1.0 (https://github.com/vtlinh/linh-news; vtlinh87+linhnews@gmail.com)"

    def _fetch(url, *args, **kwargs):
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

    return _fetch


def _find_box_by_tag(root, tag: str):
    """Depth-first search for the first laid-out box whose element_tag
    matches ``tag`` (e.g. 'body', 'html'). WeasyPrint annotates content
    boxes with ``element_tag``; structural boxes (PageBox, MarginBox) do
    not have a matching tag, so this skips them naturally."""
    if getattr(root, "element_tag", None) == tag:
        return root
    for child in getattr(root, "children", ()) or ():
        found = _find_box_by_tag(child, tag)
        if found is not None:
            return found
    return None


def _deepest_descendant_bottom(box) -> float:
    """Return the deepest ``position_y + height`` reached by any descendant
    of ``box`` (in WeasyPrint CSS-px units). Used for both fit checks
    (compare to page height) and natural-height measurement (subtract the
    container's own position_y to get content height)."""
    deepest = 0.0
    try:
        y = float(getattr(box, "position_y", 0) or 0)
        h = float(getattr(box, "height", 0) or 0)
        deepest = y + h
    except (TypeError, ValueError):
        pass

    def _walk(b):
        nonlocal deepest
        try:
            y = float(getattr(b, "position_y", 0) or 0)
            h = float(getattr(b, "height", 0) or 0)
            if h > 0 or y > 0:
                bottom = y + h
                if bottom > deepest:
                    deepest = bottom
        except (TypeError, ValueError):
            pass
        for child in getattr(b, "children", ()) or ():
            _walk(child)

    for child in getattr(box, "children", ()) or ():
        _walk(child)
    return deepest


def _render_doc(html_str: str, url_fetcher):
    """Render a single-region HTML string and return (doc, pdf_bytes,
    n_pages, deepest_px, page_h_px).

    ``deepest_px`` is the deepest content position reached *inside the
    body element*. We deliberately exclude the PageBox / margin boxes /
    HtmlBox from this measurement so a tall measurement page (e.g. 30in
    used for natural-height pre-pass) doesn't make ``deepest`` equal the
    whole page height. The legacy single-doc-fit code relied on
    ``deepest`` strictly for blank-ratio reporting, but the new pipeline
    branches on ``deepest > page_h_px`` to detect region overflow — so
    accuracy matters now.
    """
    from weasyprint import HTML

    doc = HTML(string=html_str, url_fetcher=url_fetcher).render()
    pdf_bytes = doc.write_pdf()
    n_pages = len(doc.pages)
    if n_pages == 0:
        return doc, pdf_bytes, 0, 0.0, 0.0
    page = doc.pages[0]
    page_h_px = float(getattr(page, "height", 0) or 0)
    root = getattr(page, "_page_box", None)
    deepest = 0.0
    if root is not None:
        body = _find_box_by_tag(root, "body")
        if body is None:
            # Fall back to walking the whole page if no body box was found
            # (shouldn't happen for our HTML, but be defensive).
            body = root
        deepest = _deepest_descendant_bottom(body)
        # Convert "deepest absolute position" to a height-from-body-top by
        # subtracting body's own top. (Body sits at y≈0 in our wrappers,
        # but be precise.)
        body_top = float(getattr(body, "position_y", 0) or 0)
        deepest = max(0.0, deepest - body_top)
    return doc, pdf_bytes, n_pages, deepest, page_h_px


def _fit_region(
    name: str,
    inner_html: str,
    *,
    width_in: float,
    height_in: float,
    region_css: str,
    font_face_css: str,
    drop_fns: list,
    url_fetcher,
) -> _FitResult | None:
    """Binary-search ``[FONT_MIN, FONT_MAX]`` at 0.1pt for the largest font
    where ``inner_html`` fits in a ``width_in × height_in`` box. On
    overflow at MIN, call ``drop_fns`` in order until one yields a smaller
    fragment, then retry (up to 20 attempts).

    Returns the chosen font, the (possibly trimmed) inner HTML, and the
    blank ratio of the final layout. Returns ``None`` if nothing can be
    dropped to make MIN fit.
    """
    current = inner_html
    t0 = time.monotonic()
    for attempt in range(20):
        result = _try_fit(
            name,
            current,
            width_in=width_in,
            height_in=height_in,
            region_css=region_css,
            font_face_css=font_face_css,
            url_fetcher=url_fetcher,
        )
        if result is not None:
            _save_font_sample(name, _count_words(current), result.font_pt)
            log.info(
                "PDF region %s fit in %.2fs (font=%.1fpt, blank=%.1f%%, %d drops)",
                name,
                time.monotonic() - t0,
                result.font_pt,
                result.blank_ratio * 100,
                attempt,
            )
            return _FitResult(
                inner_html=current,
                font_pt=result.font_pt,
                blank_ratio=result.blank_ratio,
            )
        trimmed = None
        for fn in drop_fns:
            trimmed = fn(current)
            if trimmed is not None:
                break
        if trimmed is None:
            log.warning("PDF region %s: nothing left to drop; giving up", name)
            return None
        current = trimmed
    log.error("PDF region %s: still overflowing after 20 drops", name)
    return None


def _try_fit(
    name: str,
    inner_html: str,
    *,
    width_in: float,
    height_in: float,
    region_css: str,
    font_face_css: str,
    url_fetcher,
    font_min_override: float | None = None,
    font_max_override: float | None = None,
) -> _FitResult | None:
    """One pass of the MIN-then-grow binary search. ``None`` means MIN
    doesn't fit (caller should drop). ``font_min_override`` lowers the
    floor below the module-level ``_FONT_MIN`` — used by the headline
    fit where dense long-form prose needs to fit in 1/3 of news area.
    ``font_max_override`` caps the ceiling below ``_FONT_MAX`` — used by
    the headline body to keep it from outsizing the rest of the page."""
    page_h_target_in = height_in
    font_min = font_min_override if font_min_override is not None else _FONT_MIN
    font_max = font_max_override if font_max_override is not None else _FONT_MAX
    min_html = build_single_region_html(
        inner_html,
        width_in=width_in,
        height_in=height_in,
        font_pt=font_min,
        region_css=region_css,
        font_face_css=font_face_css,
    )
    _doc, _pdf, n_pages, deepest, page_h_px = _render_doc(min_html, url_fetcher)
    tried: list[str] = []
    fits_at_min = n_pages <= 1 and (page_h_px <= 0 or deepest <= page_h_px)
    tried.append(f"{font_min:.1f}{'✓' if fits_at_min else '✗'}")
    if not fits_at_min:
        log.info("PDF region %s: MIN font doesn't fit (%s)", name, " ".join(tried))
        return None

    best_pt = font_min
    best_blank = 1.0 if page_h_px <= 0 else max(0.0, 1.0 - deepest / page_h_px)

    # Seed binary search with the cache-predicted font (clipped to window)
    # — gives us a much better starting point than blind midpoints.
    seed = _floor_step(_predict_font(name, _count_words(inner_html)))
    seed = max(font_min, min(font_max, seed))
    if seed > font_min:
        seed_html = build_single_region_html(
            inner_html,
            width_in=width_in,
            height_in=height_in,
            font_pt=seed,
            region_css=region_css,
            font_face_css=font_face_css,
        )
        _doc, _pdf, n_pages, deepest, page_h_px = _render_doc(seed_html, url_fetcher)
        fits = n_pages <= 1 and (page_h_px <= 0 or deepest <= page_h_px)
        tried.append(f"{seed:.1f}{'✓' if fits else '✗'}")
        if fits:
            best_pt = seed
            best_blank = max(0.0, 1.0 - deepest / page_h_px)
            lo, hi = seed, font_max
        else:
            lo, hi = font_min, seed
    else:
        lo, hi = font_min, font_max

    # Binary-search upward for the biggest font that fits.
    while hi - lo > _FONT_STEP:
        mid = _round_step((lo + hi) / 2)
        if mid <= lo or mid >= hi:
            break
        mid_html = build_single_region_html(
            inner_html,
            width_in=width_in,
            height_in=height_in,
            font_pt=mid,
            region_css=region_css,
            font_face_css=font_face_css,
        )
        _doc, _pdf, n_pages, deepest, page_h_px = _render_doc(mid_html, url_fetcher)
        fits = n_pages <= 1 and (page_h_px <= 0 or deepest <= page_h_px)
        tried.append(f"{mid:.1f}{'✓' if fits else '✗'}")
        if fits:
            best_pt = mid
            best_blank = max(0.0, 1.0 - deepest / page_h_px) if page_h_px > 0 else 0.0
            lo = mid
        else:
            hi = mid

    log.info(
        "PDF region %s fit (W=%.2fin H=%.2fin): %s → %.1fpt blank=%.1f%%",
        name,
        width_in,
        page_h_target_in,
        " ".join(tried),
        best_pt,
        best_blank * 100,
    )
    return _FitResult(inner_html=inner_html, font_pt=best_pt, blank_ratio=best_blank)


def _measure_natural_height_in(
    inner_html: str,
    *,
    width_in: float,
    font_pt: float,
    region_css: str,
    font_face_css: str,
    url_fetcher,
) -> float:
    """Render ``inner_html`` into a tall page sized exactly to ``width_in``
    and report the natural content height (deepest box's bottom) in
    inches. Used to size the top and stocks regions before the news/rail
    regions are fit. Adds a tiny 0.05in pad so close-fitting content
    doesn't get pinched."""
    from app.pdf_renderer import build_measure_only_html

    html_str = build_measure_only_html(
        inner_html,
        width_in=width_in,
        font_pt=font_pt,
        region_css=region_css,
        font_face_css=font_face_css,
    )
    _doc, _pdf, _n, deepest_px, _page_h = _render_doc(html_str, url_fetcher)
    return deepest_px / _PX_PER_IN + 0.05


# ── Headline (front-page hero) fit ───────────────────────────────────────


@dataclass
class _HeadlineFit:
    col_span: int                # 2 or 3
    box_w_in: float
    headline_h_in: float
    image_w_in: float
    image_h_in: float
    image_data_uri: str
    body_font_pt: float
    title_font_pt: float
    # Body's per-column slice height. With column-fill: auto and a fixed
    # body height, WeasyPrint fills column 1 to this height before
    # overflowing into column 2 (and column 3 when col_span == 3).
    body_col_h_in: float


def _fit_headline(
    parts: PdfParts,
    *,
    news_w_in: float,
    upper_h_total_in: float,
    url_fetcher,
) -> _HeadlineFit | None:
    """Decide column span (2 vs 3), font size, and image dimensions for
    the headline box. Pre-scales the image bytes to exact pixels so
    WeasyPrint does no runtime scaling. Returns ``None`` if neither
    column span fits even at the floor font."""
    from app import images as _images

    body_html = parts.headline_body_html or ""
    if not body_html.strip():
        return None

    col_gap_in = 14 / 72
    col_w_in = (news_w_in - 3 * col_gap_in) / 4
    pad_top_in = 8 / 72
    pad_bottom_in = 3 / 72  # tighter bottom — body's last column rarely fills to the corner
    border_in = 1 / 72
    img_text_gap_in = 6 / 72
    # Title is hard-coded at 18pt. We measure its actual rendered height
    # per col_span (since title wraps differently at 2-col vs 3-col box
    # width) instead of reserving a generous flat 45pt — the over-reserve
    # was causing visible empty space at the bottom of the headline box.
    title_font_pt = 18.0
    title_html = f'<h3>{_esc(parts.headline_title or "")}</h3>'
    title_css = headline_title_region_css(title_font_pt)

    # Headline box height fixed at body_h/3. body_h is the printable region
    # minus masthead and stocks footer; ``upper_h_total_in`` is 70% of body_h.
    body_h_total_in = upper_h_total_in / 0.7
    headline_min_h_in = body_h_total_in / 3.0
    headline_max_h_in = body_h_total_in / 3.0
    img_max_w_in = news_w_in / 2.0
    # Image height is capped so the body always gets enough vertical room.
    img_max_h_in = 3.0
    aspect = parts.headline_image_aspect or 16 / 9  # fallback aspect

    # Headline is always 3 columns wide per user spec. Try image-height
    # candidates from biggest to smallest, keeping image as long as the
    # body can fit at the floor font.
    img_height_candidates = (img_max_h_in, 2.5, 2.0, 1.5, 1.25, 1.0, 0.75, 0.5, 0.0)
    col_span = 3

    for img_h_cap in img_height_candidates:
        # 3-col headline width = 3 cols + 2 inter-col gaps. The right
        # pane sits in the standard 4-col grid's column 4 (1 col wide),
        # separated by a single col-gap from this box.
        box_w_in = col_span * col_w_in + (col_span - 1) * col_gap_in
        inner_w_in = box_w_in - 2 * pad_top_in - 2 * border_in
        if parts.headline_image_bytes and img_h_cap > 0:
            img_w_in = min(inner_w_in, img_max_w_in)
            img_h_in = img_w_in / aspect if aspect > 0 else 0
            if img_h_in > img_h_cap:
                img_h_in = img_h_cap
                img_w_in = img_h_in * aspect
        else:
            img_w_in = 0.0
            img_h_in = 0.0
        # Measure actual title height at this col_span's inner width.
        try:
            title_h_in = _measure_natural_height_in(
                title_html,
                width_in=inner_w_in,
                font_pt=title_font_pt,
                region_css=title_css,
                font_face_css=parts.font_face_css,
                url_fetcher=url_fetcher,
            )
        except Exception:  # noqa: BLE001
            log.exception("Could not measure headline title height; using 45pt fallback")
            title_h_in = 45 / 72
        chrome_in = pad_top_in + pad_bottom_in + 2 * border_in + title_h_in
        if img_h_in > 0:
            chrome_in += img_h_in + img_text_gap_in
        text_h_max = headline_max_h_in - chrome_in
        if text_h_max < 0.4:
            continue

        fit = _try_fit(
            f"headline-{col_span}c",
            body_html,
            width_in=inner_w_in,
            height_in=text_h_max,
            region_css=headline_body_region_css(col_span),
            font_face_css=parts.font_face_css,
            url_fetcher=url_fetcher,
            # Headline body uses the same 10pt floor as the news bands.
            font_min_override=10.0,
            # Cap the headline body at 13.5pt so it doesn't outsize the
            # rest of the page when the content is short enough to grow
            # past the regular news font.
            font_max_override=13.5,
        )
        if fit is None:
            continue

        # Measure the body's actual rendered height at the chosen font in
        # col_span layout. With column-fill: balance + body height = this
        # measured value, content fills both columns evenly with no
        # ragged whitespace at the bottom.
        try:
            measured_h_in = _measure_natural_height_in(
                body_html,
                width_in=inner_w_in,
                font_pt=fit.font_pt,
                region_css=headline_body_region_css(col_span),
                font_face_css=parts.font_face_css,
                url_fetcher=url_fetcher,
            )
        except Exception:  # noqa: BLE001
            log.exception("Could not measure headline col_span height; using cap")
            measured_h_in = text_h_max
        body_col_h_in = min(text_h_max, max(0.3, measured_h_in))
        # Box is always exactly body_h/3 (min == max). Content may be
        # shorter — empty space sits below the balanced body columns.
        headline_h_in = headline_min_h_in

        image_data_uri = ""
        if parts.headline_image_bytes and img_w_in > 0 and img_h_in > 0:
            # Embed at 200 PPI for print-quality sharpness. The CSS keeps
            # the displayed size in inches so PDF readers rasterize the
            # high-resolution bitmap into the smaller physical box.
            img_w_px = max(1, round(img_w_in * 200))
            img_h_px = max(1, round(img_h_in * 200))
            resized = _images.resize_to_box(
                parts.headline_image_bytes, img_w_px, img_h_px
            )
            if resized is not None:
                data, mime, _w, _h = resized
                image_data_uri = _images.to_data_uri(data, mime)
            else:
                # Decode failed — fall through text-only.
                headline_h_in -= img_h_in + img_text_gap_in
                img_w_in = 0.0
                img_h_in = 0.0

        log.info(
            "PDF: headline fit at %d cols, box=%.2fx%.2fin, img=%.2fx%.2fin, "
            "font=%.1fpt, body-col=%.2fin",
            col_span, box_w_in, headline_h_in, img_w_in, img_h_in,
            fit.font_pt, body_col_h_in,
        )
        return _HeadlineFit(
            col_span=col_span,
            box_w_in=box_w_in,
            headline_h_in=headline_h_in,
            image_w_in=img_w_in,
            image_h_in=img_h_in,
            image_data_uri=image_data_uri,
            body_font_pt=fit.font_pt,
            # Hard-coded 18pt — independent of the body font so the title
            # always has the same visual weight at the front of the page.
            title_font_pt=title_font_pt,
            body_col_h_in=body_col_h_in,
        )
    return None


def _split_sections_by_area(
    sections: list[dict],
    *,
    first_area: float,
    second_area: float,
) -> tuple[list[dict], list[dict]]:
    """Sub-split upper-band sections into two panes by word count
    proportional to area, preserving LLM order. ``sections[:k]`` goes to
    the first pane (target area = ``first_area``), the rest to the second."""
    from app.pdf_renderer import _section_word_count

    if not sections:
        return [], []
    total_area = max(1e-6, first_area + second_area)
    target_first_ratio = first_area / total_area
    counts = [_section_word_count(s) for s in sections]
    total_words = sum(counts) or 1
    best_k = 0
    best_diff = float("inf")
    for k in range(0, len(sections) + 1):
        first_ratio = sum(counts[:k]) / total_words
        diff = abs(first_ratio - target_first_ratio)
        if diff < best_diff:
            best_diff = diff
            best_k = k
    return sections[:best_k], sections[best_k:]


# ── Top-level entry point ────────────────────────────────────────────────


def html_to_pdf(parts: PdfParts, *, cached_rail: dict | None = None) -> bytes:
    """Convenience wrapper that returns only the PDF bytes."""
    pdf_bytes, _, _ = html_to_pdf_ex(parts, cached_rail=cached_rail)
    return pdf_bytes


def html_to_pdf_ex(
    parts: PdfParts,
    *,
    cached_rail: dict | None = None,
) -> tuple[bytes, float | None, dict[str, str] | None]:
    """Render ``parts`` to a single-page broadsheet PDF.

    Returns ``(pdf_bytes, rail_font_pt, trimmed_rail)``:

    * ``rail_font_pt`` — the font the rail was fit at. ``None`` when the
      rail came from ``cached_rail`` (no fresh fit), or when the
      placeholder PDF is returned.
    * ``trimmed_rail`` — ``{"calendar_html", "movies_html"}`` for the rail
      that actually fit. ``None`` when ``cached_rail`` was supplied (the
      caller keeps its existing cache).

    ``cached_rail`` (optional): when a previous run on the same day
    already produced a fitted rail, pass ``{"calendar_html", "movies_html",
    "font_pt"}`` here. We skip the rail fit entirely, lock the rail to the
    cached HTML/font, and only fit the upper + lower news bands.

    Raises ``PdfSkipped`` when the structured response contains exactly
    one section — a 70/30 split has no meaning there. The caller should
    catch this and store the HTML edition without a PDF.
    """
    if parts.section_count == 1:
        raise PdfSkipped("Exactly one news section: 70/30 split is undefined, skipping PDF")

    try:
        _ensure_dll_path()
        from weasyprint import HTML  # noqa: F401  (probe only)
    except (OSError, ImportError) as e:
        log.warning("WeasyPrint native libs unavailable, using placeholder PDF: %s", e)
        return _PLACEHOLDER_PDF, None, None

    fetch_cache: dict[str, dict] = {}
    url_fetcher = _make_url_fetcher(fetch_cache)

    overall_t0 = time.monotonic()
    log.info("PDF: ── pipeline begin (5-region fit) ──")

    inner_w_in = _PAGE_W_IN - 2 * _PAGE_MARGIN_IN
    inner_h_in = _PAGE_H_IN - 2 * _PAGE_MARGIN_IN

    # ── Pre-pass: measure top + stocks at their fixed body fonts ────────
    # Sanity caps: if the measurement comes back pathologically large
    # (broken layout, font load failure, etc.) we fall back to fixed
    # defaults so the news/rail regions still get a reasonable share of
    # vertical space. These caps are intentionally generous — anything
    # within them is trusted as real.
    _TOP_MAX_IN = 3.0
    _TOP_DEFAULT_IN = 1.6
    _STOCKS_MAX_IN = 1.0
    _STOCKS_DEFAULT_IN = 0.5

    top_h_in_raw = _measure_natural_height_in(
        parts.top_inner_html,
        width_in=inner_w_in,
        font_pt=_TOP_FONT_PT,
        region_css=top_region_css(),
        font_face_css=parts.font_face_css,
        url_fetcher=url_fetcher,
    )
    if top_h_in_raw <= 0 or top_h_in_raw > _TOP_MAX_IN:
        log.warning(
            "PDF: top region measured %.3fin (out of [0, %.2f]in) — "
            "falling back to default %.2fin",
            top_h_in_raw, _TOP_MAX_IN, _TOP_DEFAULT_IN,
        )
        top_h_in = _TOP_DEFAULT_IN
    else:
        top_h_in = top_h_in_raw
    log.info("PDF: top region height = %.3fin (raw=%.3fin)", top_h_in, top_h_in_raw)

    if parts.stocks_inner_html.strip():
        stocks_h_in_raw = _measure_natural_height_in(
            parts.stocks_inner_html,
            width_in=inner_w_in,
            font_pt=_STOCKS_FONT_PT,
            region_css=stocks_region_css(),
            font_face_css=parts.font_face_css,
            url_fetcher=url_fetcher,
        )
        if stocks_h_in_raw <= 0 or stocks_h_in_raw > _STOCKS_MAX_IN:
            log.warning(
                "PDF: stocks region measured %.3fin (out of [0, %.2f]in) — "
                "falling back to default %.2fin",
                stocks_h_in_raw, _STOCKS_MAX_IN, _STOCKS_DEFAULT_IN,
            )
            stocks_h_in = _STOCKS_DEFAULT_IN
        else:
            stocks_h_in = stocks_h_in_raw
        log.info(
            "PDF: stocks region height = %.3fin (raw=%.3fin)",
            stocks_h_in, stocks_h_in_raw,
        )
    else:
        stocks_h_in = 0.0

    body_h_in = inner_h_in - top_h_in - stocks_h_in
    if body_h_in <= 1.0:
        log.error("PDF: body height collapsed to %.2fin; aborting", body_h_in)
        return _PLACEHOLDER_PDF, None, None

    news_w_in = inner_w_in - _RAIL_W_IN - _CONTENT_GAP_IN
    # Two heights per band:
    #   * ``*_h_total``: the band's box height in the final assembly
    #     (border-box, includes any internal chrome).
    #   * ``*_h_fit``  : the height passed to the fit binary search —
    #     equals the band's *content* area (i.e. ``total - chrome``) plus
    #     a tiny safety buffer subtracted so a borderline-fitting last line
    #     doesn't get its descender clipped in the final paginated render.
    # The lower band carries a border-top (0.75pt) and padding-top (4pt)
    # that eat from its content area. The upper band has no chrome.
    # The safety buffer is ~3pt (≈0.04in); chosen to be smaller than any
    # body-text line height so we don't waste a full line of vertical
    # space, but large enough to absorb sub-pt rounding between the
    # fit-pass page render and the multi-region assembly render.
    # The band-divider element between upper and lower contributes
    # ``_BAND_DIVIDER_IN`` of stacked height: 1.5pt top border + 1.5pt
    # padding + 0.75pt bottom border + 4pt margin-bottom = 7.75pt total.
    _BAND_SAFETY_IN = 3 / 72
    _BAND_DIVIDER_IN = (1.5 + 1.5 + 0.75 + 4) / 72
    upper_h_total = (body_h_in - _BAND_DIVIDER_IN) * _UPPER_BAND_RATIO
    lower_h_total = body_h_in - _BAND_DIVIDER_IN - upper_h_total
    upper_h_fit = max(0.5, upper_h_total - _BAND_SAFETY_IN)
    lower_h_fit = max(0.5, lower_h_total - _BAND_SAFETY_IN)

    log.info(
        "PDF: body=%.2fin, news_w=%.2fin, upper_total=%.2fin (fit=%.2fin), "
        "divider=%.2fin, lower_total=%.2fin (fit=%.2fin)",
        body_h_in, news_w_in,
        upper_h_total, upper_h_fit,
        _BAND_DIVIDER_IN,
        lower_h_total, lower_h_fit,
    )

    # ── Headline fit (optional) ─────────────────────────────────────────
    # Decide column span, font size, body slice height, and image dims
    # before fitting the upper band. The upper band's content is then
    # sub-split into right-of-headline + below-headline slices, each
    # fit independently with the same _fit_region primitive.
    headline_fit: _HeadlineFit | None = None
    if parts.has_headline:
        headline_fit = _fit_headline(
            parts,
            news_w_in=news_w_in,
            upper_h_total_in=upper_h_total,
            url_fetcher=url_fetcher,
        )
        if headline_fit is None:
            log.warning("Headline did not fit at 2 or 3 cols — falling back to no-headline layout")

    # ── Fit upper + lower news bands ────────────────────────────────────
    if len(parts.news_bands) == 2:
        upper_inner, _ = parts.news_bands[0]
        lower_inner, _ = parts.news_bands[1]
    elif len(parts.news_bands) == 1:
        # 0 sections payload: single empty band; only chrome-only PDF.
        upper_inner, _ = parts.news_bands[0]
        lower_inner = ""
    else:
        upper_inner = ""
        lower_inner = ""

    upper_pt = _FONT_MIN
    lower_pt = _FONT_MIN
    fitted_upper = upper_inner
    fitted_lower = lower_inner
    fitted_upper_right_html = ""
    fitted_upper_bottom_html = ""
    upper_right_pt = _FONT_MIN
    upper_bottom_pt = _FONT_MIN

    if headline_fit is not None:
        # New side-by-side layout: split upper-band sections between
        #   * .upper-right pane: full upper-band height × 1 col width
        #   * .upper-bottom pane: (upper_h - headline_h) × box_w_in
        # Each fit independently via the existing _fit_region primitive.
        # The final .upper-right pane is rendered with border-left (0.5pt) +
        # padding-left (7pt) inside a border-box flex item whose outer width
        # is `news_u_right_w_in` (which already includes a 12pt safety
        # margin vs. raw column-4 width). The fit pass renders content into
        # a page of width = content-area width, so subtract the 7.5pt of
        # left chrome here too — otherwise the fit pass lays out at a
        # wider content area than the final document, and content overflows
        # the clip box in assembly.
        safety_pt = 12.0
        border_pad_pt = 7.5
        right_w_in = (
            news_w_in
            - headline_fit.box_w_in
            - _CONTENT_GAP_IN
            - (safety_pt + border_pad_pt) / 72.0
        )
        right_h_in = upper_h_fit  # right pane is FULL upper band height
        bottom_h_in = max(0.0, upper_h_fit - headline_fit.headline_h_in - 6 / 72)
        right_cols = 1
        upper_secs = parts.upper_sections or []
        # Section assignment order: first sections in LLM order go to the
        # upper-bottom pane (directly below the headline), then upper-right,
        # then lower band. _split_sections_by_area picks the area-proportional
        # split between bottom and right, preserving LLM order.
        below_sections, right_sections = _split_sections_by_area(
            upper_secs,
            first_area=headline_fit.box_w_in * bottom_h_in,
            second_area=right_w_in * right_h_in,
        )
        from app.pdf_renderer import _render_news_band  # local: avoid cycle

        right_html_raw = _render_news_band(
            right_sections, image_bytes_by_id=parts.image_bytes_by_id
        )
        below_html_raw = _render_news_band(
            below_sections, image_bytes_by_id=parts.image_bytes_by_id
        )
        right_css = news_region_css().replace(
            "column-count: 4;", f"column-count: {right_cols}; column-fill: auto;"
        )
        # .upper-bottom uses the same column count as the headline (3),
        # so news flowing under the box lines up visually.
        below_css = news_region_css().replace(
            "column-count: 4", f"column-count: {headline_fit.col_span}"
        )
        if right_html_raw.strip() and right_h_in > 0.3 and right_w_in > 0.5:
            ra = _fit_region(
                "upper-right",
                right_html_raw,
                width_in=right_w_in,
                height_in=right_h_in,
                region_css=right_css,
                font_face_css=parts.font_face_css,
                drop_fns=[_drop_one_article, _drop_one_section],
                url_fetcher=url_fetcher,
            )
            if ra is None:
                log.warning("Upper-right unfittable; rendering empty pane")
            else:
                fitted_upper_right_html = ra.inner_html
                upper_right_pt = ra.font_pt
        if below_html_raw.strip() and bottom_h_in > 0.3:
            bo = _fit_region(
                "upper-bottom",
                below_html_raw,
                width_in=headline_fit.box_w_in,
                height_in=bottom_h_in,
                region_css=below_css,
                font_face_css=parts.font_face_css,
                drop_fns=[_drop_one_article, _drop_one_section],
                url_fetcher=url_fetcher,
            )
            if bo is None:
                log.warning("Upper-bottom unfittable; rendering empty pane")
            else:
                fitted_upper_bottom_html = bo.inner_html
                upper_bottom_pt = bo.font_pt
        # Headline path renders the upper band itself — suppress the
        # legacy upper-band fit. (When headline_fit is None we left
        # upper_inner as the pre-rendered upper band, so the legacy path
        # below renders it as a normal 4-col upper band.)
        upper_inner = ""

    if upper_inner.strip():
        up = _fit_region(
            "upper-news",
            upper_inner,
            width_in=news_w_in,
            height_in=upper_h_fit,
            region_css=news_region_css(),
            font_face_css=parts.font_face_css,
            drop_fns=[_drop_one_article, _drop_one_section],
            url_fetcher=url_fetcher,
        )
        if up is None:
            log.error("PDF: upper band unfittable; placeholder")
            return _PLACEHOLDER_PDF, None, None
        upper_pt = up.font_pt
        fitted_upper = up.inner_html

    if lower_inner.strip():
        lo = _fit_region(
            "lower-news",
            lower_inner,
            width_in=news_w_in,
            height_in=lower_h_fit,
            region_css=news_region_css(),
            font_face_css=parts.font_face_css,
            drop_fns=[_drop_one_article, _drop_one_section],
            url_fetcher=url_fetcher,
        )
        if lo is None:
            log.error("PDF: lower band unfittable; placeholder")
            return _PLACEHOLDER_PDF, None, None
        lower_pt = lo.font_pt
        fitted_lower = lo.inner_html

    # ── Fit (or reuse) the rail ─────────────────────────────────────────
    rail_pt: float | None = None
    trimmed_rail: dict[str, str] | None = None
    if cached_rail is not None:
        # Caller has already fit the rail today. Trust it.
        rail_inner = parts.rail_inner_html  # already the cached HTML
        rail_pt_cached = cached_rail.get("font_pt")
        rail_pt = (
            float(rail_pt_cached)
            if isinstance(rail_pt_cached, (int, float))
            else _DEFAULT_FONT_GUESS
        )
        log.info("PDF: rail reused from cache (font=%.1fpt)", rail_pt)
    else:
        rail_inner_init = parts.rail_inner_html
        if rail_inner_init.strip():
            # The final ``aside.rail`` is border-box with border-left 0.5pt
            # + padding-left 8pt, so its content area is narrower than
            # ``_RAIL_W_IN``. The fit pass renders into a page sized to the
            # exact content width, so subtract that chrome here — otherwise
            # the fit lays out at a wider area than the document and the
            # right edge of every rail line gets clipped in assembly.
            rail_border_pad_in = (0.5 + 8) / 72
            rail_fit_w_in = _RAIL_W_IN - rail_border_pad_in
            ra = _fit_region(
                "rail",
                rail_inner_init,
                width_in=rail_fit_w_in,
                height_in=body_h_in,
                region_css=rail_region_css(),
                font_face_css=parts.font_face_css,
                drop_fns=[_drop_one_movie, _drop_one_calendar_event],
                url_fetcher=url_fetcher,
            )
            if ra is None:
                log.warning("PDF: rail unfittable even after drops; using minimal rail")
                rail_inner_init = ""
                rail_pt = _FONT_MIN
            else:
                rail_pt = ra.font_pt
                rail_inner_init = ra.inner_html
        else:
            rail_pt = _FONT_MIN
        rail_inner = rail_inner_init
        trimmed_rail = _extract_rail_blocks(rail_inner)

    # ── Assemble + render once more (the real PDF) ──────────────────────
    fitted_has_headline = headline_fit is not None
    fitted_parts = PdfParts(
        top_inner_html=parts.top_inner_html,
        news_bands=[(fitted_upper, _count_words(fitted_upper))]
        + ([(fitted_lower, _count_words(fitted_lower))] if len(parts.news_bands) == 2 else []),
        rail_inner_html=rail_inner,
        stocks_inner_html=parts.stocks_inner_html,
        font_face_css=parts.font_face_css,
        masthead_title_pt=parts.masthead_title_pt,
        section_count=parts.section_count,
        has_headline=fitted_has_headline,
        headline_title=parts.headline_title,
        headline_body_html=parts.headline_body_html,
        headline_word_count=parts.headline_word_count,
        headline_image_bytes=parts.headline_image_bytes,
        headline_image_mime=parts.headline_image_mime,
        headline_image_aspect=parts.headline_image_aspect,
    )
    layout = AssemblyLayout(
        page_w_in=_PAGE_W_IN,
        page_h_in=_PAGE_H_IN,
        margin_in=_PAGE_MARGIN_IN,
        top_h_in=top_h_in,
        stocks_h_in=stocks_h_in,
        rail_w_in=_RAIL_W_IN,
        content_gap_in=_CONTENT_GAP_IN,
        upper_h_in=upper_h_total,
        lower_h_in=lower_h_total if len(parts.news_bands) == 2 else None,
        upper_font_pt=upper_pt,
        lower_font_pt=lower_pt if len(parts.news_bands) == 2 else None,
        rail_font_pt=rail_pt or _DEFAULT_FONT_GUESS,
        top_font_pt=_TOP_FONT_PT,
        stocks_font_pt=_STOCKS_FONT_PT,
        masthead_title_pt=parts.masthead_title_pt,
        headline_h_in=headline_fit.headline_h_in if headline_fit else 0.0,
        headline_box_w_in=headline_fit.box_w_in if headline_fit else 0.0,
        headline_col_span=headline_fit.col_span if headline_fit else 0,
        headline_image_w_in=headline_fit.image_w_in if headline_fit else 0.0,
        headline_image_h_in=headline_fit.image_h_in if headline_fit else 0.0,
        headline_image_data_uri=headline_fit.image_data_uri if headline_fit else "",
        headline_title=parts.headline_title,
        headline_body_html=parts.headline_body_html,
        headline_body_font_pt=headline_fit.body_font_pt if headline_fit else 0.0,
        headline_title_font_pt=headline_fit.title_font_pt if headline_fit else 0.0,
        headline_body_col_h_in=headline_fit.body_col_h_in if headline_fit else 0.0,
        upper_right_html=fitted_upper_right_html,
        upper_right_font_pt=upper_right_pt,
        upper_bottom_html=fitted_upper_bottom_html,
        upper_bottom_font_pt=upper_bottom_pt,
    )
    final_html = assemble_final_html(fitted_parts, layout)

    # Snapshot the assembled HTML for forensic debugging when overflow,
    # missing-image, or layout issues show up in the final PDF.
    try:
        from pathlib import Path

        snap_path = Path(__file__).resolve().parent.parent / "logs" / "pdf-assembled-latest.html"
        snap_path.parent.mkdir(exist_ok=True)
        snap_path.write_text(final_html, encoding="utf-8")
    except Exception:  # noqa: BLE001
        log.exception("Could not snapshot assembled HTML")

    from weasyprint import HTML as _HTML

    doc = _HTML(string=final_html, url_fetcher=url_fetcher).render()
    pdf_bytes = doc.write_pdf()
    n_pages = len(doc.pages)
    log.info(
        "PDF: ── pipeline end in %.2fs (n_pages=%d, %d bytes) ──",
        time.monotonic() - overall_t0,
        n_pages,
        len(pdf_bytes),
    )
    if n_pages != 1:
        log.warning(
            "PDF: final assembly produced %d pages (expected 1) — fit overrun, shipping anyway",
            n_pages,
        )
    return pdf_bytes, rail_pt, trimmed_rail


def page_count(pdf_bytes: bytes) -> int:
    """Cheap page-count from raw PDF bytes (used by tests)."""
    return pdf_bytes.count(b"/Type /Page") + pdf_bytes.count(b"/Type/Page")
