"""Scrape ``og:image`` (or ``twitter:image``) URLs from article pages.

The LLM can't be trusted to invent image URLs — it hallucinates plausible
looking CDN paths that 404 ~100% of the time. So instead, we ask it for
sources only and let the server fetch the article HTML, parse the
``<meta property="og:image">`` tag, and use that as the candidate.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

log = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 6
_MAX_BYTES = 512 * 1024  # 512KB of HTML head is plenty
_USER_AGENT = (
    "Mozilla/5.0 (compatible; Linh-News/1.0; +https://github.com/vtlinh/linh-news)"
)

# Match <meta property="og:image" content="..."> with attributes in any order.
# We accept ``og:image``, ``og:image:url``, and ``twitter:image`` as keys.
_META_RE = re.compile(
    r'<meta\b[^>]*?(?:property|name)\s*=\s*["\']'
    r"(og:image(?::url)?|twitter:image(?::src)?)"
    r'["\'][^>]*?content\s*=\s*["\']([^"\']+)["\']',
    re.IGNORECASE | re.DOTALL,
)
# Also catch the reversed attribute order (content first, then property/name).
_META_RE_REV = re.compile(
    r'<meta\b[^>]*?content\s*=\s*["\']([^"\']+)["\'][^>]*?'
    r'(?:property|name)\s*=\s*["\']'
    r"(og:image(?::url)?|twitter:image(?::src)?)"
    r'["\']',
    re.IGNORECASE | re.DOTALL,
)


def fetch_og_image(article_url: str) -> str | None:
    """Return the absolute URL of the article's og:image, or None if the
    page can't be fetched or doesn't expose one. Best-effort, never
    raises."""
    if not article_url or not article_url.startswith(("http://", "https://")):
        return None
    try:
        req = urllib.request.Request(
            article_url,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html,*/*;q=0.5"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
            head = resp.read(_MAX_BYTES)
            final_url = resp.url
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        log.info("og:image fetch failed (%s): %s", article_url, e)
        return None

    try:
        html = head.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None

    # Stop searching once the </head> closes — og: tags must live there.
    head_end = html.lower().find("</head>")
    haystack = html[:head_end] if head_end > 0 else html

    # First match in document order wins (sites typically put og:image
    # before twitter:image; we just take whichever appears first).
    candidates: list[tuple[int, str]] = []
    for m in _META_RE.finditer(haystack):
        candidates.append((m.start(), m.group(2)))
    for m in _META_RE_REV.finditer(haystack):
        candidates.append((m.start(), m.group(1)))
    if not candidates:
        return None
    candidates.sort()
    raw = candidates[0][1].strip()
    if not raw:
        return None
    # Resolve relative URLs against the (possibly redirected) page URL.
    absolute = urljoin(final_url, raw)
    if not urlparse(absolute).scheme.startswith("http"):
        return None
    return absolute
