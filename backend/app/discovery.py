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
from sqlalchemy import func, select

from . import feedkind, safefetch
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

# Domains that must never be adopted as "outlets", in two kinds — and the
# distinction matters, because only one of them can be applied backwards.
#
# Aggregators and social platforms are not outlets, but Delphi builds feeds on
# some of them on purpose: the 497 city sources and every topic tracker are
# news.google.com searches, and automatic repair can rewire a dead outlet onto
# one. Refusing to *adopt* them is right; sweeping the catalog for them would
# disable the city coverage.
_SKIP_AGGREGATORS = {
    "news.google.com", "google.com", "youtube.com", "youtu.be",
    "reddit.com", "bsky.app", "twitter.com", "x.com", "facebook.com",
    "instagram.com", "t.me", "medium.com", "msn.com", "news.yahoo.com",
}

# Not news under any circumstances, however the source got here. Ticketing and
# event platforms publish feeds with exactly the shape of a news feed — dated
# entries, headlines, bodies — which is how baodaorecords.kktix.cc, a record
# label's ticket calendar, became a "national news" source and put concert
# listings into an energy feed. Storefronts and job boards are the same shape
# and the same problem. This set is safe to apply to sources already adopted.
_SKIP_NOT_NEWS = {
    "kktix.cc", "eventbrite.com", "eventbrite.co.uk", "ticketmaster.com",
    "peatix.com", "accupass.com", "kkday.com", "klook.com", "meetup.com",
    "bandsintown.com", "songkick.com", "dice.fm", "ticketek.com",
    "shopify.com", "etsy.com", "ebay.com", "craigslist.org",
    "indeed.com", "linkedin.com",
    # Deliberately NOT amazon.com. The first sweep of the live catalog
    # disabled "Amazon Web Services (AWS)" — aws.amazon.com/rss, a product
    # and outage announcement feed a tech watcher would want — because the
    # entry aimed at the storefront matched every subdomain. Suffix matching
    # is what makes this list work and also what makes a broad entry
    # dangerous; the storefront publishes nothing anyone would discover as
    # news, so the entry was all cost and no benefit.
}

_SKIP_DOMAINS = _SKIP_AGGREGATORS | _SKIP_NOT_NEWS


def _under(domain: str, blocked: set[str]) -> bool:
    """Matched by suffix, not equality. As exact strings these missed every
    subdomain, which is how baodaorecords.kktix.cc got in past kktix.cc."""
    return any(domain == bad or domain.endswith("." + bad) for bad in blocked)


def not_news(domain: str) -> bool:
    """A domain that is not a news source however it arrived."""
    return _under(domain, _SKIP_NOT_NEWS)


def skipped(domain: str) -> bool:
    """Is this domain — or anything under it — on the never-adopt list?"""
    return _under(domain, _SKIP_DOMAINS)


# Sightings required before a domain is probed at all.
#
# A blocklist can only name the kinds of site somebody thought of; the catalog
# had picked up a funeral home. What separates an outlet from a one-off is
# recurrence — a real publisher keeps appearing in coverage, while a ticketing
# calendar or a local business surfaces once. Waiting for a second sighting
# costs a cycle and needs no list of every kind of website that is not a
# newspaper.
MIN_SIGHTINGS = int(os.environ.get("NEWS_DISCOVER_MIN_SIGHTINGS", "2"))

# How large the catalog may grow before auto-discovery stops adding to it.
#
# It had no ceiling, and in one day it went from 4,655 enabled sources to 9,421.
# Nothing was wrong with any individual adoption; there was simply no answer to
# "how many is too many", so the answer was "more".
#
# The number comes from how long a full sweep takes. The poller reaches
# POLL_BATCH + CITY_PER_TICK sources per tick -- 52 every fifteen seconds, about
# 12,500 an hour -- so 12,000 sources are all polled inside an hour, which is
# fresh enough for news. Twice that and a feed waits over an hour and a half to
# be read, which is a worse product than a smaller catalog polled promptly.
#
# Only auto-discovery is bounded by this. A source added by hand is a decision
# somebody made, and no ceiling should silently overrule it.
MAX_SOURCES = int(os.environ.get("NEWS_MAX_SOURCES", "12000"))


def at_capacity(db) -> int:
    """Enabled sources beyond the ceiling, or 0 while there is room."""
    if MAX_SOURCES <= 0:
        return 0
    enabled = db.scalar(
        select(func.count(Source.id)).where(Source.enabled.is_(True))) or 0
    return max(0, enabled - MAX_SOURCES)


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
        if href.startswith("http") and title and dom and not skipped(dom) \
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
        # "seen" is a domain still on its way to a second sighting, not a
        # settled outcome — it must stay eligible or it could never reach one.
        if rec.status == "added" or (rec.status != "seen" and rec.checked_at >= cutoff):
            known.add(rec.domain)
    return known


def _sightings(db, domains: set[str]) -> dict[str, int]:
    if not domains:
        return {}
    return {rec.domain: rec.sightings or 0 for rec in db.scalars(
        select(DiscoveredDomain).where(DiscoveredDomain.domain.in_(domains)))}


def note_sighting(db, domain: str) -> int:
    """Record that `domain` published something, and return the running count."""
    rec = db.scalar(select(DiscoveredDomain).where(DiscoveredDomain.domain == domain))
    if rec is None:
        rec = DiscoveredDomain(domain=domain, status="seen", sightings=1)
        db.add(rec)
        # Flushed, not just added: the next lookup of this domain — the same
        # outlet appearing twice in one batch, or the next cycle before a
        # commit — has to find the row. Without it every sighting is a first
        # sighting and nothing is ever adopted.
        db.flush()
        return 1
    rec.sightings = (rec.sightings or 0) + 1
    rec.checked_at = utcnow()
    if rec.status not in ("added", "no-feed"):
        rec.status = "seen"
    return rec.sightings


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
    over = at_capacity(db)
    if over:
        # Sightings are still not recorded here: a domain seen while the catalog
        # is full should be considered fresh if room appears later, not silently
        # aged out of eligibility.
        log.info("discovery: catalog is full (%d over %d enabled sources) — "
                 "not adopting anything new", over, MAX_SOURCES)
        return []
    known = _known_domains(db)
    candidates = {dom: v for dom, v in publishers.items()
                  if dom not in known and not skipped(dom)}

    # Count this cycle's sighting first, then take only the domains that have
    # now been seen often enough. A domain on its first sighting is recorded
    # and left alone; if it is a real outlet it will be back.
    seen_counts = _sightings(db, set(candidates))
    todo = []
    for dom, (name, home) in candidates.items():
        count = note_sighting(db, dom)
        seen_counts[dom] = count
        if count >= MIN_SIGHTINGS:
            todo.append((dom, name, home))
    todo = todo[:DISCOVER_MAX_PER_CYCLE]
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
            # The entries are already here — the probe fetched and parsed them
            # to decide the feed was real — and until now the only thing ever
            # asked of them was whether there were any. A ticketing calendar
            # passes that test as well as a newspaper does.
            verdict = feedkind.classify(found[1]) if found else feedkind.Verdict()
            if found and not verdict.is_news:
                _record(db, dom, "not-news")
                log.info("discovery: declined %s (%s) — %s",
                         name, dom, verdict.summary())
            elif found and found[0] not in taken:
                feed_url, entries = found
                source = Source(
                    name=name, rss_url=feed_url, homepage=homepage,
                    scope="national", tier=3, platform="news",
                    added_by="auto-discovered",
                    last_status="ok (auto-discovered)", last_fetched_at=utcnow(),
                    feed_kind="news", feed_kind_at=utcnow(),
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


def audit_catalog(db) -> dict:
    """Apply today's adoption rules to sources adopted under yesterday's.

    Tightening what may be adopted does nothing about what already was, and
    the catalog is 85% auto-adopted — the ticketing calendar that started this
    was still sitting in it, still enabled, still filed as national news.

    Disabled, not deleted, and never silently: the source stays in the Sources
    panel saying why it was switched off, so an operator who disagrees can
    switch it back on. Deleting would also take its articles, and those are
    shared by every feed on the instance.

    Two deliberate exemptions. A source a person added by hand is left alone —
    they chose it, and this is a heuristic. And only `_SKIP_NOT_NEWS` is swept:
    the aggregator half of the list includes news.google.com, which the 497
    city sources and every topic tracker are built on, so sweeping for it
    would disable the city coverage entirely.
    """
    hits: list[Source] = []
    for src in db.scalars(select(Source).where(Source.enabled.is_(True))):
        if src.added_by == "user":
            continue
        if any(not_news(d) for d in (domain_of(src.rss_url), domain_of(src.homepage)) if d):
            hits.append(src)

    for src in hits:
        src.enabled = False
        src.last_status = "disabled: not a news source (event/commerce platform)"
    if hits:
        db.commit()
        log.info("catalog audit: disabled %d non-news source(s): %s",
                 len(hits), ", ".join(s.name for s in hits[:10]))
    return {"checked": True, "disabled": len(hits),
            "names": [s.name for s in hits[:20]]}
