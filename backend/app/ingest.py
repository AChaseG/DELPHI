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
from datetime import datetime, timedelta

import feedparser
import httpx
from sqlalchemy import select

from .clustering import assign_events
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
USER_AGENT = "GlobalNewsDashboard/1.0 (+RSS reader; respects robots and publisher feeds)"

status: dict = {"running": False, "last_run": None, "last_new_articles": 0, "cycles": 0}

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
    try:
        resp = await client.get(source.rss_url, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            return source, [], f"parse error: {parsed.get('bozo_exception', 'unknown')}"
        return source, parsed.entries, "ok"
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


def process_entries(db, source: Source, entries: list, recent_clusters: list) -> list[Article]:
    """Normalize feed entries into Article rows; returns newly inserted articles."""
    new_articles: list[Article] = []
    existing_urls = {
        u for (u,) in db.execute(
            select(Article.url).where(Article.source_id == source.id)
        ).all()
    }

    for entry in entries[:80]:
        url = (entry.get("link") or "").strip()
        title = _strip_html(entry.get("title", "")).strip()
        if not url or not title or url in existing_urls:
            continue
        existing_urls.add(url)

        summary = _strip_html(entry.get("summary", ""))[:2000]
        text = f"{title}\n{summary}"
        places = extract_places(text)
        country = places[0]["country"] if places else source.country
        tokens = cluster_tokens(title)
        corroborating = _corroboration(source.id, tokens, recent_clusters)

        article = Article(
            source_id=source.id,
            guid=(entry.get("id") or "")[:500],
            url=url[:1000],
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
                    "article_id": article.id,
                    "title": article.title,
                    "url": article.url,
                    "importance": article.importance,
                    "country": article.country,
                })
    return hits


async def run_ingest_cycle() -> dict:
    """One full poll of all enabled sources. Returns cycle stats."""
    db = SessionLocal()
    try:
        sources = db.scalars(select(Source).where(Source.enabled.is_(True))).all()
        recent = _recent_clusters(db)

        sem = asyncio.Semaphore(CONCURRENCY)
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
            async def bounded(source):
                async with sem:
                    return await fetch_source(client, source)
            results = await asyncio.gather(*(bounded(s) for s in sources))

        all_new: list[Article] = []
        ok_sources = 0
        for source, entries, fetch_status in results:
            source.last_fetched_at = utcnow()
            source.last_status = fetch_status
            if fetch_status == "ok":
                ok_sources += 1
                new = process_entries(db, source, entries, recent)
                source.last_article_count = len(new)
                all_new.extend(new)
        db.commit()  # assign article ids before clustering / alert evaluation

        new_events = assign_events(db, all_new)
        hits = evaluate_alerts(db, all_new)
        db.commit()

        status.update({
            "last_run": utcnow().isoformat(),
            "last_new_events": new_events,
            "last_new_articles": len(all_new),
            "sources_ok": ok_sources,
            "sources_total": len(sources),
            "cycles": status.get("cycles", 0) + 1,
        })
        if all_new:
            broadcaster.publish({"type": "articles", "count": len(all_new)})
        for hit in hits:
            broadcaster.publish({"type": "alert", **hit})
        log.info("ingest cycle: %d/%d sources ok, %d new articles, %d alert hits",
                 ok_sources, len(sources), len(all_new), len(hits))
        return {"new_articles": len(all_new), "new_events": new_events,
                "alert_hits": len(hits),
                "sources_ok": ok_sources, "sources_total": len(sources)}
    finally:
        db.close()


async def ingest_loop():
    status["running"] = True
    while True:
        try:
            await run_ingest_cycle()
        except Exception:
            log.exception("ingest cycle failed")
        await asyncio.sleep(FETCH_INTERVAL)
