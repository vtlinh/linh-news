"""Scrape ``og:image`` and supporting metadata from article pages.

The LLM can't be trusted to invent image URLs — it hallucinates plausible
looking CDN paths that 404 ~100% of the time. So instead, we ask it for
sources only and let the server fetch the article HTML, parse the
``<meta property="og:image">`` tag, and use that as the candidate.

Alongside the image URL we also pull the page's og:title (or ``<title>``)
and the og:image:alt — the caller uses these to verify that the image is
actually about the same story as the subsection's headline, instead of a
generic site banner served as og:image on a homepage or section page.
"""

from __future__ import annotations

import http.client
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

log = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 6
_MAX_BYTES = 512 * 1024  # 512KB of HTML head is plenty
_USER_AGENT = (
    "Mozilla/5.0 (compatible; Linh-News/1.0; +https://github.com/vtlinh/linh-news)"
)

_META_RE = re.compile(r"<meta\b([^>]*?)/?>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(
    r"""(\w[\w:-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""",
    re.IGNORECASE | re.DOTALL,
)
_TITLE_RE = re.compile(r"<title[^>]*>([^<]+)</title>", re.IGNORECASE | re.DOTALL)

_IMAGE_KEYS = {
    "og:image",
    "og:image:url",
    "og:image:secure_url",
    "twitter:image",
    "twitter:image:src",
}
_TITLE_KEYS = {"og:title", "twitter:title"}
_ALT_KEYS = {"og:image:alt", "twitter:image:alt"}


@dataclass(frozen=True)
class OgImage:
    url: str
    page_title: str
    alt: str


def _is_homepage(article_url: str) -> bool:
    """A homepage URL has no path segments (``https://site.com`` or
    ``https://site.com/``). og:image on these is the site banner, not a
    story-specific image, so we skip it entirely."""
    path = urlparse(article_url).path or ""
    return not [s for s in path.split("/") if s]


def _parse_attrs(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _ATTR_RE.finditer(raw):
        key = m.group(1).lower()
        val = m.group(2) if m.group(2) is not None else m.group(3)
        out[key] = val
    return out


def fetch_og_image(article_url: str) -> OgImage | None:
    """Return the og:image plus page title and image alt from
    ``article_url``, or None if the page can't be fetched, looks like a
    homepage, or has no og:image. Best-effort, never raises."""
    if not article_url or not article_url.startswith(("http://", "https://")):
        return None
    if _is_homepage(article_url):
        log.info("og:image skipped — homepage URL: %s", article_url)
        return None
    try:
        req = urllib.request.Request(
            article_url,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html,*/*;q=0.5"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
            head = resp.read(_MAX_BYTES)
            final_url = resp.url
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        http.client.HTTPException,
    ) as e:
        log.info("og:image fetch failed (%s): %s", article_url, e)
        return None

    try:
        html = head.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None

    head_end = html.lower().find("</head>")
    haystack = html[:head_end] if head_end > 0 else html

    image_url: str | None = None
    page_title: str = ""
    alt: str = ""

    for m in _META_RE.finditer(haystack):
        attrs = _parse_attrs(m.group(1))
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        content = (attrs.get("content") or "").strip()
        if not key or not content:
            continue
        if image_url is None and key in _IMAGE_KEYS:
            image_url = content
        elif not page_title and key in _TITLE_KEYS:
            page_title = content
        elif not alt and key in _ALT_KEYS:
            alt = content

    if not page_title:
        tm = _TITLE_RE.search(haystack)
        if tm:
            page_title = tm.group(1).strip()

    if not image_url:
        return None
    absolute = urljoin(final_url, image_url)
    if not urlparse(absolute).scheme.startswith("http"):
        return None
    return OgImage(url=absolute, page_title=page_title, alt=alt)
