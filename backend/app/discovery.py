"""Automatic source discovery from Google News coverage.

Google News tracker feeds (the worldwide-coverage sources created for boolean
queries and topic trackers) tag every entry with the outlet that published it
(``<source url="https://outlet.com">Outlet</source>``); any other feed may
carry the same tag. When a cycle sees an outlet whose domain isn't in the
catalog yet, Delphi hunts for that outlet's own feed — homepage
autodiscovery, then conventional paths — validates it, and adds it as a real
source, so the catalog grows toward wherever the news actually comes from.

Outcomes are remembered per domain (discovered_domains): dead ends are only
re-probed after RECHECK_DAYS, and domains that became sources are never
probed again. NEWS_AUTO_DISCOVER=0 turns the whole mechanism off.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from . import safefetch
from .models import DiscoveredDomain, Source, utcnow
from .repair import COMMON_PATHS, REPAIR_TIMEOUT, _FeedLinkParser, _get, _validate

log = logging.getLogger("discovery")

AUTO_DISCOVER = os.environ.get("NEWS_AUTO_DISCOVER", "1") == "1"
# How many unknown publishers to chase per tick, and how many at once.
#
# Every domain converted from "seen in Google's coverage" into a feed of its
# own is both better coverage — an outlet's whole output rather than Google's
# ranked slice of it — and one less thing competing for the Google budget. The
# work is self-limiting: a domain is recorded once and not re-probed for
# RECHECK_DAYS, so the unknown set shrinks as the catalog grows.
DISCOVER_MAX_PER_CYCLE = int(os.environ.get("NEWS_DISCOVER_MAX_PER_CYCLE", "8"))
DISCOVER_CONCURRENCY = int(os.environ.get("NEWS_DISCOVER_CONCURRENCY", "4"))
RECHECK_DAYS = float(os.environ.get("NEWS_DISCOVER_RECHECK_DAYS", "30"))
MAX_CANDIDATES = 8

# Aggregators and platforms that must never be added as "outlets".
_SKIP_DOMAINS = {
    "news.google.com", "google.com", "youtube.com", "youtu.be",
    "reddit.com", "bsky.app", "twitter.com", "x.com", "facebook.com",
    "instagram.com", "t.me", "medium.com", "msn.com", "news.yahoo.com",
}


def domain_of(url: str) -> str:
    host = urlparse(url or "").netloc.lower()
    return host.removeprefix("www.")


def collect_publishers(entries: list) -> dict[str, tuple[str, str]]:
    """domain -> (outlet name, outlet homepage) for every entry's publisher."""
    out: dict[str, tuple[str, str]] = {}
    for entry in entries:
        src = entry.get("source") or {}
        href = (src.get("href") or src.get("url") or "").strip()
        title = (src.get("title") or "").strip()
        dom = domain_of(href)
        if href.startswith("http") and title and dom and dom not in _SKIP_DOMAINS \
                and dom not in out:
            out[dom] = (title[:200], href[:500])
    return out


def _known_domains(db) -> set[str]:
    """Domains that must not be probed: existing sources, plus every recorded
    discovery outcome (failed probes age out after RECHECK_DAYS)."""
    known: set[str] = set()
    for rss, home in db.execute(select(Source.rss_url, Source.homepage)):
        for url in (rss, home):
            if d := domain_of(url):
                known.add(d)
    cutoff = utcnow() - timedelta(days=RECHECK_DAYS)
    for rec in db.scalars(select(DiscoveredDomain)):
        if rec.status == "added" or rec.checked_at >= cutoff:
            known.add(rec.domain)
    return known


def _record(db, domain: str, status: str):
    rec = db.scalar(select(DiscoveredDomain).where(DiscoveredDomain.domain == domain))
    if rec:
        rec.status, rec.checked_at = status, utcnow()
    else:
        db.add(DiscoveredDomain(domain=domain, status=status))


async def _find_site_feed(client: httpx.AsyncClient, homepage: str) -> tuple[str, list] | None:
    """Autodiscovery on the outlet's homepage, then conventional feed paths."""
    candidates: list[str] = []
    resp = await _get(client, homepage)
    if resp is not None and resp.status_code == 200:
        parser = _FeedLinkParser(str(resp.url))
        try:
            parser.feed(resp.text)
        except Exception:
            pass
        candidates.extend(parser.links + parser.anchors[:3])
    p = urlparse(homepage)
    root = f"{p.scheme or 'https'}://{p.netloc}"
    candidates.extend(root + path for path in COMMON_PATHS)

    seen: set[str] = set()
    checked = 0
    for url in candidates:
        url = url.strip()[:500]
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        checked += 1
        if checked > MAX_CANDIDATES:
            break
        entries = await _validate(client, url)
        if entries:
            return url, entries
    return None


async def discover_new_sources(db, publishers: dict[str, tuple[str, str]]
                               ) -> list[tuple[Source, list]]:
    """Probe unknown publisher domains and add sources for the ones with a
    working feed. Returns (source, entries) pairs — sources are flushed (ids
    assigned) but not committed; the caller ingests the entries and commits."""
    if not AUTO_DISCOVER or not publishers:
        return []
    known = _known_domains(db)
    todo = [(dom, name, home) for dom, (name, home) in publishers.items()
            if dom not in known][:DISCOVER_MAX_PER_CYCLE]
    if not todo:
        return []
    taken = set(db.scalars(select(Source.rss_url)))
    added: list[tuple[Source, list]] = []
    # Probe together, record one at a time.
    #
    # Each probe is a homepage fetch plus a walk of the common feed paths, and
    # doing them one after another put the whole tick behind them — eight
    # unresponsive domains at the 8s timeout is over a minute during which no
    # source is polled. The network work now overlaps; the database work stays
    # serial, because the session is not safe to share.
    sem = asyncio.Semaphore(DISCOVER_CONCURRENCY)
    async with safefetch.client(timeout=REPAIR_TIMEOUT) as client:
        async def probe(entry):
            async with sem:
                return entry, await _find_site_feed(client, entry[2])

        probed = await asyncio.gather(*(probe(e) for e in todo))
        for (dom, name, homepage), found in probed:
            if found and found[0] not in taken:
                feed_url, entries = found
                source = Source(
                    name=name, rss_url=feed_url, homepage=homepage,
                    scope="national", tier=3, platform="news",
                    added_by="auto-discovered",
                    last_status="ok (auto-discovered)", last_fetched_at=utcnow(),
                )
                db.add(source)
                db.flush()  # article rows need source.id
                _record(db, dom, "added")
                taken.add(feed_url)
                added.append((source, entries))
                log.info("discovery: added %s (%s) -> %s", name, dom, feed_url)
            else:
                _record(db, dom, "no-feed")
                log.info("discovery: no usable feed for %s (%s)", name, dom)
    return added
