"""Feed ingestion: poll every enabled source, normalize + geotag + score new
articles, then evaluate active alerts and push events to connected clients.

Ingestion uses each outlet's published RSS/Atom feed — the channel publishers
provide for syndication — rather than scraping article HTML.
"""
from __future__ import annotations

import asyncio
import calendar
import logging
import os
import re
from collections import Counter
from datetime import datetime, timedelta
from urllib.parse import urlparse

import feedparser
import httpx
from sqlalchemy import select

from . import discovery, repair
from .clustering import assign_events
from .content import fetch_article_text
from .database import SessionLocal
from .events import broadcaster
from .matching import CriteriaMatcher
from .models import Alert, AlertEvent, Article, Source, utcnow
from .geo import extract_places
from .scoring import classify_categories, cluster_tokens, score_importance, tokens_similarity

log = logging.getLogger("ingest")

FETCH_INTERVAL = int(os.environ.get("NEWS_FETCH_INTERVAL", "300"))
FETCH_TIMEOUT = float(os.environ.get("NEWS_FETCH_TIMEOUT", "20"))
CONCURRENCY = int(os.environ.get("NEWS_FETCH_CONCURRENCY", "10"))

# ---- rolling poller pacing ----
# Sources are polled continuously on their own cadence rather than in one big
# cycle. Each tick the scheduler fetches whichever sources are "due", spread
# out per host by a steady gap so a rate-limited host (news.google.com, which
# backs ~500 city feeds) gets a gentle drip instead of a serialized burst.
POLL_TICK = float(os.environ.get("NEWS_POLL_TICK", "15"))        # seconds between ticks
POLL_BATCH = int(os.environ.get("NEWS_POLL_BATCH", "80"))         # max non-city fetches per tick
CITY_PER_TICK = int(os.environ.get("NEWS_CITY_PER_TICK", "20"))   # max city feeds per tick (bounds the google drip)
BASE_INTERVAL = int(os.environ.get("NEWS_FETCH_INTERVAL", "300"))  # non-city refresh (s)
CITY_INTERVAL = int(os.environ.get("NEWS_CITY_INTERVAL", "5400"))  # city local refresh (s, 90m)
CITY_IDLE_BACKOFF_MAX = int(os.environ.get("NEWS_CITY_IDLE_BACKOFF_MAX", "4"))  # ×2^n cap
GOOGLE_GAP = float(os.environ.get("NEWS_GOOGLE_GAP", "2.0"))       # min gap between google hits
SHARED_HOST_GAP = float(os.environ.get("NEWS_SHARED_HOST_GAP", "1.0"))  # other multi-feed hosts
USER_AGENT = "Delphi/1.0 (+RSS reader; respects robots and publisher feeds)"
# Some CDNs 403 any unknown agent while happily serving browsers the same
# public syndication feed; retry once with a browser UA before giving up.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"

status: dict = {"running": False, "last_run": None, "last_new_articles": 0, "cycles": 0}

# Full-article content fetching (match criteria against the story body, not
# just headline + feed summary). NEWS_CONTENT_FETCH=0 disables.
CONTENT_FETCH = os.environ.get("NEWS_CONTENT_FETCH", "1") == "1"
CONTENT_MAX_PER_CYCLE = int(os.environ.get("NEWS_CONTENT_MAX_PER_CYCLE", "150"))
CONTENT_TIMEOUT = float(os.environ.get("NEWS_CONTENT_TIMEOUT", "10"))
CONTENT_CONCURRENCY = int(os.environ.get("NEWS_CONTENT_CONCURRENCY", "8"))

# source_id -> don't poll again before this time (set on HTTP 429)
_backoff_until: dict[int, datetime] = {}
# Only one ingest cycle at a time; the API returns "busy" instead of racing.
cycle_lock = asyncio.Lock()

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _TAG_RE.sub(" ", text or "").replace("&nbsp;", " ").strip()


def _entry_published(entry) -> datetime:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime.utcfromtimestamp(calendar.timegm(parsed))
            except (ValueError, OverflowError):
                pass
    return utcnow()


def _entry_image(entry) -> str:
    for media in entry.get("media_content", []) or []:
        if media.get("url"):
            return media["url"]
    for media in entry.get("media_thumbnail", []) or []:
        if media.get("url"):
            return media["url"]
    for link in entry.get("links", []) or []:
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image"):
            return link.get("href", "")
    return ""


async def fetch_source(client: httpx.AsyncClient, source: Source) -> tuple[Source, list, str]:
    until = _backoff_until.get(source.id)
    if until and utcnow() < until:
        return source, [], f"rate-limited; retrying after {until:%H:%M} UTC"
    try:
        resp = await client.get(source.rss_url,
                                headers={"User-Agent": USER_AGENT, "Accept": ACCEPT})
        if resp.status_code == 403:
            resp = await client.get(source.rss_url,
                                    headers={"User-Agent": BROWSER_UA, "Accept": ACCEPT})
        if resp.status_code == 429:
            try:
                retry_after = int(resp.headers.get("Retry-After", "0"))
            except ValueError:
                retry_after = 0
            delay = min(max(retry_after, 900), 3600)
            _backoff_until[source.id] = utcnow() + timedelta(seconds=delay)
            return source, [], f"error: 429 rate-limited (backing off {delay // 60} min)"
        resp.raise_for_status()
        _backoff_until.pop(source.id, None)

        head = resp.content[:400].lstrip().lower()
        if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
            return source, [], "error: not a feed — URL returns an HTML page (edit the URL in Sources)"
        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            return source, [], f"parse error: {parsed.get('bozo_exception', 'unknown')}"[:200]
        return source, parsed.entries, "ok"
    except httpx.HTTPStatusError as exc:  # clean "error: 404 Not Found" statuses
        r = exc.response
        return source, [], f"error: {r.status_code} {r.reason_phrase}"[:200]
    except Exception as exc:  # network errors must never kill the cycle
        return source, [], f"error: {type(exc).__name__}: {exc}"[:200]


def _recent_clusters(db, hours: int = 48) -> list[tuple[int, str]]:
    since = utcnow() - timedelta(hours=hours)
    rows = db.execute(
        select(Article.source_id, Article.cluster_tokens).where(
            Article.published_at >= since, Article.cluster_tokens != ""
        )
    ).all()
    return [(r[0], r[1]) for r in rows]


def _corroboration(source_id: int, tokens: str, recent: list[tuple[int, str]]) -> int:
    if not tokens:
        return 0
    others = set()
    for other_source, other_tokens in recent:
        if other_source == source_id:
            continue
        if tokens_similarity(tokens, other_tokens) >= 0.5:
            others.add(other_source)
    return len(others)


def process_entries(db, source: Source, entries: list, recent_clusters: list,
                    seen_urls: set[str]) -> list[Article]:
    """Normalize feed entries into Article rows; returns newly inserted articles.

    Dedup must be GLOBAL, not per-source: articles.url is unique across the
    table and different feeds routinely syndicate the same URL (Google News
    country/topic feeds especially). `seen_urls` spans the whole cycle.
    """
    new_articles: list[Article] = []
    candidates = []
    for entry in entries[:80]:
        url = (entry.get("link") or "").strip()[:1000]
        title = _strip_html(entry.get("title", "")).strip()
        if url and title and url not in seen_urls:
            candidates.append((url, title, entry))
    if not candidates:
        return []
    existing = set(db.scalars(
        select(Article.url).where(Article.url.in_([c[0] for c in candidates]))
    ))

    for url, title, entry in candidates:
        if url in existing or url in seen_urls:
            continue
        seen_urls.add(url)

        summary = _strip_html(entry.get("summary", ""))[:2000]
        text = f"{title}\n{summary}"
        places = extract_places(text)
        country = places[0]["country"] if places else source.country
        tokens = cluster_tokens(title)
        corroborating = _corroboration(source.id, tokens, recent_clusters)

        article = Article(
            source_id=source.id,
            guid=(entry.get("id") or "")[:500],
            url=url,
            title=title,
            summary=summary,
            image_url=_entry_image(entry)[:1000],
            published_at=_entry_published(entry),
            language=source.language,
            country=country or "",
            categories=classify_categories(text, source.categories),
            places=places,
            importance=score_importance(text, source.scope, source.tier, places, corroborating),
            cluster_tokens=tokens,
        )
        db.add(article)
        new_articles.append(article)
        recent_clusters.append((source.id, tokens))
    return new_articles


async def enrich_with_content(db, articles: list[Article], cap: int | None = None) -> int:
    """Fetch article bodies (newest first, capped) and re-run the text-derived
    enrichment — geotagging and categorization — over headline + summary +
    body. Failures leave an article headline-matched, as before."""
    todo = [a for a in articles if not a.content and a.url.startswith("http")]
    todo.sort(key=lambda a: a.published_at or utcnow(), reverse=True)
    todo = todo[:cap if cap is not None else CONTENT_MAX_PER_CYCLE]
    if not todo:
        return 0
    sem = asyncio.Semaphore(CONTENT_CONCURRENCY)
    async with httpx.AsyncClient(timeout=CONTENT_TIMEOUT, follow_redirects=True) as client:
        async def one(article):
            async with sem:
                return article, await fetch_article_text(client, article.url)
        results = await asyncio.gather(*(one(a) for a in todo))
    fetched = 0
    for article, text in results:
        if not text:
            continue
        article.content = text
        full = f"{article.title}\n{article.summary}\n{text}"
        article.places = extract_places(full)
        if article.places:
            article.country = article.country or article.places[0]["country"]
        source_cats = article.source.categories if article.source else []
        article.categories = classify_categories(full, source_cats)  # title still weighted 2x
        fetched += 1
    db.commit()
    return fetched


def evaluate_alerts(db, new_articles: list[Article]) -> list[dict]:
    """Match new articles against every active alert; persist and return hits."""
    alerts = db.scalars(select(Alert).where(Alert.active.is_(True))).all()
    hits: list[dict] = []
    for alert in alerts:
        matcher = CriteriaMatcher(alert.criteria)
        for article in new_articles:
            if matcher.matches(article, article.source):
                db.add(AlertEvent(alert_id=alert.id, article_id=article.id))
                alert.last_triggered_at = utcnow()
                hits.append({
                    "alert_id": alert.id,
                    "alert_name": alert.name,
                    "user_id": alert.user_id,
                    "pantheon_id": alert.pantheon_id,
                    "article_id": article.id,
                    "title": article.title,
                    "url": article.url,
                    "importance": article.importance,
                    "country": article.country,
                })
    return hits


# ---------- rolling poll scheduling ----------

def _host(source: Source) -> str:
    return urlparse(source.rss_url).netloc


def _is_city(source: Source) -> bool:
    return source.added_by == "city-catalog"


def host_gap(host: str, count: int) -> float:
    """Minimum seconds between requests to a host. Google News backs hundreds
    of city feeds and rate-limits hard, so it gets the widest gap; other hosts
    serving several of our feeds get a smaller one; unique hosts, none."""
    if host.endswith("news.google.com"):
        return GOOGLE_GAP
    return SHARED_HOST_GAP if count > 1 else 0.0


def effective_interval(source: Source) -> float:
    """How long between polls of this source. City local feeds refresh slowly
    (local news changes slowly) and back off further when they keep coming up
    empty — freeing Google capacity for the feeds that actually produce news."""
    if _is_city(source):
        return CITY_INTERVAL * (2 ** min(source.idle_polls or 0, CITY_IDLE_BACKOFF_MAX))
    return BASE_INTERVAL


def _due(source: Source, now: datetime) -> bool:
    if source.last_fetched_at is None:
        return True
    return (now - source.last_fetched_at).total_seconds() >= effective_interval(source)


def due_sources(enabled: list[Source], now: datetime) -> list[Source]:
    """Sources past their refresh interval, ordered so nothing starves:
    never-fetched first, news wires ahead of city feeds, then oldest first."""
    due = [s for s in enabled if _due(s, now)]
    due.sort(key=lambda s: (s.last_fetched_at is not None, _is_city(s),
                            s.last_fetched_at or datetime.min))
    return due


class HostPacer:
    """Hands out steady per-host time slots. reserve() atomically books the
    next slot for a host and returns how long the caller should wait before
    firing, so concurrent requests to a rate-limited host queue up at `gap`
    spacing instead of bursting. Waiting happens outside the lock (and before
    acquiring a concurrency slot), so paced feeds don't tie up resources."""

    def __init__(self):
        self._next: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def reserve(self, host: str, gap: float) -> float:
        if gap <= 0:
            return 0.0
        loop = asyncio.get_event_loop()
        async with self._lock:
            now = loop.time()
            start = max(now, self._next.get(host, 0.0))
            self._next[host] = start + gap
            return start - now


async def _fetch_batch(sources: list[Source]) -> list[tuple]:
    """Fetch a set of sources concurrently, spread per host by the pacer."""
    pacer = HostPacer()
    counts = Counter(_host(s) for s in sources)
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
        async def one(source):
            host = _host(source)
            delay = await pacer.reserve(host, host_gap(host, counts[host]))
            if delay > 0:
                await asyncio.sleep(delay)
            async with sem:
                return await fetch_source(client, source)
        return await asyncio.gather(*(one(s) for s in sources))


async def _ingest_batch(db, sources: list[Source]) -> dict:
    """Poll `sources`, insert new articles, then run repair, discovery,
    content enrichment, clustering, and alert evaluation over the result.
    The caller owns `db`. Returns stats for this batch."""
    if not sources:
        return {"new_articles": 0, "new_events": 0, "alert_hits": 0,
                "content_fetched": 0, "repaired": 0, "discovered": 0,
                "sources_ok": 0, "sources_total": 0}
    recent = _recent_clusters(db)
    results = await _fetch_batch(sources)

    all_new: list[Article] = []
    ok_sources = 0
    seen_urls: set[str] = set()
    publishers: dict[str, tuple[str, str]] = {}  # outlets named by entries' <source> tags
    for source, entries, fetch_status in results:
        source.last_fetched_at = utcnow()
        source.last_status = fetch_status
        if fetch_status != "ok":
            source.consecutive_failures = (source.consecutive_failures or 0) + 1
            continue
        source.consecutive_failures = 0
        for dom, pub in discovery.collect_publishers(entries).items():
            publishers.setdefault(dom, pub)
        # Commit per source so one bad feed can't roll back the whole batch.
        try:
            new = process_entries(db, source, entries, recent, seen_urls)
            source.last_article_count = len(new)
            # Quiet city feeds back off; a productive poll resets the streak.
            if _is_city(source):
                source.idle_polls = 0 if new else (source.idle_polls or 0) + 1
            ok_sources += 1
            db.commit()
            all_new.extend(new)
        except Exception as exc:
            db.rollback()
            source.last_status = f"error: processing: {type(exc).__name__}: {exc}"[:200]
            log.exception("processing failed for source %s", source.name)
    db.commit()  # persist statuses; article ids are assigned for clustering/alerts

    # Self-repair: sources stuck on permanent-looking errors (403/404/
    # not-a-feed/parse) get their feed URL rediscovered — and, when a
    # replacement validates, are ingested right away.
    repaired = 0
    due = [s for s in sources if repair.due_for_repair(s)][:repair.REPAIR_MAX_PER_CYCLE]
    if due:
        async with httpx.AsyncClient(timeout=repair.REPAIR_TIMEOUT, follow_redirects=True) as client:
            for source in due:
                try:
                    fixed, entries = await repair.attempt_repair(client, db, source)
                    if fixed:
                        new = process_entries(db, source, entries, recent, seen_urls)
                        source.last_article_count = len(new)
                        ok_sources += 1
                        repaired += 1
                        all_new.extend(new)
                    db.commit()  # per source: also persists last_repair_at on misses
                except Exception:
                    db.rollback()
                    log.exception("repair failed for source %s", source.name)

    # Source discovery: outlets named by Google News coverage but missing from
    # the catalog get their own feed found, added, and ingested now.
    discovered = 0
    try:
        for source, entries in await discovery.discover_new_sources(db, publishers):
            new = process_entries(db, source, entries, recent, seen_urls)
            source.last_article_count = len(new)
            db.commit()
            all_new.extend(new)
            discovered += 1
    except Exception:
        db.rollback()
        log.exception("source discovery failed")

    # Pull article bodies BEFORE alert evaluation so alerts see full text.
    content_fetched = 0
    if CONTENT_FETCH:
        content_fetched = await enrich_with_content(db, all_new)

    new_events = assign_events(db, all_new)
    hits = evaluate_alerts(db, all_new)
    db.commit()

    status.update({
        "last_run": utcnow().isoformat(),
        "last_error": None,
        "last_new_events": new_events,
        "last_content_fetched": content_fetched,
        "last_new_articles": len(all_new),
        "last_repaired": repaired,
        "last_discovered": discovered,
        "sources_ok": ok_sources,
        "sources_total": len(sources),
        "cycles": status.get("cycles", 0) + 1,
    })
    broadcaster.publish({"type": "cycle", "sources_ok": ok_sources,
                         "sources_total": len(sources), "new_articles": len(all_new)})
    if all_new:
        broadcaster.publish({"type": "articles", "count": len(all_new)})
    for hit in hits:
        broadcaster.publish({"type": "alert", **hit})
    log.info("ingest batch: %d/%d sources ok, %d new articles, %d alert hits, "
             "%d repaired, %d discovered",
             ok_sources, len(sources), len(all_new), len(hits), repaired, discovered)
    return {"new_articles": len(all_new), "new_events": new_events,
            "alert_hits": len(hits), "content_fetched": content_fetched,
            "repaired": repaired, "discovered": discovered,
            "sources_ok": ok_sources, "sources_total": len(sources)}


async def run_ingest_cycle() -> dict:
    """Manual "refresh now": poll all news wires + trackers immediately (fast,
    diverse hosts) plus any city local feeds that are currently due (bounded
    and paced). The ~500 city feeds are otherwise owned by the rolling loop —
    a button press must never trigger a serialized Google stampede.

    Serialized by cycle_lock so it never overlaps a rolling tick.
    """
    async with cycle_lock:
        db = SessionLocal()
        try:
            enabled = db.scalars(select(Source).where(Source.enabled.is_(True))).all()
            now = utcnow()
            non_city = [s for s in enabled if not _is_city(s)]
            city_due = [s for s in enabled if _is_city(s) and _due(s, now)]
            city_due.sort(key=lambda s: (s.last_fetched_at is not None,
                                         s.last_fetched_at or datetime.min))
            return await _ingest_batch(db, non_city + city_due[:CITY_PER_TICK])
        finally:
            db.close()


async def ingest_loop():
    """Continuous rolling poll: every tick, fetch whichever sources are due
    (news wires first, then a bounded slice of city feeds), paced per host so
    Google gets a steady drip rather than a burst. Each source refreshes on its
    own interval; nothing starves and no single tick runs long."""
    status["running"] = True
    while True:
        try:
            async with cycle_lock:
                db = SessionLocal()
                try:
                    enabled = db.scalars(select(Source).where(Source.enabled.is_(True))).all()
                    due = due_sources(enabled, utcnow())
                    batch = ([s for s in due if not _is_city(s)][:POLL_BATCH]
                             + [s for s in due if _is_city(s)][:CITY_PER_TICK])
                    if batch:
                        await _ingest_batch(db, batch)
                finally:
                    db.close()
        except Exception as exc:
            status["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
            log.exception("ingest tick failed")
        await asyncio.sleep(POLL_TICK)
