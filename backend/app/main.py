"""Delphi — global news monitoring dashboard (FastAPI application)."""
from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete as sa_delete, func, or_, select
from sqlalchemy.orm import Session

from . import ingest, translate
from .boolean_query import validate_query
from .catalog import seed_demo_articles, seed_sources
from .clustering import assign_events, rebuild_events
from .database import Base, engine, get_db
from .events import broadcaster
from .geo import load_gazetteer
from .matching import query_articles
from .models import Alert, AlertEvent, Article, Event, Feed, Source, Translation, utcnow
from .schemas import AlertIn, FeedIn, QueryValidateIn, SocialTrackerIn, SourceIn, TopicTrackerIn
from .scoring import STANDARD_CATEGORIES, classify_categories

logging.basicConfig(level=logging.INFO)

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
DISABLE_INGEST = os.environ.get("NEWS_DISABLE_INGEST", "") == "1"


def _ensure_schema():
    """Additive migrations for databases created by earlier versions."""
    wanted = {
        "articles": {"event_id": "INTEGER"},
        "feeds": {"group_events": "BOOLEAN DEFAULT 0"},
        "sources": {"platform": "VARCHAR(20) DEFAULT 'news'"},
    }
    with engine.begin() as conn:
        for table, columns in wanted.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    _ensure_schema()
    db = next(get_db())
    try:
        seed_sources(db)
    finally:
        db.close()
    task = None
    if not DISABLE_INGEST:
        task = asyncio.create_task(ingest.ingest_loop())
    yield
    if task:
        task.cancel()


app = FastAPI(title="Delphi", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


def user_id_header(x_user_id: str = Header(default="default")) -> str:
    return (x_user_id or "default")[:64]


def _article_json(a: Article, tr: dict | None = None) -> dict:
    """Serialize an article; `tr` is a {article_id: {title, summary}} map of
    translations into the requester's language."""
    t = (tr or {}).get(a.id)
    return {
        "id": a.id,
        "event_id": a.event_id,
        "title": t["title"] if t else a.title,
        "summary": (t["summary"] if t else a.summary)[:400],
        "translated_from": a.language if t else None,
        "url": a.url,
        "image_url": a.image_url,
        "published_at": a.published_at.isoformat() + "Z" if a.published_at else None,
        "language": a.language,
        "country": a.country,
        "categories": a.categories or [],
        "places": a.places or [],
        "importance": a.importance,
        "source": {
            "id": a.source.id, "name": a.source.name, "country": a.source.country,
            "scope": a.source.scope,
        } if a.source else None,
    }


# ---------- meta ----------

@app.get("/api/meta")
def meta(db: Session = Depends(get_db)):
    gaz = load_gazetteer()
    total_articles = db.scalar(select(func.count(Article.id))) or 0
    day_ago = utcnow().replace(microsecond=0)
    from datetime import timedelta
    articles_24h = db.scalar(
        select(func.count(Article.id)).where(Article.fetched_at >= day_ago - timedelta(hours=24))
    ) or 0
    countries_24h = db.scalar(
        select(func.count(func.distinct(Article.country))).where(
            Article.fetched_at >= day_ago - timedelta(hours=24), Article.country != ""
        )
    ) or 0
    sources_ok = db.scalar(
        select(func.count(Source.id)).where(Source.enabled.is_(True), Source.last_status == "ok")
    ) or 0
    sources_total = db.scalar(select(func.count(Source.id)).where(Source.enabled.is_(True))) or 0
    return {
        "categories": STANDARD_CATEGORIES,
        "scopes": ["local", "national", "international"],
        "platforms": ["news", "reddit", "mastodon", "bluesky", "youtube"],
        "countries": [
            {"iso2": c["iso2"], "name": c["name"]}
            for c in sorted(gaz["countries"], key=lambda c: c["name"])
        ],
        "languages": sorted({s.language for s in db.scalars(select(Source)).all()} | {"en"}),
        "ui_languages": translate.UI_LANGUAGES,
        "translation": {"provider": translate.PROVIDER, "enabled": translate.enabled()},
        "stats": {
            "total_articles": total_articles,
            "articles_24h": articles_24h,
            "countries_24h": countries_24h,
            "sources_ok": sources_ok,
            "sources_total": sources_total,
        },
        "ingest": ingest.status,
    }


# ---------- sources ----------

@app.get("/api/sources")
def list_sources(db: Session = Depends(get_db)):
    sources = db.scalars(select(Source).order_by(Source.name)).all()
    return [{
        "id": s.id, "name": s.name, "rss_url": s.rss_url, "homepage": s.homepage,
        "country": s.country, "region": s.region, "language": s.language,
        "scope": s.scope, "categories": s.categories or [], "tier": s.tier,
        "platform": s.platform or "news",
        "enabled": s.enabled, "added_by": s.added_by,
        "last_fetched_at": s.last_fetched_at.isoformat() + "Z" if s.last_fetched_at else None,
        "last_status": s.last_status, "last_article_count": s.last_article_count,
    } for s in sources]


@app.post("/api/sources", status_code=201)
def create_source(body: SourceIn, db: Session = Depends(get_db)):
    if db.scalar(select(Source).where(Source.rss_url == body.rss_url)):
        raise HTTPException(409, "A source with this RSS URL already exists")
    source = Source(**body.model_dump(), added_by="user")
    db.add(source)
    db.commit()
    return {"id": source.id}


@app.post("/api/sources/topic-tracker", status_code=201)
def create_topic_tracker(body: TopicTrackerIn, db: Session = Depends(get_db)):
    """Track any topic worldwide via a Google News search RSS virtual source."""
    q = body.query.strip()
    if not q:
        raise HTTPException(422, "Query must not be empty")
    lang, country = body.language, body.country.upper()
    rss = (
        "https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
        + f"&hl={lang}-{country}&gl={country}&ceid={country}:{lang}"
    )
    if db.scalar(select(Source).where(Source.rss_url == rss)):
        raise HTTPException(409, "This topic is already being tracked")
    source = Source(
        name=f"Topic: {q}", rss_url=rss, homepage="https://news.google.com",
        country="", region="Global", language=lang, scope="international",
        categories=[], tier=2, added_by="topic-tracker",
    )
    db.add(source)
    db.commit()
    return {"id": source.id, "rss_url": rss}


@app.patch("/api/sources/{source_id}")
def update_source(source_id: int, body: dict, db: Session = Depends(get_db)):
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    new_url = (body.get("rss_url") or "").strip()
    if new_url and new_url != source.rss_url:
        if db.scalar(select(Source).where(Source.rss_url == new_url)):
            raise HTTPException(409, "Another source already uses this RSS URL")
        source.rss_url = new_url
        source.last_status = ""  # health unknown until next poll
    for key in ("name", "enabled", "country", "language", "scope",
                "categories", "tier", "platform", "homepage", "region"):
        if key in body:
            setattr(source, key, body[key])
    db.commit()
    return {"ok": True}


@app.post("/api/sources/social-tracker", status_code=201)
def create_social_tracker(body: SocialTrackerIn, db: Session = Depends(get_db)):
    """Track a topic across social platforms with open feeds: a Reddit search
    feed and a Mastodon hashtag feed. (X/Facebook/Instagram expose no open
    feeds, so they cannot be ingested here.)"""
    q = body.query.strip()
    if not q:
        raise HTTPException(422, "Query must not be empty")
    tag = "".join(ch for ch in q.title() if ch.isalnum())
    candidates = [
        {"name": f"Reddit search: {q}", "platform": "reddit",
         "rss_url": "https://www.reddit.com/search.rss?q=" + urllib.parse.quote(q) + "&sort=new",
         "homepage": "https://www.reddit.com"},
        {"name": f"Mastodon #{tag}", "platform": "mastodon",
         "rss_url": f"https://mastodon.social/tags/{urllib.parse.quote(tag)}.rss",
         "homepage": "https://mastodon.social"},
    ]
    created = []
    for c in candidates:
        if db.scalar(select(Source).where(Source.rss_url == c["rss_url"])):
            continue
        source = Source(**c, country="", region="Social", language="en",
                        scope="international", categories=[], tier=3,
                        added_by="topic-tracker")
        db.add(source)
        created.append(c["name"])
    db.commit()
    if not created:
        raise HTTPException(409, "This topic is already tracked on social platforms")
    return {"created": created}


@app.delete("/api/sources/{source_id}", status_code=204)
def delete_source(source_id: int, db: Session = Depends(get_db)):
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    db.delete(source)
    db.commit()


# ---------- articles / search ----------

@app.post("/api/articles/search")
async def search_articles(
    body: dict,
    sort: str = Query(default="newest"),
    limit: int = Query(default=50, le=200),
    lang: str = Query(default=""),
    db: Session = Depends(get_db),
):
    """Ad-hoc search with a criteria object (used for feed preview and search bar)."""
    articles = query_articles(db, body.get("criteria", body), sort=sort, limit=limit)
    tr = await translate.translate_articles(db, articles, lang)
    return [_article_json(a, tr) for a in articles]


@app.post("/api/query/validate")
def query_validate(body: QueryValidateIn):
    err = validate_query(body.query) if body.query.strip() else None
    return {"valid": err is None, "error": err}


# ---------- feeds ----------

@app.get("/api/feeds")
def list_feeds(user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    feeds = db.scalars(
        select(Feed).where(Feed.user_id == user_id).order_by(Feed.position, Feed.id)
    ).all()
    return [{
        "id": f.id, "name": f.name, "criteria": f.criteria, "sort": f.sort,
        "position": f.position, "width": f.width, "group_events": f.group_events,
    } for f in feeds]


@app.post("/api/feeds", status_code=201)
def create_feed(body: FeedIn, user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    _reject_invalid_query(body.criteria.query)
    max_pos = db.scalar(
        select(func.max(Feed.position)).where(Feed.user_id == user_id)
    )
    feed = Feed(
        user_id=user_id, name=body.name, criteria=body.criteria.model_dump(),
        sort=body.sort, position=(max_pos + 1) if max_pos is not None else 0,
        width=body.width, group_events=body.group_events,
    )
    db.add(feed)
    db.commit()
    return {"id": feed.id}


@app.put("/api/feeds/{feed_id}")
def update_feed(feed_id: int, body: FeedIn, user_id: str = Depends(user_id_header),
                db: Session = Depends(get_db)):
    feed = _owned(db, Feed, feed_id, user_id)
    _reject_invalid_query(body.criteria.query)
    feed.name = body.name
    feed.criteria = body.criteria.model_dump()
    feed.sort = body.sort
    feed.width = body.width
    feed.group_events = body.group_events
    db.commit()
    return {"ok": True}


@app.post("/api/feeds/reorder")
def reorder_feeds(body: dict, user_id: str = Depends(user_id_header),
                  db: Session = Depends(get_db)):
    order: list[int] = body.get("order", [])
    feeds = {f.id: f for f in db.scalars(select(Feed).where(Feed.user_id == user_id))}
    for pos, fid in enumerate(order):
        if fid in feeds:
            feeds[fid].position = pos
    db.commit()
    return {"ok": True}


@app.delete("/api/feeds/{feed_id}", status_code=204)
def delete_feed(feed_id: int, user_id: str = Depends(user_id_header),
                db: Session = Depends(get_db)):
    feed = _owned(db, Feed, feed_id, user_id)
    db.delete(feed)
    db.commit()


@app.get("/api/feeds/{feed_id}/articles")
async def feed_articles(feed_id: int, limit: int = Query(default=40, le=200),
                        lang: str = Query(default=""),
                        user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    feed = _owned(db, Feed, feed_id, user_id)
    articles = query_articles(db, feed.criteria, sort=feed.sort, limit=limit)
    tr = await translate.translate_articles(db, articles, lang)
    return [_article_json(a, tr) for a in articles]


@app.get("/api/feeds/{feed_id}/events")
async def feed_events(feed_id: int, limit: int = Query(default=30, le=100),
                      lang: str = Query(default=""),
                      user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    """Feed contents clustered into events. Groups keep the feed's sort order
    (order of each event's first matching article); articles without an event
    form singleton groups."""
    feed = _owned(db, Feed, feed_id, user_id)
    articles = query_articles(db, feed.criteria, sort=feed.sort, limit=200)
    groups: dict = {}
    order: list = []
    for a in articles:
        key = a.event_id if a.event_id is not None else f"solo-{a.id}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(a)
    order = order[:limit]
    shown = [a for key in order for a in groups[key][:6]]
    tr = await translate.translate_articles(db, shown, lang)

    out = []
    for key in order:
        members = groups[key]
        event = members[0].event if members[0].event_id else None
        out.append({
            "event_id": members[0].event_id,
            "matched_count": len(members),
            "total_count": event.article_count if event else len(members),
            "source_count": len({a.source_id for a in members}),
            "importance": event.importance if event else max(a.importance for a in members),
            "first_seen": event.first_seen.isoformat() + "Z" if event else None,
            "articles": [_article_json(a, tr) for a in members[:6]],
        })
    return out


# ---------- alerts ----------

@app.get("/api/alerts")
def list_alerts(user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    alerts = db.scalars(
        select(Alert).where(Alert.user_id == user_id).order_by(Alert.id)
    ).all()
    out = []
    for a in alerts:
        unseen = db.scalar(
            select(func.count(AlertEvent.id)).where(
                AlertEvent.alert_id == a.id, AlertEvent.seen.is_(False)
            )
        ) or 0
        out.append({
            "id": a.id, "name": a.name, "criteria": a.criteria, "active": a.active,
            "last_triggered_at": a.last_triggered_at.isoformat() + "Z" if a.last_triggered_at else None,
            "unseen": unseen,
        })
    return out


@app.post("/api/alerts", status_code=201)
def create_alert(body: AlertIn, user_id: str = Depends(user_id_header),
                 db: Session = Depends(get_db)):
    _reject_invalid_query(body.criteria.query)
    alert = Alert(user_id=user_id, name=body.name,
                  criteria=body.criteria.model_dump(), active=body.active)
    db.add(alert)
    db.commit()
    return {"id": alert.id}


@app.put("/api/alerts/{alert_id}")
def update_alert(alert_id: int, body: AlertIn, user_id: str = Depends(user_id_header),
                 db: Session = Depends(get_db)):
    alert = _owned(db, Alert, alert_id, user_id)
    _reject_invalid_query(body.criteria.query)
    alert.name = body.name
    alert.criteria = body.criteria.model_dump()
    alert.active = body.active
    db.commit()
    return {"ok": True}


@app.delete("/api/alerts/{alert_id}", status_code=204)
def delete_alert(alert_id: int, user_id: str = Depends(user_id_header),
                 db: Session = Depends(get_db)):
    alert = _owned(db, Alert, alert_id, user_id)
    db.delete(alert)
    db.commit()


@app.get("/api/alerts/{alert_id}/events")
async def alert_events(alert_id: int, limit: int = Query(default=50, le=200),
                       lang: str = Query(default=""),
                       user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    _owned(db, Alert, alert_id, user_id)
    events = db.scalars(
        select(AlertEvent).where(AlertEvent.alert_id == alert_id)
        .order_by(AlertEvent.created_at.desc()).limit(limit)
    ).all()
    articles = {a.id: a for a in
                (db.get(Article, e.article_id) for e in events) if a}
    tr = await translate.translate_articles(db, list(articles.values()), lang)
    out = []
    for e in events:
        article = articles.get(e.article_id)
        if article:
            out.append({"event_id": e.id, "seen": e.seen,
                        "created_at": e.created_at.isoformat() + "Z",
                        "article": _article_json(article, tr)})
    return out


@app.post("/api/alerts/{alert_id}/mark-seen")
def mark_alert_seen(alert_id: int, user_id: str = Depends(user_id_header),
                    db: Session = Depends(get_db)):
    _owned(db, Alert, alert_id, user_id)
    for e in db.scalars(select(AlertEvent).where(
            AlertEvent.alert_id == alert_id, AlertEvent.seen.is_(False))):
        e.seen = True
    db.commit()
    return {"ok": True}


# ---------- ingest control & live stream ----------

@app.post("/api/ingest/run")
async def ingest_run():
    if ingest.cycle_lock.locked():
        raise HTTPException(
            409, "A poll cycle is already running — new articles will appear when it finishes.")
    try:
        return await ingest.run_ingest_cycle()
    except Exception as exc:
        ingest.status["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
        raise HTTPException(500, f"Ingest cycle failed: {type(exc).__name__}: {exc}")


@app.get("/api/ingest/status")
def ingest_status():
    return ingest.status


@app.post("/api/demo/seed")
def demo_seed(db: Session = Depends(get_db)):
    added = seed_demo_articles(db)
    unclustered = db.scalars(select(Article).where(Article.event_id.is_(None))).all()
    assign_events(db, unclustered)
    db.commit()
    return {"added": added}


@app.post("/api/demo/purge")
def demo_purge(db: Session = Depends(get_db)):
    """Remove every trace of demo/sample data: seeded articles (example.org
    URLs), local test sources, their alert hits and cached translations, and
    any events left with no articles."""
    demo_sources = db.scalars(select(Source).where(or_(
        Source.rss_url.like("http://127.0.0.1%"),
        Source.rss_url.like("http://localhost%"),
        Source.rss_url.like("https://example.org%"),
    ))).all()

    conds = [Article.url.like("https://example.org/demo/%")]
    if demo_sources:
        conds.append(Article.source_id.in_([s.id for s in demo_sources]))
    article_ids = list(db.scalars(select(Article.id).where(or_(*conds))))

    removed = {"articles": len(article_ids), "sources": len(demo_sources), "events": 0}
    if article_ids:
        db.execute(sa_delete(AlertEvent).where(AlertEvent.article_id.in_(article_ids)))
        db.execute(sa_delete(Translation).where(Translation.article_id.in_(article_ids)))
        db.execute(sa_delete(Article).where(Article.id.in_(article_ids)))
    for source in demo_sources:
        db.delete(source)
    db.flush()

    used_events = select(Article.event_id).where(Article.event_id.is_not(None))
    removed["events"] = db.execute(
        sa_delete(Event).where(Event.id.not_in(used_events))
    ).rowcount
    # Demo articles may have clustered into surviving events; fix their counts.
    counts = dict(db.execute(
        select(Article.event_id, func.count()).where(Article.event_id.is_not(None))
        .group_by(Article.event_id)
    ).all())
    for event in db.scalars(select(Event)):
        event.article_count = counts.get(event.id, 0)
    db.commit()
    return removed


@app.post("/api/maintenance/reclassify")
def reclassify_articles(db: Session = Depends(get_db)):
    """Re-run the category classifier over every stored article (use after
    classifier upgrades so old articles match category feeds too)."""
    changed = total = 0
    for article in db.scalars(select(Article)):
        total += 1
        source_cats = article.source.categories if article.source else []
        new = classify_categories(f"{article.title}\n{article.summary}", source_cats)
        if new != (article.categories or []):
            article.categories = new
            changed += 1
    db.commit()
    return {"articles": total, "reclassified": changed}


@app.post("/api/events/rebuild")
def events_rebuild(db: Session = Depends(get_db)):
    """Recluster every stored article from scratch."""
    return {"events": rebuild_events(db)}


@app.get("/api/events/{event_id}")
async def event_detail(event_id: int, lang: str = Query(default=""),
                       db: Session = Depends(get_db)):
    """One event with its full article timeline (newest first)."""
    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    articles = db.scalars(
        select(Article).where(Article.event_id == event_id)
        .order_by(Article.published_at.desc()).limit(100)
    ).all()
    tr = await translate.translate_articles(db, articles, lang)
    return {
        "id": event.id,
        "title": event.title,
        "importance": event.importance,
        "article_count": event.article_count,
        "countries": event.countries or [],
        "categories": event.categories or [],
        "first_seen": event.first_seen.isoformat() + "Z",
        "updated_at": event.updated_at.isoformat() + "Z",
        "articles": [_article_json(a, tr) for a in articles],
    }


@app.get("/api/stream")
async def stream():
    """Server-Sent Events: new-article batches and alert hits, pushed live."""
    queue = broadcaster.subscribe()

    async def gen():
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            broadcaster.unsubscribe(queue)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


# ---------- helpers & static frontend ----------

def _owned(db: Session, model, obj_id: int, user_id: str):
    obj = db.get(model, obj_id)
    if not obj or obj.user_id != user_id:
        raise HTTPException(404, f"{model.__name__} not found")
    return obj


def _reject_invalid_query(query: str):
    if query and query.strip():
        err = validate_query(query)
        if err:
            raise HTTPException(422, f"Invalid boolean query: {err}")


# Unknown /api/* paths get a clear JSON 404 (registered after all real API
# routes). Without this they fall through to the static mount, which answers
# POSTs with a baffling 405 "Method Not Allowed" — typically seen when the
# client calls an endpoint newer than the running server.
@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def api_fallback(path: str):
    raise HTTPException(
        404, f"Unknown API endpoint: /api/{path}. If this endpoint should exist, "
             "the server may be running an older version — git pull and restart.")


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
