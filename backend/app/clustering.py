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
from .scoring import tokens_similarity

SIM_THRESHOLD = 0.5
WINDOW_HOURS = 72
_MAX_EVENT_TOKENS = 18


def _best_match(article: Article, events: list[Event]) -> Event | None:
    if not article.cluster_tokens:
        return None
    best, best_sim = None, 0.0
    for ev in events:
        sim = tokens_similarity(article.cluster_tokens, ev.cluster_tokens)
        if sim > best_sim:
            best, best_sim = ev, sim
    return best if best_sim >= SIM_THRESHOLD else None


def _merge(event: Event, article: Article) -> None:
    event.article_count += 1
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
        first_seen=article.published_at or utcnow(),
        updated_at=article.published_at or utcnow(),
    )
    db.add(event)
    db.flush()  # assign event.id
    return event


def assign_events(db: Session, articles: list[Article]) -> int:
    """Attach newly ingested articles to live events (or create new ones).

    Returns the number of new events created.
    """
    since = utcnow() - timedelta(hours=WINDOW_HOURS)
    live = list(db.scalars(select(Event).where(Event.updated_at >= since)))
    created = 0
    for article in sorted(articles, key=lambda a: a.published_at or utcnow()):
        match = _best_match(article, live)
        if match:
            _merge(match, article)
            article.event_id = match.id
        else:
            event = _found_event(db, article)
            article.event_id = event.id
            live.append(event)
            created += 1
    return created


def rebuild_events(db: Session) -> int:
    """Recluster every stored article from scratch (chronologically)."""
    db.execute(update(Article).values(event_id=None))
    db.execute(delete(Event))
    db.flush()
    all_events: list[Event] = []
    count = 0
    for article in db.scalars(select(Article).order_by(Article.published_at)):
        published = article.published_at or utcnow()
        horizon = published - timedelta(hours=WINDOW_HOURS)
        live = [e for e in all_events if e.updated_at >= horizon]
        match = _best_match(article, live)
        if match:
            _merge(match, article)
            article.event_id = match.id
        else:
            event = _found_event(db, article)
            article.event_id = event.id
            all_events.append(event)
            count += 1
    db.commit()
    return count
