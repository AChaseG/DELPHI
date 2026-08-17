"""Feed ingestion: poll every enabled source, normalize + geotag + score new
articles, then evaluate active alerts and push events to connected clients.

Ingestion uses each outlet's published RSS/Atom feed — the channel publishers
provide for syndication — rather than scraping article HTML.
"""
from __future__ import annotations

import asyncio
import calendar
import json
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

from . import (accounts_backup, discovery, hazards, home, langdetect, mailer,
               repair, safefetch, storage, syndication, translate, watchdog)
from .clustering import assign_events, live_events
from .content import clean_summary, fetch_article_text
from .database import SessionLocal
from .events import broadcaster
from .matching import CriteriaMatcher, match_fields
from .models import (Alert, AlertEvent, Article, Event, FavoriteLocation, Source,
                     Translation, User, ViewedArticle, ViewedEvent, utcnow)
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
# Halved after a round was measured at 6m19s end to end — longer than the
# refresh interval it was serving, so sources were permanently overdue, every
# poll came back with a full load, and the machine never had an idle moment for
# anything else. A smaller round is not less news: what a source publishes is
# set by the publisher, and a poll that finds three items instead of twelve
# finds them three times as often. It is the same articles through a narrower
# door, which is what leaves room to answer a page request.
POLL_BATCH = int(os.environ.get("NEWS_POLL_BATCH", "40"))         # max non-city fetches per tick
CITY_PER_TICK = int(os.environ.get("NEWS_CITY_PER_TICK", "12"))   # max city feeds per tick (bounds the google drip)
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

# Consecutive failed polls before a source stops being one. A catalog grows on
# its own — auto-discovery adds outlets, watched places add searches — and
# nothing used to take one away, so a feed that went permanently dead was
# re-requested every three minutes for as long as the deployment lived. Five in
# a row is roughly twenty-five minutes of a source not answering.
REMOVE_AFTER = int(os.environ.get("NEWS_REMOVE_AFTER", "5"))

# How gently to start. Every source is overdue the moment the process starts,
# so the first ticks after a deploy poll a full batch, and each one lands
# hundreds of articles to score, index and cluster plus as many article bodies
# to fetch. On two shared vCPUs that saturates the machine: measured on the
# live deployment, the health check could not be answered inside its 10s
# timeout, Fly stopped routing to the only machine, and the site was
# unreachable for about two minutes with nothing wrong with it.
#
# Catching up is not urgent — the news is already there and a few minutes
# either way changes nothing — but being reachable is. So the first few
# minutes poll a fraction of a batch and the size ramps back to normal.
WARMUP_SECONDS = float(os.environ.get("NEWS_WARMUP_SECONDS", "300"))
WARMUP_FIRST_BATCH = int(os.environ.get("NEWS_WARMUP_FIRST_BATCH", "10"))


def warmup_batch(full: int, since_start: float) -> int:
    """How many sources this tick may poll, `since_start` seconds after boot.

    Ramps linearly from WARMUP_FIRST_BATCH to the full batch across the warmup
    window, so the catch-up gets steadily faster as the machine proves it can
    keep up rather than arriving all at once.
    """
    if since_start >= WARMUP_SECONDS or full <= WARMUP_FIRST_BATCH:
        return full
    share = max(0.0, since_start) / WARMUP_SECONDS
    return max(1, min(full, round(WARMUP_FIRST_BATCH + (full - WARMUP_FIRST_BATCH) * share)))

# Article retention: with ~500+ sources the table grows without bound and
# every board query slows over time. Articles older than this are pruned
# (with their translations and alert-history rows); 0 disables pruning.
RETENTION_DAYS = float(os.environ.get("NEWS_RETENTION_DAYS", "30"))
PRUNE_EVERY_SECONDS = 6 * 3600
# When a bounded pass did not reach the cutoff, the gap before trying again.
# Long enough that catching up does not become the only thing the poller does,
# short enough that a changed retention window takes hours rather than days.
PRUNE_CATCHUP_SECONDS = float(os.environ.get("NEWS_PRUNE_CATCHUP_S", "60"))
# How often the catalog is re-checked against the current adoption rules.
# Daily: the rules change when somebody notices something that should never
# have been adopted, which is not an hourly event, and the sweep reads every
# source row.
AUDIT_EVERY_SECONDS = float(os.environ.get("NEWS_AUDIT_EVERY_S", str(24 * 3600)))
USER_AGENT = "Delphi/1.0 (+RSS reader; respects robots and publisher feeds)"
# Some CDNs 403 any unknown agent while happily serving browsers the same
# public syndication feed; retry once with a browser UA before giving up.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"

status: dict = {"running": False, "last_run": None, "last_new_articles": 0, "cycles": 0}
# Present from import, not from the first successful poll. /api/meta ships this
# dict to every client, and while this key was simply absent until a poll
# succeeded there was no way for anything — the map, Settings, an operator — to
# tell a hazard layer that is switched off from one that is working and quiet.
# Both drew a blank map under a ticked box. An always-present key is the whole
# fix; everything downstream just reads it.
status["hazards"] = hazards.idle_status("no poll has run yet")

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

    def __init__(self, rows: list[tuple[int, str]] = (),
                 newsroom: dict[int, int] | None = None):
        """`newsroom` maps a source to the id that counts as its publisher.

        Without it every feed is its own outlet, which is what made a publishing
        group look like a consensus: seven Vocento mastheads running one
        newsroom's copy corroborated each other seven times over. Sources
        sharing a newsroom now corroborate once between them. See
        syndication.py, which learns the mapping.
        """
        self._entries: list[tuple[int, frozenset[str]]] = []
        self._by_token: dict[str, list[int]] = {}
        self._newsroom = newsroom or {}
        for source_id, tokens in rows:
            self.add(source_id, tokens)

    def publisher(self, source_id: int) -> int:
        """The identity that corroborates: a newsroom, not a masthead."""
        return self._newsroom.get(source_id, source_id)

    def __len__(self) -> int:
        """How many headlines are indexed — the number that decides whether
        building this is milliseconds or a minute, so it is worth reporting."""
        return len(self._entries)

    def add(self, source_id: int, tokens: str) -> None:
        words = frozenset(tokens.split())
        if not words:
            return
        at = len(self._entries)
        # Stored as the publisher, so every comparison below is already between
        # newsrooms and the hot loop does no extra lookups.
        self._entries.append((self.publisher(source_id), words))
        for word in words:
            self._by_token.setdefault(word, []).append(at)

    def corroboration(self, source_id: int, tokens: str) -> int:
        """How many other newsrooms ran a headline this one matches."""
        words = set(tokens.split())
        if not words:
            return 0
        mine = self.publisher(source_id)
        others: set[int] = set()
        checked: set[int] = set()
        for word in words:
            for at in self._by_token.get(word, ()):
                if at in checked:
                    continue
                checked.add(at)
                other_source, other_words = self._entries[at]
                # One newsroom corroborates once, so a publisher already
                # counted needs no further comparison — and a sister masthead
                # running the same copy is the same publisher, not a second
                # opinion.
                if other_source == mine or other_source in others:
                    continue
                shared = len(words & other_words)
                if (max(shared / len(words | other_words),
                        shared / min(len(words), len(other_words))) >= 0.5):
                    others.add(other_source)
        return len(others)


# How far back corroboration looks, and the most headlines it will ever hold.
#
# Both are bounds this had none of, and it was measured on "a window of 2,016
# headlines" (see RecentClusters). At 9,400 sources the 48-hour window is about
# 350,000, which is 173 times its design input: building the index takes 6.6s on
# a fast laptop and tens of seconds on one shared core, and it was being rebuilt
# from scratch at the top of every batch, on the event loop. That is what the
# stall recorder kept reporting as 20-to-60-second freezes during "fetch".
#
# The cap is the important half. Hours alone is not a bound, because how many
# articles two days holds depends entirely on how many sources are enabled --
# the same mistake the article ceiling exists to correct.
CLUSTER_WINDOW_HOURS = float(os.environ.get("NEWS_CLUSTER_WINDOW_HOURS", "12"))
CLUSTER_WINDOW_MAX = int(os.environ.get("NEWS_CLUSTER_WINDOW_MAX", "40000"))

# How long an index is reused before being rebuilt from the database. Rebuilding
# is what costs; the batch already adds its own new articles as it goes, so
# between rebuilds the index stays current for everything that arrived. All a
# rebuild buys is dropping what has aged out of the window.
CLUSTER_REBUILD_EVERY_S = float(
    os.environ.get("NEWS_CLUSTER_REBUILD_EVERY_S", "1800"))

_clusters: RecentClusters | None = None
_clusters_built_at: float = 0.0


def _recent_clusters(db, hours: float | None = None) -> RecentClusters:
    """The corroboration index, newest first and bounded on both sides."""
    since = utcnow() - timedelta(
        hours=CLUSTER_WINDOW_HOURS if hours is None else hours)
    rows = db.execute(
        select(Article.source_id, Article.cluster_tokens)
        .where(Article.published_at >= since, Article.cluster_tokens != "")
        # Newest first, so a cap drops the least useful end of the window: an
        # unlimited query would be bounded by whatever the disk holds.
        .order_by(Article.published_at.desc())
        .limit(CLUSTER_WINDOW_MAX)
    ).all()
    return RecentClusters([(r[0], r[1]) for r in rows],
                          newsroom=syndication.group_map(db))


def cluster_index(db) -> RecentClusters:
    """The shared index, rebuilt only when it has gone stale.

    Kept between batches deliberately. `process_entries` adds each new article
    to it, so reuse costs nothing in accuracy for anything that has arrived
    since — only entries that have aged out of the window linger, and they
    corroborate a story that is genuinely a day old, which is the small end of
    a wrong answer.
    """
    global _clusters, _clusters_built_at
    if (_clusters is None
            or time.monotonic() - _clusters_built_at >= CLUSTER_REBUILD_EVERY_S):
        _clusters = _recent_clusters(db)
        _clusters_built_at = time.monotonic()
        log.info("corroboration index rebuilt: %d headlines", len(_clusters))
    return _clusters


def reset_cluster_index() -> None:
    """Forget it — for tests, and after a prune has removed most of the window."""
    global _clusters, _clusters_built_at
    _clusters = None
    _clusters_built_at = 0.0


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

        # Trailers first, then the length cap: a generator's "appeared first on
        # <Site>" is searched exactly as the headline is, and it ends every item
        # with the publication's own name.
        summary = clean_summary(_strip_html(entry.get("summary", "")))[:2000]
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
    async with safefetch.client(timeout=CONTENT_TIMEOUT) as client:
        async def one(article):
            async with sem:
                return article, await fetch_article_text(client, article.url)
        results = await asyncio.gather(*(one(a) for a in todo))
    fetched = 0
    for i in range(0, len(results), ENRICH_CHUNK):
        fetched += await asyncio.to_thread(_enrich_chunk, results[i:i + ENRICH_CHUNK])
    await asyncio.to_thread(db.commit)
    return fetched


# How many articles one enrichment slice covers. Sized against the machine
# rather than the work: performance-1x is a *single* dedicated core, so a
# thread doing this and the thread answering requests are competing for the one
# processor. The average is comfortable — ingestion needs about 45s of CPU per
# 76s cycle — but a health check allows 10s and it is burstiness that misses
# it, not the average. Smaller slices mean more, shorter bursts.
ENRICH_CHUNK = int(os.environ.get("NEWS_ENRICH_CHUNK", "10"))

# The same handover, for clustering and alert matching. Larger because the work
# per article is smaller — matching one article against the live events and the
# active alerts, rather than re-reading a whole body.
CLUSTER_CHUNK = int(os.environ.get("NEWS_CLUSTER_CHUNK", "25"))


def _enrich_chunk(batch: list[tuple]) -> int:
    """Re-derive places, country, categories and language from a full body.

    Off the event loop, in slices, because this is where the server used to go
    silent. Everything here is pure Python over the whole article text, and a
    cycle runs up to CONTENT_MAX_PER_CYCLE of it twice — once for the batch just
    fetched and once for the backlog. Straight-line on the loop that measured
    24 seconds per pass, during which nothing was served: not a page, not an API
    call, not /healthz. Fly's check gives up after 10 seconds and de-routes the
    machine, so the site stopped loading for everyone for over a minute at a
    time, several times a day, with the app itself perfectly healthy.

    Neither a thread nor a slice makes the work cheaper — the interpreter still
    runs it one bytecode at a time. What they buy is interruptibility: the loop
    gets scheduled between slices, so a request waits milliseconds instead of
    the rest of the pass.
    """
    fetched = 0
    for article, text in batch:
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

    def find():
        # Bodies are the largest column in the table, and this scans a window of
        # them looking for the empty ones. Off the loop with the rest of the
        # cycle's heavy work — it sits in the same gap, immediately after the
        # enrichment pass, so leaving it here would have kept part of the stall.
        return db.scalars(
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

    candidates = await asyncio.to_thread(find)
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
        text = match_fields(article) if reads_text else None
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
    async with safefetch.client(timeout=10, follow_redirects=False) as client:
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
    async with safefetch.client(timeout=FETCH_TIMEOUT) as client:
        async def one(source):
            host = _host(source)
            delay = await pacer.reserve(host, host_gap(host, counts[host]))
            if delay > 0:
                await asyncio.sleep(delay)
            async with sem:
                return await fetch_source(client, source)
        return await asyncio.gather(*(one(s) for s in sources))


def _has_ever_produced(db, source: Source) -> bool:
    """Has this source ever put an article in the database?

    One indexed lookup that stops at the first row — the question is whether
    there is any, not how many, and some sources have tens of thousands.
    """
    return db.scalar(select(Article.id).where(
        Article.source_id == source.id).limit(1)) is not None


def retire_or_remove(db, source: Source) -> str | None:
    """Deal with a source that has stopped answering. Returns what was done.

    Deleting a source takes its articles with it — the relationship cascades —
    and five failures in a row is only about twenty-five minutes of downtime.
    A publisher having a bad morning must not cost Delphi everything it has
    ever carried from them, so what happens depends on whether there is
    anything to lose:

    *Nothing ever came from it* — deleted outright. This is the case that
    prompted the rule: a catalog that grows by itself accumulates feeds that
    404, feeds behind a login, and search URLs for places nobody watches any
    more, and none of them have contributed a line. Removing them costs
    nothing and stops the polling.

    *It has published* — retired instead: disabled, so it is never polled
    again, and left in the Sources panel saying why. The reporting it already
    gathered stays readable, and an operator who knows the feed has really gone
    can delete it by hand.

    A source somebody added by hand is retired rather than deleted whatever its
    record. Deleting the operator's own entry without asking is a worse failure
    than keeping a dead row they can see and remove.

    Automatic repair gets its turn first. It rediscovers the feed URL of a
    source stuck on a permanent-looking error, and only five sources are tried
    per cycle — so a source can reach the failure limit without ever having
    been offered the fix. One that is still waiting for its first attempt is
    left alone.
    """
    if (source.consecutive_failures or 0) < REMOVE_AFTER:
        return None
    if (repair.AUTO_REPAIR and repair.needs_repair(source.last_status)
            and source.last_repair_at is None):
        return None

    was = (source.last_status or "no answer")[:120]
    if source.added_by == "user" or _has_ever_produced(db, source):
        source.enabled = False
        source.last_status = (
            f"retired: no answer in {REMOVE_AFTER} tries ({was}). "
            "Its stories are kept; re-enable it if the feed comes back.")[:200]
        log.info("retired source %s (%s)", source.name, source.rss_url)
        return "retired"

    # A watched place points at its search feed by id; leave it pointing at
    # nothing rather than at a row that has gone, and the next save re-creates
    # it. (SQLite does not enforce the foreign key for us here.)
    for loc in db.scalars(select(FavoriteLocation).where(
            FavoriteLocation.source_id == source.id)):
        loc.source_id = None
    log.info("removed source %s (%s) — never produced an article: %s",
             source.name, source.rss_url, was)
    db.delete(source)
    return "removed"


async def _ingest_batch(db, sources: list[Source]) -> dict:
    """Poll `sources`, insert new articles, then run repair, discovery,
    content enrichment, clustering, and alert evaluation over the result.
    The caller owns `db`. Returns stats for this batch."""
    if not sources:
        return {"new_articles": 0, "new_events": 0, "alert_hits": 0,
                "content_fetched": 0, "repaired": 0, "discovered": 0,
                "retired": 0, "removed": 0,
                "sources_ok": 0, "sources_total": 0}
    # Phase timings, logged with the batch summary. Every stall this poller has
    # caused was one phase running long, and each time it had to be inferred
    # from where the log went quiet — which only works if the phase is the sort
    # that logs at all. Measuring them is a few microseconds and turns the next
    # occurrence into something to read rather than deduce.
    phase: dict[str, float] = {}
    _t = time.monotonic()

    def mark(name):
        """Close off the phase that just ended, and name the one starting."""
        nonlocal _t
        now = time.monotonic()
        phase[name] = now - _t
        _t = now

    def start(name):
        # So a stall recorded mid-cycle can say which phase was running. The
        # timings are only printed when a cycle ends, which is no help if the
        # question is what it was doing while it was stuck.
        watchdog.doing(f"ingest:{name}")

    start("fetch")
    # Off the loop: a rebuild reads tens of thousands of rows and builds an
    # index out of them, which is pure Python and used to run right here, where
    # nothing else could be answered while it did. A thread does not make it
    # cheaper, only interruptible — which is the difference between a slow tick
    # and a health check Fly abandons after ten seconds.
    recent = await asyncio.to_thread(cluster_index, db)
    results = await _fetch_batch(sources)
    mark("fetch")
    start("store")

    all_new: list[Article] = []
    ok_sources = 0
    seen_urls: set[str] = set()
    publishers: dict[str, tuple[str, str]] = {}  # outlets named by entries' <source> tags
    unchanged = 0
    failed: list[Source] = []      # dealt with after the loop, not during it
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
            failed.append(source)
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
        async with safefetch.client(timeout=repair.REPAIR_TIMEOUT) as client:
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

    # Sources that have run out of chances. After the repair pass, not before:
    # a successful repair clears the failure count, and a source that can be
    # fixed should be fixed rather than removed.
    retired = removed = 0
    for source in failed:
        try:
            what = retire_or_remove(db, source)
        except Exception:
            db.rollback()
            log.exception("could not retire source %s", source.name)
            continue
        if what == "removed":
            removed += 1
        elif what == "retired":
            retired += 1
    if retired or removed:
        db.commit()

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
    mark("store")
    start("enrich")
    content_fetched = 0
    backfilled = 0
    if CONTENT_FETCH:
        content_fetched = await enrich_with_content(db, all_new)
        mark("enrich")
        start("backfill")
        # Whatever the cap did not spend on this batch goes to the articles
        # earlier batches had to skip, so a busy spell is caught up on the
        # quiet minutes that follow it rather than being lost.
        backfilled = await backfill_content(db, CONTENT_MAX_PER_CYCLE - content_fetched)
        mark("backfill")
        start("cluster")

    # Clustering and alert matching scale with the batch too, and a busy
    # minute brings 800+ articles. Moving them to a thread was not enough on
    # its own: one thread holding the interpreter for the whole batch starves
    # the loop just as thoroughly as running there, it merely stops logging
    # about it. Sliced, like the enrichment pass above, for the same reason.
    #
    # Sorted once here rather than inside each slice, so slicing an ordered
    # list preserves the chronological order the clustering depends on, and one
    # LiveEvents is carried across the slices — clustering the batch this way
    # gives the same answer as clustering it in a single pass.
    all_new.sort(key=lambda a: a.published_at or utcnow())
    live = await asyncio.to_thread(live_events, db)
    new_events = 0
    hits: list[dict] = []
    for i in range(0, len(all_new), CLUSTER_CHUNK):
        def slice_(chunk=all_new[i:i + CLUSTER_CHUNK]):
            created = assign_events(db, chunk, live=live)
            matched = evaluate_alerts(db, chunk)
            # Per slice, so the transaction stays small and a reader sees the
            # batch land progressively instead of all at once at the end.
            db.commit()
            return created, matched

        created, matched = await asyncio.to_thread(slice_)
        new_events += created
        hits.extend(matched)
    mark("cluster")
    watchdog.doing("idle")
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
        # Sources the catalog lost this batch. The catalog grows by itself, so
        # an operator needs to see it shrink by itself too.
        "last_retired": retired,
        "last_removed": removed,
        "retired_total": status.get("retired_total", 0) + retired,
        "removed_total": status.get("removed_total", 0) + removed,
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
    status["stalls"] = watchdog.stalls()
    status["worst_stall_s"] = watchdog.worst()
    log.info("ingest batch: %d/%d sources ok (%d unchanged), %d new articles, "
             "%d alert hits, %d repaired, %d discovered, %d retired, %d removed "
             "[%s]",
             ok_sources, len(sources), unchanged, len(all_new), len(hits),
             repaired, discovered, retired, removed,
             " ".join(f"{name} {secs:.1f}s" for name, secs in phase.items()))
    return {"new_articles": len(all_new), "new_events": new_events, "unchanged": unchanged,
            "alert_hits": len(hits), "content_fetched": content_fetched,
            "backfilled": backfilled,
            "repaired": repaired, "discovered": discovered,
            "retired": retired, "removed": removed,
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


def _delete_articles(db, ids: list[int]) -> None:
    """Remove articles and everything that only exists to point at them.

    Every table with an article_id belongs here. Missing one does not fail
    loudly — it leaves rows referring to articles that no longer exist, growing
    for as long as the system runs. ViewedArticle was exactly that: written on
    every read and never cleaned up, so each "already seen" marker outlived its
    article permanently.

    The full-text index is not in this list because it does not need to be: it
    is an external-content FTS5 table with AFTER DELETE triggers, so it follows
    the articles table by itself.
    """
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        db.execute(sa_delete(Translation).where(Translation.article_id.in_(chunk)))
        db.execute(sa_delete(AlertEvent).where(AlertEvent.article_id.in_(chunk)))
        db.execute(sa_delete(ViewedArticle).where(ViewedArticle.article_id.in_(chunk)))
        db.execute(sa_delete(Article).where(Article.id.in_(chunk)))
    if ids:
        db.commit()


def _drop_empty_events(db, cutoff) -> int:
    orphan_ids = db.scalars(select(Event.id).where(
        Event.updated_at < cutoff,
        ~exists().where(Article.event_id == Event.id))
        .order_by(Event.updated_at.asc()).limit(EMPTY_EVENT_BATCH)).all()
    for i in range(0, len(orphan_ids), 500):
        chunk = orphan_ids[i:i + 500]
        db.execute(sa_delete(ViewedEvent).where(ViewedEvent.event_id.in_(chunk)))
        db.execute(sa_delete(Event).where(Event.id.in_(chunk)))
    if orphan_ids:
        db.commit()
    return len(orphan_ids)


# Whatever the disk says, articles this new are never dropped. Without a floor,
# a database that will not shrink — one not yet converted to incremental
# vacuum, where deleting changes the file size not at all — would be pruned
# further and further in pursuit of a number it cannot reach, until there was
# no news left. Age-based retention is the rule; size is the backstop.
MIN_KEEP_DAYS = float(os.environ.get("NEWS_MIN_KEEP_DAYS", "2"))
# Ceiling on how much one size-driven pass will remove, so it works the backlog
# down over several passes instead of stalling a tick.
OVERSIZE_BATCH = int(os.environ.get("NEWS_OVERSIZE_BATCH", "5000"))

# The same ceiling for the age-driven rule, and it was missing. Ordinarily
# age-based pruning removes one day's tail per pass and a bound never binds —
# but *lowering* RETENTION_DAYS makes every article between the old cutoff and
# the new one eligible at once, and `_next_prune_at` starts at zero so it runs
# on the first tick after the deploy that changed it. Unbounded, that is one
# delete of several hundred thousand rows, each cascading through the full-text
# index, on the tick a deploy has just restarted: precisely the hours-long
# outage this module keeps trying to prevent, triggered by the setting meant to
# prevent it. Bounded, the same change is absorbed over a few passes.
RETENTION_BATCH = int(os.environ.get("NEWS_RETENTION_BATCH", "5000"))

# Events are dropped by scanning for those no articles point at any more, which
# is a full pass over a table with as many rows as there are stories. Bounded
# for the same reason.
EMPTY_EVENT_BATCH = int(os.environ.get("NEWS_EMPTY_EVENT_BATCH", "5000"))


def prune_old_articles(db) -> dict:
    """Retention: drop articles older than RETENTION_DAYS along with their
    translations, alert history and viewed markers, then events that no longer
    have any articles. Keeps the working set — and every board/search scan —
    bounded as ingestion runs forever.

    Oldest first and bounded per pass, so shortening the retention window is
    absorbed over several passes rather than in one stalling delete. `more`
    says the window is not caught up yet, so a caller can come back sooner
    than the usual six hours instead of leaving the backlog until tomorrow.
    """
    if RETENTION_DAYS <= 0:
        return {"articles": 0, "events": 0, "more": False}
    cutoff = utcnow() - timedelta(days=RETENTION_DAYS)
    old_ids = db.scalars(
        select(Article.id).where(Article.published_at < cutoff)
        .order_by(Article.published_at.asc()).limit(RETENTION_BATCH)).all()
    _delete_articles(db, list(old_ids))
    events = _drop_empty_events(db, cutoff)
    return {"articles": len(old_ids), "events": events,
            "more": len(old_ids) >= RETENTION_BATCH}


def prune_to_fit(db) -> dict:
    """Drop the oldest articles until the archive is back under its ceiling.

    Age-based retention cannot bound a database on its own, because how much
    thirty days weighs depends entirely on how much news happened. A quiet
    month and a busy one differ by more than the volume has spare. This is the
    backstop that keeps the file inside the disk it lives on.

    Deliberately oldest-first and bounded on both sides: never past
    MIN_KEEP_DAYS, never more than OVERSIZE_BATCH in one pass.
    """
    excess = storage.over_ceiling()
    if excess <= 0:
        return {"articles": 0, "events": 0, "excess_bytes": 0}

    floor = utcnow() - timedelta(days=MIN_KEEP_DAYS)
    ids = db.scalars(
        select(Article.id).where(Article.published_at < floor)
        .order_by(Article.published_at.asc()).limit(OVERSIZE_BATCH)).all()
    if not ids:
        log.warning(
            "archive is %.0f MB over its ceiling but nothing is older than "
            "%.1f days — the disk is too small for this much news, or "
            "NEWS_DB_MAX_FRACTION is set too low",
            excess / 1e6, MIN_KEEP_DAYS)
        return {"articles": 0, "events": 0, "excess_bytes": excess}

    _delete_articles(db, list(ids))
    events = _drop_empty_events(db, floor)
    # Deleting alone changes nothing on disk; this is the half that does.
    storage.checkpoint()
    freed = storage.reclaim()
    log.info("archive was %.0f MB over its ceiling: dropped %d of the oldest "
             "articles, returned %.0f MB to the disk",
             excess / 1e6, len(ids), freed / 1e6)
    return {"articles": len(ids), "events": events, "excess_bytes": excess,
            "freed_bytes": freed}


_next_prune_at: float = 0.0  # monotonic deadline for the next retention pass
# Zero, so the first tick after a restart runs it. Tightening the rules is
# pointless if the catalog keeps yesterday's until this time tomorrow, and a
# deploy is exactly when the rules have just changed.
_next_audit_at: float = 0.0
# Not zero: mailing a copy of every account is not what a restart is for, and a
# deploy loop would send one per deploy. The first copy goes out an hour in.
_next_account_backup_at: float = 3600.0
# A minute in, for the same reason as the backup but shorter: a restart's first
# tick should be spent on the source catalog warming up, not on somebody else's
# API. A minute is soon enough that a deploy shows hazards almost immediately.
_next_hazard_at: float = 60.0


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


def reading_languages(db) -> list[str]:
    """The languages accounts on this server actually read in.

    Read from the settings blob rather than assumed, so a server whose only
    reader is English does not pay to translate the world into sixteen
    languages nobody has selected.
    """
    langs: dict[str, None] = {}
    for blob in db.scalars(select(User.settings)):
        if not blob:
            continue
        try:
            lang = (json.loads(blob) or {}).get("lang")
        except (ValueError, TypeError):
            continue
        if isinstance(lang, str) and lang.strip():
            langs[lang.strip().lower()[:2]] = None
    return list(langs)


async def warm_translations():
    """Translate Home's warmed stories before a reader opens the board.

    Translation is per article and per language, cached forever after the
    first time — but the first time was being paid by whoever opened the
    board, inside their request, one network round-trip deep per story. A
    column of forty foreign-language stories meant a reader waiting on dozens
    of calls to a translation service before the page could be drawn at all,
    which is what a `slow:7.2s POST /api/articles/search` in the log was.

    Showing the original text sooner is not an answer: someone who reads only
    English is no better served by Mandarin arriving quickly. So the wait is
    moved rather than shortened — the stories Home has just warmed are
    translated here, between cycles, into the languages this server's accounts
    actually read, so that by the time anyone looks the answer is already in
    the database and the request costs one query.

    Best-effort throughout. A translation service that is slow, rate-limiting
    or down must delay news, not stop it, so every failure is logged and
    swallowed; the reader simply pays for that story the old way.
    """
    if not translate.enabled():
        return
    db = None
    try:
        ids = home.warm_article_ids()
        if not ids:
            return
        db = SessionLocal()
        langs = await asyncio.to_thread(reading_languages, db)
        if not langs:
            return
        articles = await asyncio.to_thread(
            lambda: db.scalars(select(Article).where(Article.id.in_(ids))).all())
        for lang in langs:
            await translate.translate_articles(db, articles, lang)
    except Exception:
        log.exception("could not warm translations")
    finally:
        if db is not None:
            db.close()


async def ingest_loop():
    """Continuous rolling poll: every tick, fetch whichever sources are due
    (news wires first, then a bounded slice of city feeds), paced per host so
    Google gets a steady drip rather than a burst. Each source refreshes on its
    own interval; nothing starves and no single tick runs long."""
    global _next_prune_at, _next_audit_at, _next_account_backup_at, _next_hazard_at
    status["running"] = True
    started_at = time.monotonic()      # the warmup ramp measures from here
    # The one-off conversion to incremental auto-vacuum is NOT done here, and
    # that is a correction to how this shipped. It VACUUMs — rewriting the
    # whole database while holding a lock that blocks readers, not just
    # writers, for however long a multi-gigabyte copy takes. Running that
    # automatically on the first tick after a deploy means the site stalls for
    # minutes with no warning and no way to choose the moment. It is an
    # operator's decision now: POST /api/maintenance/reclaim-space, with the
    # console saying plainly what it will cost. Databases created from here on
    # are incremental from birth (see database.py) and never need it.

    while True:
        new_articles = 0
        try:
            async with cycle_lock:
                db = SessionLocal()
                try:
                    space = storage.disk()
                    if space["ok"] and space["low"]:
                        # Refusing to fetch is what turns "the disk filled" into
                        # a degraded system instead of one that will not start.
                        # Pruning still runs below — the way out is down, not up.
                        status["last_error"] = (
                            f"Only {space['free_bytes'] / 1e6:.0f} MB free on the "
                            f"data volume — ingestion is paused so the database "
                            f"can still be opened. Old articles are being cleared; "
                            f"if this persists the volume needs to be larger.")
                        log.error("storage: %s", status["last_error"])
                    else:
                        # Twelve hundred rows built into ORM objects, every
                        # fifteen seconds, to decide which handful is due. Small
                        # next to a batch, but it is on the loop and it repeats
                        # four times a minute forever.
                        def due_now():
                            enabled = db.scalars(
                                select(Source).where(Source.enabled.is_(True))).all()
                            return due_sources(enabled, utcnow())

                        due = await asyncio.to_thread(due_now)
                        since_start = time.monotonic() - started_at
                        batch = (
                            [s for s in due if not _is_city(s)]
                            [:warmup_batch(POLL_BATCH, since_start)]
                            + [s for s in due if _is_city(s)]
                            [:warmup_batch(CITY_PER_TICK, since_start)])
                        if batch:
                            new_articles = (await _ingest_batch(db, batch))["new_articles"]

                    # Typhon, on the news side of the disk guard rather than
                    # the housekeeping side. Audit and backup run even when the
                    # volume is nearly full because they mostly delete; a
                    # hazard poll writes, so it stops when fetching stops. This
                    # is the disk-full outage trying to come back in a new door.
                    if not hazards.enabled():
                        status["hazards"] = hazards.idle_status(
                            "switched off with NEWS_HAZARDS=0")
                    elif space["ok"] and space["low"]:
                        status["hazards"] = hazards.idle_status(
                            "paused — the data volume is nearly full")
                    elif time.monotonic() >= _next_hazard_at:
                        _next_hazard_at = time.monotonic() + hazards.POLL_EVERY_SECONDS
                        status["hazards"] = await hazards.poll(db)

                    if time.monotonic() >= _next_audit_at:
                        _next_audit_at = time.monotonic() + AUDIT_EVERY_SECONDS
                        # Reads every source row and writes a handful, so it
                        # goes where the rest of the cycle's database work
                        # goes rather than on the loop.
                        audit = await asyncio.to_thread(discovery.audit_catalog, db)
                        status["last_audit"] = utcnow().isoformat()
                        status["last_audit_disabled"] = audit["disabled"]
                        status["audited_total"] = (
                            status.get("audited_total", 0) + audit["disabled"])
                        # Which mastheads share a newsroom, relearned on the
                        # same schedule: a publishing group acquires and drops
                        # titles, and a mapping nobody revisits becomes a claim
                        # about last month. Same thread, same reason.
                        status["syndication"] = await asyncio.to_thread(
                            syndication.detect, db)
                        # The corroboration index holds the old grouping, so it
                        # has to be rebuilt against the new one.
                        reset_cluster_index()

                    if (accounts_backup.EVERY_SECONDS > 0
                            and time.monotonic() >= _next_account_backup_at):
                        _next_account_backup_at = (
                            time.monotonic() + accounts_backup.EVERY_SECONDS)
                        # Fly's own volume snapshots are off, because copying
                        # the whole archive nightly took the site off the
                        # internet for hours. This is the replacement, and it
                        # only works because it is the small part: a few
                        # kilobytes of accounts rather than gigabytes of news,
                        # mailed away from this machine because a copy on the
                        # volume is no help when the volume is what was lost.
                        await asyncio.to_thread(accounts_backup.send_scheduled, db)

                    due_to_prune = time.monotonic() >= _next_prune_at
                    # When space is short, prune every tick rather than every
                    # six hours: the interval is tuned for housekeeping, and
                    # this is no longer housekeeping.
                    if RETENTION_DAYS > 0 and (due_to_prune or (space["ok"] and space["low"])):
                        if due_to_prune:
                            _next_prune_at = time.monotonic() + PRUNE_EVERY_SECONDS

                        def housekeeping():
                            # Bulk deletes, a WAL checkpoint and an incremental
                            # vacuum: minutes of database work on a large table,
                            # and the same stall as the enrichment pass if it
                            # runs where requests are answered. Rare — every six
                            # hours — which is exactly what makes it the kind of
                            # outage nobody catches in the act.
                            pruned = prune_old_articles(db)
                            # Age-based pruning first, then whatever the disk
                            # still demands beyond it.
                            prune_to_fit(db)
                            if pruned["articles"]:
                                storage.checkpoint()
                                storage.reclaim()
                            return pruned

                        pruned = await asyncio.to_thread(housekeeping)
                        if pruned["articles"] or pruned["events"]:
                            log.info("retention: pruned %d articles, %d empty events",
                                     pruned["articles"], pruned["events"])
                        # A bounded pass that filled its batch means the window
                        # is not caught up. Come back on the next tick instead
                        # of in six hours: shortening the retention window would
                        # otherwise take days to take effect, one batch a
                        # quarter-day, which is indistinguishable from the
                        # setting having been ignored.
                        if pruned.get("more"):
                            _next_prune_at = time.monotonic() + PRUNE_CATCHUP_SECONDS
                            log.info("retention: more to remove, next pass in %.0fs",
                                     PRUNE_CATCHUP_SECONDS)
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
            # Straight after, so the stories it just warmed are readable in
            # the reader's own language before the reader arrives.
            await warm_translations()
        await asyncio.sleep(POLL_TICK)
