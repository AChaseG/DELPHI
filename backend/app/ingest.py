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
import time
from collections import Counter
from datetime import datetime, timedelta
from urllib.parse import urlparse

import feedparser
import httpx
from sqlalchemy import delete as sa_delete, exists, or_, select

from . import discovery, home, langdetect, mailer, repair
from .clustering import assign_events
from .content import fetch_article_text
from .database import SessionLocal
from .events import broadcaster
from .matching import CriteriaMatcher, article_text
from .models import (Alert, AlertEvent, Article, Event, Source, Translation,
                     User, ViewedEvent, utcnow)
from .geo import extract_places
from .scoring import classify_categories, cluster_tokens, score_importance

log = logging.getLogger("ingest")

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
# Refresh intervals. Both came down when polling became conditional: a feed
# that hasn't changed now answers 304 with no body and nothing to parse, so the
# cost of asking more often is a request rather than a download. Wires moved
# 5min → 3min and city feeds 90min → 60min, which at 497 city feeds is 8.3
# Google requests a minute against the 30/min the host pacer allows.
BASE_INTERVAL = int(os.environ.get("NEWS_FETCH_INTERVAL", "180"))   # non-city refresh (s)
CITY_INTERVAL = int(os.environ.get("NEWS_CITY_INTERVAL", "3600"))   # city local refresh (s, 60m)
CITY_IDLE_BACKOFF_MAX = int(os.environ.get("NEWS_CITY_IDLE_BACKOFF_MAX", "4"))  # ×2^n cap
GOOGLE_GAP = float(os.environ.get("NEWS_GOOGLE_GAP", "2.0"))       # min gap between google hits
SHARED_HOST_GAP = float(os.environ.get("NEWS_SHARED_HOST_GAP", "1.0"))  # other multi-feed hosts

# Article retention: with ~500+ sources the table grows without bound and
# every board query slows over time. Articles older than this are pruned
# (with their translations and alert-history rows); 0 disables pruning.
RETENTION_DAYS = float(os.environ.get("NEWS_RETENTION_DAYS", "30"))
PRUNE_EVERY_SECONDS = 6 * 3600
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


def _is_http_url(url: str) -> bool:
    """Only http(s) links are safe to store and later render as an href."""
    return url.lower().startswith(("http://", "https://"))


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


# The status a source gets when the publisher says nothing has changed. Not an
# error and not a poll that found news — the caller has to tell all three apart.
UNCHANGED = "ok: unchanged"


def _conditional_headers(source: Source) -> dict:
    """Ask the publisher to answer only if the feed has actually changed.

    Most polls change nothing, and without these every one of them downloads
    and re-parses the whole feed. With them the publisher answers 304 in a few
    hundred bytes and there is nothing to parse — which is what makes it
    reasonable to poll more often rather than less."""
    headers = {"User-Agent": USER_AGENT, "Accept": ACCEPT}
    if source.etag:
        headers["If-None-Match"] = source.etag
    if source.last_modified:
        headers["If-Modified-Since"] = source.last_modified
    return headers


def _remember_validators(source: Source, resp) -> None:
    """Keep whatever the publisher gave us to ask with next time.

    Only overwritten when the server sends one: a server that offers an ETag
    on one response and omits it on the next has not withdrawn it, and
    forgetting would silently turn conditional polling back off."""
    etag = resp.headers.get("ETag")
    modified = resp.headers.get("Last-Modified")
    if etag:
        source.etag = etag[:200]
    if modified:
        source.last_modified = modified[:80]


async def fetch_source(client: httpx.AsyncClient, source: Source) -> tuple[Source, list, str]:
    until = _backoff_until.get(source.id)
    if until and utcnow() < until:
        return source, [], f"rate-limited; retrying after {until:%H:%M} UTC"
    try:
        resp = await client.get(source.rss_url, headers=_conditional_headers(source))
        if resp.status_code == 403:
            retry = dict(_conditional_headers(source), **{"User-Agent": BROWSER_UA})
            resp = await client.get(source.rss_url, headers=retry)
        if resp.status_code == 304:
            # Nothing new since last time. The cheapest possible poll: no body
            # to download, no feed to parse, and the source is healthy.
            _backoff_until.pop(source.id, None)
            _remember_validators(source, resp)
            return source, [], UNCHANGED
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
        _remember_validators(source, resp)
        return source, parsed.entries, "ok"
    except httpx.HTTPStatusError as exc:  # clean "error: 404 Not Found" statuses
        r = exc.response
        return source, [], f"error: {r.status_code} {r.reason_phrase}"[:200]
    except Exception as exc:  # network errors must never kill the cycle
        return source, [], f"error: {type(exc).__name__}: {exc}"[:200]


class RecentClusters:
    """Headline token-sets from the past two days, indexed by token.

    Corroboration asks how many other outlets are carrying the same story,
    which meant comparing each incoming headline against every recent one:
    thousands of set comparisons per article, and a busy tick brings hundreds
    of articles. Two headlines with no word in common cannot be similar, so an
    inverted index answers the same question by looking only at the handful
    that share a word — measured at 2.5ms per article before, 0.05ms after,
    on a window of 2,016 headlines.

    The batch adds to it as it goes, so articles arriving together still
    corroborate each other.
    """

    def __init__(self, rows: list[tuple[int, str]] = ()):
        self._entries: list[tuple[int, frozenset[str]]] = []
        self._by_token: dict[str, list[int]] = {}
        for source_id, tokens in rows:
            self.add(source_id, tokens)

    def add(self, source_id: int, tokens: str) -> None:
        words = frozenset(tokens.split())
        if not words:
            return
        at = len(self._entries)
        self._entries.append((source_id, words))
        for word in words:
            self._by_token.setdefault(word, []).append(at)

    def corroboration(self, source_id: int, tokens: str) -> int:
        """How many other outlets ran a headline this one matches."""
        words = set(tokens.split())
        if not words:
            return 0
        others: set[int] = set()
        checked: set[int] = set()
        for word in words:
            for at in self._by_token.get(word, ()):
                if at in checked:
                    continue
                checked.add(at)
                other_source, other_words = self._entries[at]
                # One outlet corroborates once, so a source already counted
                # needs no further comparison.
                if other_source == source_id or other_source in others:
                    continue
                shared = len(words & other_words)
                if (max(shared / len(words | other_words),
                        shared / min(len(words), len(other_words))) >= 0.5):
                    others.add(other_source)
        return len(others)


def _recent_clusters(db, hours: int = 48) -> RecentClusters:
    since = utcnow() - timedelta(hours=hours)
    rows = db.execute(
        select(Article.source_id, Article.cluster_tokens).where(
            Article.published_at >= since, Article.cluster_tokens != ""
        )
    ).all()
    return RecentClusters([(r[0], r[1]) for r in rows])


def process_entries(db, source: Source, entries: list, recent_clusters: RecentClusters,
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
        # Only http(s) links: a hostile/compromised feed could otherwise ship a
        # javascript: (or data:) link that becomes a clickable XSS payload in
        # every reader's board. Such entries are dropped entirely.
        if url and _is_http_url(url) and title and url not in seen_urls:
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
        entry_tags = [t.get("term", "") for t in (entry.get("tags") or []) if t.get("term")]
        places = extract_places(text)
        country = places[0]["country"] if places else source.country
        tokens = cluster_tokens(title, text, places)
        corroborating = recent_clusters.corroboration(source.id, tokens)

        article = Article(
            source_id=source.id,
            guid=(entry.get("id") or "")[:500],
            url=url,
            title=title,
            summary=summary,
            image_url=_entry_image(entry)[:1000],
            published_at=_entry_published(entry),
            # Detect from the text, not the source tag: aggregators carry many
            # languages and discovered outlets default to "en", which would
            # otherwise leave foreign articles tagged English and untranslated.
            language=langdetect.detect(text, source.language),
            country=country or "",
            categories=classify_categories(text, source.categories, entry_tags),
            places=places,
            importance=score_importance(text, source.scope, source.tier, places, corroborating),
            cluster_tokens=tokens,
        )
        db.add(article)
        new_articles.append(article)
        recent_clusters.add(source.id, tokens)
    return new_articles


async def enrich_with_content(db, articles: list[Article], cap: int | None = None) -> int:
    """Fetch article bodies (newest first, capped) and re-run the text-derived
    enrichment — geotagging and categorization — over headline + summary +
    body. Failures leave an article headline-matched, as before."""
    # Skip paywalled outlets: fetching their article page returns a paywall/
    # truncated stub, which would pollute content and matching. They match on
    # RSS headline + summary only, and readers get an archive.ph link.
    todo = [a for a in articles if not a.content and a.url.startswith("http")
            and not (a.source and a.source.paywall)]
    todo.sort(key=lambda a: a.published_at or utcnow(), reverse=True)
    todo = todo[:cap if cap is not None else CONTENT_MAX_PER_CYCLE]
    if not todo:
        return 0
    # Stamped before the fetch, not after, and whether or not it succeeds: this
    # is "we have tried", which is what stops the backlog below returning the
    # same unreachable page on every tick from now on.
    attempted = utcnow()
    for article in todo:
        article.content_tried_at = attempted
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
        # Full body gives a stronger language signal than the headline did.
        article.language = langdetect.detect(full, article.language)
        fetched += 1
    db.commit()
    return fetched


# How far back the backlog reaches, and how long before an article whose page
# could not be fetched is worth another go. A page that 404s or times out
# usually always will; a page that was briefly down deserves one more attempt
# before it ages out of the window entirely.
BACKFILL_HOURS = float(os.environ.get("NEWS_BACKFILL_HOURS", "48"))
BACKFILL_RETRY_HOURS = float(os.environ.get("NEWS_BACKFILL_RETRY_HOURS", "6"))


async def backfill_content(db, spare: int) -> int:
    """Spend a tick's unused body-fetch capacity on articles that missed it.

    A busy minute brings more articles than one cycle fetches bodies for, and
    the remainder used to stay headline-only permanently — nothing ever went
    back for them. Since alerts and searches match against the body, that was a
    silent loss of recall on exactly the busiest news.

    Only the leftovers of the cap are used, so a tick that is already at its
    limit does no extra work, and a quiet tick catches the backlog up.
    """
    if spare <= 0:
        return 0
    since = utcnow() - timedelta(hours=BACKFILL_HOURS)
    retry_before = utcnow() - timedelta(hours=BACKFILL_RETRY_HOURS)
    candidates = db.scalars(
        select(Article)
        .join(Source, Source.id == Article.source_id)
        .where(Article.content == "",
               Article.published_at >= since,
               Source.paywall.is_(False),
               # Never tried, or tried long enough ago to be worth one retry.
               or_(Article.content_tried_at.is_(None),
                   Article.content_tried_at < retry_before))
        # Newest first: the articles a reader is most likely to be looking at.
        .order_by(Article.published_at.desc())
        .limit(spare)
    ).all()
    if not candidates:
        return 0
    return await enrich_with_content(db, candidates, cap=spare)


def evaluate_alerts(db, new_articles: list[Article]) -> list[dict]:
    """Match new articles against every active alert; persist and return hits."""
    alerts = db.scalars(select(Alert).where(Alert.active.is_(True))).all()
    hits: list[dict] = []
    # Article-first, so each article's text — headline, summary and a body of
    # up to 20 KB — is assembled once instead of once per alert.
    matchers = [(alert, CriteriaMatcher(alert.criteria)) for alert in alerts]
    reads_text = any(matcher.needs_text for _, matcher in matchers)
    for article in new_articles:
        text = article_text(article) if reads_text else ""
        for alert, matcher in matchers:
            if matcher.matches(article, article.source, text=text):
                db.add(AlertEvent(alert_id=alert.id, article_id=article.id))
                alert.last_triggered_at = utcnow()
                hits.append({
                    "alert_id": alert.id,
                    "alert_name": alert.name,
                    "user_id": alert.user_id,
                    "pantheon_id": alert.pantheon_id,
                    "notify_email": bool(alert.notify_email),
                    "webhook_url": alert.webhook_url or "",
                    "article_id": article.id,
                    "title": article.title,
                    "url": article.url,
                    "importance": article.importance,
                    "country": article.country,
                })
    return hits


async def deliver_alerts(db, hits: list[dict]) -> int:
    """Out-of-app delivery for hits whose alert opted in: one email to the
    alert owner and/or one webhook POST per alert per cycle (batched so a busy
    alert can't flood). In-app SSE/toast is handled separately by the caller.
    Never raises — a failed channel is logged, ingestion continues."""
    from collections import defaultdict
    by_alert: dict[int, list[dict]] = defaultdict(list)
    for h in hits:
        if h.get("notify_email") or h.get("webhook_url"):
            by_alert[h["alert_id"]].append(h)
    if not by_alert:
        return 0
    delivered = 0
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        for items in by_alert.values():
            first = items[0]
            if first.get("notify_email") and mailer.enabled():
                uid = first["user_id"]
                user = db.get(User, int(uid.split(":", 1)[1])) if uid.startswith("acct:") else None
                if user and user.email:
                    try:
                        # smtplib is blocking — keep it off the event loop.
                        await asyncio.to_thread(mailer.send_alert_digest,
                                                user.email, first["alert_name"], items)
                        delivered += 1
                    except Exception:
                        log.exception("alert email delivery failed")
            url = first.get("webhook_url")
            if url:
                payload = {"alert": first["alert_name"], "count": len(items),
                           "hits": [{k: h[k] for k in ("title", "url", "importance",
                                                        "country", "article_id")} for h in items]}
                try:
                    await client.post(url, json=payload)
                    delivered += 1
                except Exception:
                    log.warning("alert webhook POST to %s failed", url)
    return delivered


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
    unchanged = 0
    for source, entries, fetch_status in results:
        source.last_fetched_at = utcnow()
        source.last_status = fetch_status
        if fetch_status == UNCHANGED:
            # A healthy poll that found nothing new. It counts as a success —
            # the source answered — but there is nothing to parse, and a city
            # feed that keeps saying this should still back off.
            source.consecutive_failures = 0
            source.last_article_count = 0
            if _is_city(source):
                source.idle_polls = (source.idle_polls or 0) + 1
            unchanged += 1
            ok_sources += 1
            continue
        if fetch_status != "ok":
            source.consecutive_failures = (source.consecutive_failures or 0) + 1
            continue
        source.consecutive_failures = 0
        for dom, pub in discovery.collect_publishers(entries).items():
            publishers.setdefault(dom, pub)
        # Commit per source so one bad feed can't roll back the whole batch.
        def store(source=source, entries=entries):
            new = process_entries(db, source, entries, recent, seen_urls)
            source.last_article_count = len(new)
            # Quiet city feeds back off; a productive poll resets the streak.
            if _is_city(source):
                source.idle_polls = 0 if new else (source.idle_polls or 0) + 1
            db.commit()
            return new

        try:
            # Off the event loop: scoring, language detection, geotagging, and
            # the FTS-indexed inserts are blocking CPU/disk work. Run inline,
            # they hold the loop for the whole batch — on a small VM that is
            # tens of seconds during which the server answers no requests at
            # all, and sign-in appears to hang. Awaiting a worker thread keeps
            # exactly one of these running (the session is never used
            # concurrently) while HTTP requests continue to be served.
            new = await asyncio.to_thread(store)
            ok_sources += 1
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
                        new = await asyncio.to_thread(
                            process_entries, db, source, entries, recent, seen_urls)
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
            new = await asyncio.to_thread(
                process_entries, db, source, entries, recent, seen_urls)
            source.last_article_count = len(new)
            db.commit()
            all_new.extend(new)
            discovered += 1
    except Exception:
        db.rollback()
        log.exception("source discovery failed")

    # Pull article bodies BEFORE alert evaluation so alerts see full text.
    content_fetched = 0
    backfilled = 0
    if CONTENT_FETCH:
        content_fetched = await enrich_with_content(db, all_new)
        # Whatever the cap did not spend on this batch goes to the articles
        # earlier batches had to skip, so a busy spell is caught up on the
        # quiet minutes that follow it rather than being lost.
        backfilled = await backfill_content(db, CONTENT_MAX_PER_CYCLE - content_fetched)

    # Clustering and alert matching scale with the batch and are equally
    # blocking — same treatment, so a large batch can't stall the server.
    def cluster_and_match():
        events = assign_events(db, all_new)
        matched = evaluate_alerts(db, all_new)
        db.commit()
        return events, matched

    new_events, hits = await asyncio.to_thread(cluster_and_match)
    # Out-of-app delivery (email/webhook) for alerts that opted in — after the
    # commit so a delivery hiccup can't lose the persisted hit.
    try:
        await deliver_alerts(db, hits)
    except Exception:
        log.exception("alert delivery pass failed")

    status.update({
        "last_run": utcnow().isoformat(),
        "last_error": None,
        "last_new_events": new_events,
        "last_content_fetched": content_fetched,
        "last_backfilled": backfilled,
        "last_new_articles": len(all_new),
        "last_repaired": repaired,
        "last_discovered": discovered,
        "sources_ok": ok_sources,
        "sources_total": len(sources),
        # How many polls the publisher answered with "nothing changed". The
        # higher this is, the more of the poll budget conditional requests are
        # giving back — an operator watching it can judge whether the intervals
        # could come down further.
        "last_unchanged": unchanged,
        "cycles": status.get("cycles", 0) + 1,
    })
    broadcaster.publish({"type": "cycle", "sources_ok": ok_sources,
                         "sources_total": len(sources), "new_articles": len(all_new)})
    if all_new:
        broadcaster.publish({"type": "articles", "count": len(all_new)})
    for hit in hits:
        broadcaster.publish({"type": "alert", **hit})
    log.info("ingest batch: %d/%d sources ok (%d unchanged), %d new articles, "
             "%d alert hits, %d repaired, %d discovered",
             ok_sources, len(sources), unchanged, len(all_new), len(hits),
             repaired, discovered)
    return {"new_articles": len(all_new), "new_events": new_events, "unchanged": unchanged,
            "alert_hits": len(hits), "content_fetched": content_fetched,
            "backfilled": backfilled,
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


def prune_old_articles(db) -> dict:
    """Retention: drop articles older than RETENTION_DAYS along with their
    translations and alert-history rows, then events that no longer have any
    articles (and their per-user viewed markers). Keeps the working set — and
    every board/search scan — bounded as ingestion runs forever."""
    if RETENTION_DAYS <= 0:
        return {"articles": 0, "events": 0}
    cutoff = utcnow() - timedelta(days=RETENTION_DAYS)
    old_ids = db.scalars(select(Article.id).where(Article.published_at < cutoff)).all()
    for i in range(0, len(old_ids), 500):
        chunk = old_ids[i:i + 500]
        db.execute(sa_delete(Translation).where(Translation.article_id.in_(chunk)))
        db.execute(sa_delete(AlertEvent).where(AlertEvent.article_id.in_(chunk)))
        db.execute(sa_delete(Article).where(Article.id.in_(chunk)))
    if old_ids:
        db.commit()
    orphan_ids = db.scalars(select(Event.id).where(
        Event.updated_at < cutoff,
        ~exists().where(Article.event_id == Event.id))).all()
    for i in range(0, len(orphan_ids), 500):
        chunk = orphan_ids[i:i + 500]
        db.execute(sa_delete(ViewedEvent).where(ViewedEvent.event_id.in_(chunk)))
        db.execute(sa_delete(Event).where(Event.id.in_(chunk)))
    if orphan_ids:
        db.commit()
    return {"articles": len(old_ids), "events": len(orphan_ids)}


_next_prune_at: float = 0.0  # monotonic deadline for the next retention pass


async def warm_home():
    """Re-match Home's shared columns, off the event loop.

    It is a plain blocking scan, and on a large corpus it is not a short one, so
    it runs in a worker thread with a session of its own rather than holding up
    every other request while it works."""
    def run():
        db = SessionLocal()
        try:
            return home.refresh(db)
        finally:
            db.close()

    try:
        await asyncio.to_thread(run)
    except Exception:
        log.exception("could not warm the Home board")


async def ingest_loop():
    """Continuous rolling poll: every tick, fetch whichever sources are due
    (news wires first, then a bounded slice of city feeds), paced per host so
    Google gets a steady drip rather than a burst. Each source refreshes on its
    own interval; nothing starves and no single tick runs long."""
    global _next_prune_at
    status["running"] = True
    while True:
        new_articles = 0
        try:
            async with cycle_lock:
                db = SessionLocal()
                try:
                    enabled = db.scalars(select(Source).where(Source.enabled.is_(True))).all()
                    due = due_sources(enabled, utcnow())
                    batch = ([s for s in due if not _is_city(s)][:POLL_BATCH]
                             + [s for s in due if _is_city(s)][:CITY_PER_TICK])
                    if batch:
                        new_articles = (await _ingest_batch(db, batch))["new_articles"]
                    if RETENTION_DAYS > 0 and time.monotonic() >= _next_prune_at:
                        _next_prune_at = time.monotonic() + PRUNE_EVERY_SECONDS
                        pruned = prune_old_articles(db)
                        if pruned["articles"] or pruned["events"]:
                            log.info("retention: pruned %d articles, %d empty events",
                                     pruned["articles"], pruned["events"])
                finally:
                    db.close()
        except Exception as exc:
            status["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
            log.exception("ingest tick failed")
        # Home's columns are one query per reader that never varies by reader,
        # so they are matched here instead of on arrival: whenever news landed,
        # and otherwise well before the warm lists would age out. Outside the
        # cycle lock, so a manual refresh is never kept waiting on it.
        warm = home.status()
        if (new_articles or not warm["columns"]
                or warm["oldest_age_s"] > home.MAX_AGE_S / 2):
            await warm_home()
        await asyncio.sleep(POLL_TICK)
