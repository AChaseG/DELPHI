"""Self-healing for broken sources.

A source that keeps failing with a permanent-looking error — HTTP
401/403/404/410/451, an HTML page where a feed should be, or unparseable
XML — gets an automatic repair attempt during the ingest cycle:

 1. Feed autodiscovery: fetch the outlet's homepage (and the feed URL's own
    site root) and read ``<link rel="alternate" type="application/rss+xml">``
    tags plus visible RSS/feed links.
 2. Common feed paths on the outlet's host (/feed, /rss.xml, /atom.xml, …).
 3. For press sources, a Google News search feed scoped to the outlet's
    domain — the outlet's coverage keeps flowing even if its own feed is gone.

Every candidate is validated (fetched and parsed, must yield entries) before
the source is switched; the original URL is preserved in ``repaired_from`` so
the change is visible and reversible in the Sources panel. Transient failures
(timeouts, 429 rate limits, DNS blips) never trigger repair — they resolve on
their own and rewriting the URL for them would do harm.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlparse

import feedparser
import httpx
from sqlalchemy import select

from .models import Source, utcnow

log = logging.getLogger("repair")

AUTO_REPAIR = os.environ.get("NEWS_AUTO_REPAIR", "1") == "1"
REPAIR_AFTER = int(os.environ.get("NEWS_REPAIR_AFTER", "2"))  # consecutive failed cycles
REPAIR_MAX_PER_CYCLE = int(os.environ.get("NEWS_REPAIR_MAX_PER_CYCLE", "5"))
REPAIR_RETRY_HOURS = float(os.environ.get("NEWS_REPAIR_RETRY_HOURS", "6"))
REPAIR_TIMEOUT = float(os.environ.get("NEWS_REPAIR_TIMEOUT", "8"))
MAX_CANDIDATES = 10  # bound the work one broken source can cost per attempt

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
FEED_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"

_PERMANENT = re.compile(r"^error: (401|403|404|410|451)\b")
COMMON_PATHS = ["/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml",
                "/index.xml", "/feeds", "/?feed=rss2", "/feeds/posts/default?alt=rss"]

_FEED_TYPES = {"application/rss+xml", "application/atom+xml", "application/xml", "text/xml"}
_FEED_HREF = re.compile(r"(rss|atom|feed)", re.IGNORECASE)


def needs_repair(status: str) -> bool:
    """Does this fetch status look permanent enough to try rewiring the URL?"""
    s = status or ""
    return bool(_PERMANENT.match(s)) or s.startswith("error: not a feed") \
        or s.startswith("parse error")


def due_for_repair(source: Source) -> bool:
    return (AUTO_REPAIR
            and needs_repair(source.last_status)
            and (source.consecutive_failures or 0) >= REPAIR_AFTER
            and (source.last_repair_at is None
                 or utcnow() - source.last_repair_at >= timedelta(hours=REPAIR_RETRY_HOURS)))


class _FeedLinkParser(HTMLParser):
    """Collects feed URLs from <link rel=alternate> tags and rss-ish <a> hrefs."""

    def __init__(self, base_url: str):
        super().__init__()
        self.base = base_url
        self.links: list[str] = []    # autodiscovery <link> tags, best signal
        self.anchors: list[str] = []  # <a href="...rss..."> fallbacks

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        href = (a.get("href") or "").strip()
        if not href:
            return
        if tag == "link" and "alternate" in (a.get("rel") or "") \
                and (a.get("type") or "").split(";")[0].strip() in _FEED_TYPES:
            self.links.append(urljoin(self.base, href))
        elif tag == "a" and _FEED_HREF.search(urlparse(urljoin(self.base, href)).path):
            self.anchors.append(urljoin(self.base, href))


def _looks_like_feed(content: bytes) -> list | None:
    """Parse candidate bytes; return entries when it's a real, non-empty feed."""
    head = content[:400].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        return None
    parsed = feedparser.parse(content)
    if parsed.entries and not (parsed.bozo and not parsed.entries):
        return parsed.entries
    return None


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    try:
        return await client.get(url, headers={"User-Agent": BROWSER_UA, "Accept": FEED_ACCEPT})
    except Exception:
        return None


async def _validate(client: httpx.AsyncClient, url: str) -> list | None:
    resp = await _get(client, url)
    if resp is None or resp.status_code != 200:
        return None
    return _looks_like_feed(resp.content)


def _site_roots(source: Source) -> list[str]:
    roots = []
    for base in (source.homepage, source.rss_url):
        p = urlparse(base or "")
        if p.netloc:
            root = f"{p.scheme or 'https'}://{p.netloc}"
            if root not in roots:
                roots.append(root)
    return roots


async def find_replacement(client: httpx.AsyncClient, source: Source,
                           taken: set[str]) -> tuple[str, list] | None:
    """Return (new_url, entries) for the first working candidate, or None."""
    roots = _site_roots(source)
    candidates: list[str] = []

    # 1) autodiscovery: the homepage as configured (it may live under a
    #    path), then each site root
    pages = ([source.homepage] if source.homepage else []) + \
            [r for r in roots if r.rstrip("/") != (source.homepage or "").rstrip("/")]
    for page in pages:
        resp = await _get(client, page)
        if resp is not None and resp.status_code == 200:
            parser = _FeedLinkParser(str(resp.url))
            try:
                parser.feed(resp.text)
            except Exception:
                pass
            candidates.extend(parser.links + parser.anchors[:3])

    for root in roots:  # 2) conventional feed locations
        candidates.extend(urljoin(root, path) for path in COMMON_PATHS)

    if (source.platform or "news") == "news" and roots:  # 3) last resort: Google News
        domain = urlparse(roots[0]).netloc.removeprefix("www.")
        candidates.append("https://news.google.com/rss/search?q="
                          + quote(f"site:{domain}") + "&hl=en-US&gl=US&ceid=US:en")

    skip = {source.rss_url} | taken
    checked = 0
    for url in candidates:
        url = url.strip()[:500]
        if not url.startswith("http") or url in skip:
            continue
        skip.add(url)
        checked += 1
        if checked > MAX_CANDIDATES:
            break
        entries = await _validate(client, url)
        if entries:
            return url, entries
    return None


async def attempt_repair(client: httpx.AsyncClient, db, source: Source,
                         verify_current: bool = False) -> tuple[bool, list]:
    """Try to fix one source in place. Returns (changed_or_recovered, entries).

    With verify_current (the manual Sources-panel button), the existing URL is
    re-checked first — a source that merely had a bad day is marked healthy
    without being rewired. The caller commits.
    """
    source.last_repair_at = utcnow()
    if verify_current:
        entries = await _validate(client, source.rss_url)
        if entries:
            source.last_status = "ok"
            source.consecutive_failures = 0
            source.last_fetched_at = utcnow()
            return True, entries

    taken = set(db.scalars(select(Source.rss_url).where(Source.id != source.id)))
    found = await find_replacement(client, source, taken)
    if not found:
        log.info("repair: no working feed found for %s (%s)", source.name, source.rss_url)
        return False, []

    new_url, entries = found
    if not source.repaired_from:
        source.repaired_from = source.rss_url  # keep the original, not intermediate hops
    log.info("repair: %s switched %s -> %s", source.name, source.rss_url, new_url)
    source.rss_url = new_url
    source.last_status = "ok (auto-repaired)"
    source.consecutive_failures = 0
    source.last_fetched_at = utcnow()
    return True, entries
