"""Cluster articles into events.

An "event" is a group of articles — usually from different outlets — covering
the same real-world happening. Clustering is incremental: each new article is
compared (Jaccard similarity over normalized headline tokens) against events
active within a rolling time window and attached to the best match, or it
founds a new event. Language note: tokens are surface forms, so coverage in
different languages of the same event clusters separately.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .models import Article, Event, utcnow
from .scoring import sets_similarity

SIM_THRESHOLD = 0.5
WINDOW_HOURS = 72
_MAX_EVENT_TOKENS = 18


class LiveEvents:
    """The events inside the clustering window, indexed by headline token.

    Every incoming article used to be compared against every live event —
    6,339 of them in a 72-hour window — re-splitting each event's tokens for
    each comparison, at 5.5ms per article, which is four seconds of a busy
    tick. Clustering already refuses to consider an event that shares fewer
    than two tokens, so an index over those tokens produces exactly the same
    candidates without touching the rest: measured 5.5ms → 0.06ms per article.
    """

    def __init__(self, events=()):
        self._events: list[Event] = []
        self._tokens: list[frozenset[str]] = []
        self._by_token: dict[str, list[int]] = {}
        for event in events:
            self.add(event)

    def __len__(self) -> int:
        return len(self._events)

    def add(self, event: Event) -> None:
        self._events.append(event)
        self._tokens.append(frozenset())
        self._reindex(len(self._events) - 1)

    def _reindex(self, at: int) -> None:
        """Pick up an event's current tokens.

        Merging rewrites them, so this runs again after every merge. Only new
        tokens are added: the cap on an event's token list means merging can
        also drop one, but a posting left behind merely offers a candidate that
        the exact comparison below then rejects."""
        was = self._tokens[at]
        now = frozenset(self._events[at].cluster_tokens.split())
        self._tokens[at] = now
        for word in now - was:
            self._by_token.setdefault(word, []).append(at)

    def best_match(self, article: Article, since=None) -> int | None:
        """Position of the event this article belongs to, or None for a new one.

        `since` drops events last touched before it, for a rebuild walking
        forward through time with a moving window."""
        words = set(article.cluster_tokens.split())
        if len(words) < 2:
            return None  # too sparse to cluster confidently (would over-merge
                         # on a single common token, e.g. a lone city name)
        shared: dict[int, int] = {}
        for word in words:
            for at in self._by_token.get(word, ()):
                shared[at] = shared.get(at, 0) + 1
        best, best_sim = None, 0.0
        # Ascending, so that among equally good matches the oldest event wins —
        # the order a scan of the whole list would have found them in.
        for at in sorted(shared):
            if shared[at] < 2:
                continue      # require ≥2 shared tokens, not just a high ratio
            if since is not None and self._events[at].updated_at < since:
                continue
            other = self._tokens[at]
            if len(words & other) < 2:
                continue      # a posting can outlive a token a merge dropped
            sim = sets_similarity(words, other)
            if sim > best_sim:
                best, best_sim = at, sim
        return best if best_sim >= SIM_THRESHOLD else None

    def event(self, at: int) -> Event:
        return self._events[at]

    def merged(self, at: int) -> None:
        """Tell the index an event's tokens have just changed."""
        self._reindex(at)


# How many distinct outlets one event tracks by name. Past this the answer to
# "how many outlets" is "lots", which is all any threshold needs to know.
MAX_EVENT_SOURCES = 100


def _merge(event: Event, article: Article) -> None:
    event.article_count += 1
    known = event.source_ids or []
    if article.source_id not in known and len(known) < MAX_EVENT_SOURCES:
        event.source_ids = known + [article.source_id]
    event.importance = max(event.importance, article.importance)
    if article.published_at and article.published_at > event.updated_at:
        event.updated_at = article.published_at
    if article.country and article.country not in (event.countries or []):
        event.countries = (event.countries or []) + [article.country]
    event.categories = sorted(set(event.categories or []) | set(article.categories or []))
    merged = sorted(set(event.cluster_tokens.split()) | set(article.cluster_tokens.split()))
    event.cluster_tokens = " ".join(merged[:_MAX_EVENT_TOKENS])


def _found_event(db: Session, article: Article) -> Event:
    event = Event(
        title=article.title,
        cluster_tokens=article.cluster_tokens,
        importance=article.importance,
        article_count=1,
        countries=[article.country] if article.country else [],
        categories=list(article.categories or []),
        source_ids=[article.source_id] if article.source_id else [],
        first_seen=article.published_at or utcnow(),
        updated_at=article.published_at or utcnow(),
    )
    db.add(event)
    db.flush()  # assign event.id
    return event


def live_events(db: Session) -> LiveEvents:
    """The events a newly ingested article could still be joined to.

    Separable from assign_events so a large batch can be clustered in slices
    without rebuilding this each time: gathering it is one query and an index
    over every event of the last WINDOW_HOURS, while the matching it feeds is
    per-article. Carrying one across the slices also keeps the result identical
    to clustering the batch in a single pass — an event created by an early
    article is still there for a later one to join.
    """
    since = utcnow() - timedelta(hours=WINDOW_HOURS)
    return LiveEvents(db.scalars(select(Event).where(Event.updated_at >= since)))


def assign_events(db: Session, articles: list[Article],
                  live: LiveEvents | None = None) -> int:
    """Attach newly ingested articles to live events (or create new ones).

    Returns the number of new events created.
    """
    if live is None:
        live = live_events(db)
    created = 0
    for article in sorted(articles, key=lambda a: a.published_at or utcnow()):
        at = live.best_match(article)
        if at is not None:
            event = live.event(at)
            _merge(event, article)
            live.merged(at)
            article.event_id = event.id
        else:
            event = _found_event(db, article)
            article.event_id = event.id
            live.add(event)
            created += 1
    return created


def rebuild_events(db: Session) -> int:
    """Recluster every stored article from scratch (chronologically)."""
    db.execute(update(Article).values(event_id=None))
    db.execute(delete(Event))
    db.flush()
    live = LiveEvents()
    count = 0
    for article in db.scalars(select(Article).order_by(Article.published_at)):
        published = article.published_at or utcnow()
        horizon = published - timedelta(hours=WINDOW_HOURS)
        at = live.best_match(article, since=horizon)
        if at is not None:
            event = live.event(at)
            _merge(event, article)
            live.merged(at)
            article.event_id = event.id
        else:
            event = _found_event(db, article)
            article.event_id = event.id
            live.add(event)
            count += 1
    db.commit()
    return count
