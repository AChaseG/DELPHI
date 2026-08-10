"""D.E.L.P.H.I. — Digital Exploration and Layout for Publicly Harvested Intelligence."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import urllib.parse
import uuid

import httpx
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (BackgroundTasks, Depends, FastAPI, Header, HTTPException,
                     Query, Request)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import (FileResponse, JSONResponse, Response,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete as sa_delete, func, or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

import re as _username_re

from . import (accounts_backup, auth, devices, discovery, export, geocode, home,
               ingest, langdetect, mailer, passwords, ratelimit, repair,
               safefetch, storage, syndication, translate, watchdog)
from .boolean_query import normalize_quotes, query_advisories, validate_query
from .catalog import seed_city_sources, seed_sources
from .changelog import CHANGELOG, fingerprints, unseen_entries, updates_since
from .clustering import assign_events, rebuild_events
from .database import Base, SessionLocal, engine, get_db
from .events import broadcaster
from .geo import load_gazetteer, search_places
from .matching import explain_text_match, query_articles
from .models import (Alert, AlertEvent, Article, Device, DiscoveredDomain, Event,
                     FavoriteLocation, Feed,
                     Pantheon, PantheonInvite, PantheonMember, Source,
                     Translation, User, ViewedArticle, ViewedEvent, utcnow)
from .schemas import AlertIn, FeedIn, QueryValidateIn, SocialTrackerIn, SourceIn, TopicTrackerIn
from .scoring import STANDARD_CATEGORIES, classify_categories

logging.basicConfig(level=logging.INFO)

# Hand the interpreter over five times more often than the 5ms default.
#
# This machine is performance-1x: one *dedicated* core, against the two
# throttled ones it replaced. That was the right trade for throughput — a
# collection round went from 6m19s to 76s — but it means the thread scoring
# articles and the thread answering requests now share a single processor
# rather than having one each. Only one Python thread runs at a time, and the
# interpreter only offers the turn to another every switch interval, so a
# request can wait behind a burst of scoring for far longer than the work
# itself would suggest.
#
# Ingestion needs roughly 45s of processor per 76s round, so the core is not
# saturated; the problem is burstiness, and the health check allows 10s. A
# shorter interval trades a little throughput for the loop getting its turn
# sooner. It is a latency setting on a machine that has to answer while it
# works, and 1ms is still thousands of bytecodes.
sys.setswitchinterval(0.001)

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
DISABLE_INGEST = os.environ.get("NEWS_DISABLE_INGEST", "") == "1"

# How much of a story's body the focused view shows. Enough to judge the piece
# without it standing in for the outlet's page, which is one click away.
ARTICLE_EXCERPT_CHARS = 1200
STORY_TIMELINE_MAX = 100  # reports shown for one story, newest first



def _ensure_schema():
    """Additive migrations for databases created by earlier versions."""
    wanted = {
        "articles": {"event_id": "INTEGER", "content": "TEXT DEFAULT ''",
                     "content_tried_at": "DATETIME"},
        "feeds": {"group_events": "BOOLEAN DEFAULT 0",
                  "pantheon_id": "INTEGER",
                  "shared_by": "VARCHAR(32) DEFAULT ''"},
        "alerts": {"pantheon_id": "INTEGER",
                   "shared_by": "VARCHAR(32) DEFAULT ''",
                   "notify_email": "BOOLEAN DEFAULT 0",
                   "webhook_url": "VARCHAR(500) DEFAULT ''"},
        "sources": {"platform": "VARCHAR(20) DEFAULT 'news'",
                    "consecutive_failures": "INTEGER DEFAULT 0",
                    "repaired_from": "VARCHAR(500) DEFAULT ''",
                    "last_repair_at": "DATETIME",
                    "idle_polls": "INTEGER DEFAULT 0",
                    "paywall": "BOOLEAN DEFAULT 0",
                    "etag": "VARCHAR(200) DEFAULT ''",
                    "last_modified": "VARCHAR(80) DEFAULT ''",
                    "syndicate": "VARCHAR(40) DEFAULT ''"},
        "users": {"email": "VARCHAR(200) DEFAULT ''",
                  "email_verified": "BOOLEAN DEFAULT 0",
                  "last_seen_at": "DATETIME",
                  "changelog_seen": "TEXT",
                  "settings": "TEXT",
                  "is_admin": "BOOLEAN DEFAULT 0",
                  "disabled": "BOOLEAN DEFAULT 0",
                  "token_version": "INTEGER DEFAULT 0",
                  "device_limit": "INTEGER"},
        "discovered_domains": {"sightings": "INTEGER DEFAULT 0"},
        "favorite_locations": {"place_name": "VARCHAR(120) DEFAULT ''",
                               "country": "VARCHAR(2) DEFAULT ''",
                               "source_id": "INTEGER"},
    }
    with engine.begin() as conn:
        for table, columns in wanted.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        # Indexes create_all won't add to pre-existing tables (dashboard stats
        # filter on fetched_at every refresh).
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_articles_fetched_at ON articles (fetched_at)")
        _ensure_fts(conn)


def _ensure_fts(conn) -> None:
    """Full-text index over article title+summary+content, kept in sync by
    triggers, used to prefilter keyword/boolean searches (matching.py). New
    on an existing database → backfill the current rows once."""
    fresh = not conn.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='articles_fts'"
    ).fetchone()
    conn.exec_driver_sql(
        "CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5("
        "title, summary, content, content='articles', content_rowid='id', "
        "tokenize='unicode61 remove_diacritics 2')")
    # Triggers mirror every write into the index (external-content pattern).
    conn.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS articles_fts_ai AFTER INSERT ON articles BEGIN "
        "INSERT INTO articles_fts(rowid, title, summary, content) "
        "VALUES (new.id, new.title, new.summary, new.content); END")
    conn.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS articles_fts_ad AFTER DELETE ON articles BEGIN "
        "INSERT INTO articles_fts(articles_fts, rowid, title, summary, content) "
        "VALUES ('delete', old.id, old.title, old.summary, old.content); END")
    conn.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS articles_fts_au AFTER UPDATE ON articles BEGIN "
        "INSERT INTO articles_fts(articles_fts, rowid, title, summary, content) "
        "VALUES ('delete', old.id, old.title, old.summary, old.content); "
        "INSERT INTO articles_fts(rowid, title, summary, content) "
        "VALUES (new.id, new.title, new.summary, new.content); END")
    if fresh:
        conn.exec_driver_sql(
            "INSERT INTO articles_fts(rowid, title, summary, content) "
            "SELECT id, title, summary, content FROM articles")


def _purge_demo_data(db) -> int:
    """Delete any sample articles left over from earlier versions.

    Delphi no longer generates demo data, but instances seeded by an older
    build still carry it, and the button that used to clear it is gone. This
    runs at every start and is a no-op once the rows are gone. Scoped to the
    unmistakable marker — the example.org URLs the generator produced — so it
    can never touch real reporting.
    """
    ids = list(db.scalars(select(Article.id).where(
        Article.url.like("https://example.org/demo/%"))))
    demo_sources = db.scalars(select(Source).where(
        Source.rss_url.like("https://example.org%"))).all()
    if not ids and not demo_sources:
        return 0
    if ids:
        db.execute(sa_delete(AlertEvent).where(AlertEvent.article_id.in_(ids)))
        db.execute(sa_delete(Translation).where(Translation.article_id.in_(ids)))
        db.execute(sa_delete(Article).where(Article.id.in_(ids)))
    for source in demo_sources:
        db.delete(source)
    db.flush()
    # Events whose only articles were samples would otherwise linger empty.
    used = select(Article.event_id).where(Article.event_id.is_not(None))
    db.execute(sa_delete(Event).where(Event.id.not_in(used)))
    db.commit()
    logging.getLogger("catalog").info(
        "removed %d leftover sample articles and %d sample sources",
        len(ids), len(demo_sources))
    return len(ids)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    _ensure_schema()
    db = next(get_db())
    try:
        _purge_demo_data(db)
        merged = _consolidate_location_feeds(db)
        if merged:
            logging.getLogger("catalog").info(
                "merged per-location feeds into one for %d account(s)", merged)
        seed_sources(db)
        if os.environ.get("NEWS_SEED_CITIES", "1") != "0":
            added = seed_city_sources(db)
            if added:
                logging.getLogger("catalog").info("seeded %d city local-news sources", added)
    finally:
        db.close()
    tasks = []
    if not DISABLE_INGEST:
        # Home's shared columns, matched before anyone asks for them. Started
        # alongside the poller rather than awaited, so a cold start still serves
        # the sign-in page immediately; until it lands, columns are matched live.
        tasks.append(asyncio.create_task(ingest.warm_home()))
        tasks.append(asyncio.create_task(ingest.ingest_loop()))
    # Outside the ingest check on purpose. The loop can be starved by anything
    # — a slow request, a background job, this machine's single core — and a
    # watchdog that only runs when the poller does cannot say so.
    tasks.append(asyncio.create_task(watchdog.watch()))
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="D.E.L.P.H.I.", lifespan=lifespan)

# Delphi serves its own frontend from this same origin (see the static mount at
# the bottom of the file), so the browser needs no cross-origin permission for
# anything the app itself does — and it was being handed a blanket one.
#
# It was never the hole it looks like: sessions are Bearer tokens read from
# localStorage rather than cookies, so a hostile page could not ride one and
# cannot read another origin's storage. What "*" did buy was letting any site
# script sign-in and registration attempts from its visitors' browsers, which
# spreads them over the visitors' addresses rather than the attacker's. That is
# a rate-limit problem more than a CORS one, but there is no reason to leave
# the door open when nothing walks through it.
#
# So: no cross-origin access unless someone asks for it by name. Set
# NEWS_CORS_ORIGINS to a comma-separated list of origins if you are building a
# browser client of your own against this API.
CORS_ORIGINS = [o.strip().rstrip("/") for o
                in os.environ.get("NEWS_CORS_ORIGINS", "").split(",") if o.strip()]
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware, allow_origins=CORS_ORIGINS,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
# Everything Delphi sends is text — script, stylesheets, and JSON full of
# headlines — and none of it was compressed. Opening the dashboard moved 314 KB
# of script and markup and another 28 KB per column of articles; gzipped that
# is 93 KB and about 6 KB. On a phone away from wi-fi it is the difference
# between a board that arrives and one that is still arriving.
#
# Level 6 rather than the default 9: it saves 45.0 KB against 44.8 KB on the
# largest file and costs 5.6ms of a single core instead of 10.5ms, which on one
# small machine also serving the poller is the better trade. Live updates are
# exempt — Starlette leaves text/event-stream alone, so an alert still arrives
# the instant it fires rather than when a compression buffer fills.
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)


"""How long a request may take before the log says so, in seconds.

The dashboard gives up on a request after 30s and shows "Failed to load", and
until now nothing on the server said which request that was or how close to the
line the others were running. A single warning per slow request is enough to
tell an overloaded machine (everything slow) from an expensive feed (one path
slow), which are fixed in completely different places."""
SLOW_REQUEST_S = float(os.environ.get("NEWS_SLOW_REQUEST_S", "3"))
_slow_log = logging.getLogger("slow")
_fail_log = logging.getLogger("failure")

# The last few unhandled errors, newest first, so a reference quoted by a reader
# can be looked up after the log has scrolled past it. Deliberately small: this
# is for "what was that", not an error tracker.
MAX_RECENT_FAILURES = 20
recent_failures: deque[dict] = deque(maxlen=MAX_RECENT_FAILURES)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    """Turn a crash into something two different people can act on.

    An unhandled exception used to reach the browser as FastAPI's bare
    "Internal Server Error". The reader learned nothing they could report, and
    the operator had a traceback in the log with no way to tell which of the
    day's errors the reader was describing. Both now share a short reference:
    it goes in the log line beside the traceback, in the response body, and in
    a header, so "it broke, reference 4f2a1c" is enough to find the exact
    failure.

    The exception's own text never reaches the browser, because tracebacks name
    file paths and query shapes.

    The reference used to identify one moment in the log and nothing else. That
    was a mistake on this deployment: the log holds about a minute, so by the
    time a reference is reported it names a line nobody can still read, and
    three separate faults have now been chased without ever seeing the
    exception. The last few are kept in memory as well and served to operators
    through /api/ingest/status, so "reference bd0de7" is a question with an
    answer. Bounded, for the same reason the stall record is.
    """
    reference = uuid.uuid4().hex[:6]
    _fail_log.exception(
        "[%s] unhandled %s on %s %s%s (account %s)",
        reference, type(exc).__name__, request.method, request.url.path,
        f"?{request.url.query}" if request.url.query else "",
        getattr(request.state, "user_id", None) or "anonymous")
    recent_failures.appendleft({
        "reference": reference,
        "at": utcnow().isoformat() + "Z",
        "error": type(exc).__name__,
        # The message, not the traceback: enough to recognise the fault without
        # putting file paths anywhere but the log.
        "detail": str(exc)[:200],
        "method": request.method,
        "path": request.url.path,
    })
    return JSONResponse(
        status_code=500,
        headers={"X-Delphi-Error": reference},
        content={"detail": f"Delphi hit an unexpected error and has logged it "
                           f"(reference {reference}). Nothing was saved. If it "
                           f"keeps happening, quote that reference.",
                 "reference": reference},
    )


@app.exception_handler(OperationalError)
async def database_busy(request: Request, exc: OperationalError):
    """SQLite's "database is locked" is a wait, not a fault.

    One machine runs both the poller and the web server against one file. A
    write that outlasts the busy timeout surfaces here, and it means try again
    in a second — which is nothing like the crash the generic handler above
    describes, so it says so and asks for a retry instead of a bug report.
    """
    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
        return await unhandled_error(request, exc)
    reference = uuid.uuid4().hex[:6]
    _fail_log.warning("[%s] database busy on %s %s", reference,
                      request.method, request.url.path)
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "2", "X-Delphi-Error": reference},
        content={"detail": "The database was busy finishing another write. "
                           "Nothing was saved — try that again in a moment.",
                 "reference": reference},
    )


# Everything the page needs comes from Delphi itself — the scripts, the
# stylesheet, Leaflet (vendored rather than pulled from a CDN) — so the policy
# can be the strict one rather than the usual pile of exceptions. Two sources
# have to stay open: article thumbnails are hotlinked from whichever publisher
# ran the story, and the map fetches its tiles from OpenStreetMap. Both are
# images, so both live in img-src and nowhere else.
#
# There is no known injection hole for this to close today — every piece of
# feed text reaches the page through the DOM, never innerHTML. That is the
# reason to add it now rather than a reason not to: a policy is worth having
# before the mistake, and it costs one header.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data: https:",   # publisher thumbnails, map tiles
    "connect-src 'self'",            # geocoding and translation are proxied
    "font-src 'self'",
    "form-action 'self'",
    "base-uri 'none'",               # no rewriting where relative URLs resolve
    "object-src 'none'",
    "frame-ancestors 'none'",        # not embeddable; clickjacking has nothing to grab
])

# Fly terminates TLS and redirects http→https already; this stops the redirect
# itself being the weak point by telling the browser never to try http again.
# Ramped deliberately: HSTS cannot be withdrawn faster than the max-age already
# handed out, so it starts at a day. Raise it once you are sure of the
# certificate, and only add includeSubDomains/preload when every subdomain is
# ready to be https-only permanently. NEWS_HSTS_MAX_AGE=0 turns it off.
#
# Sent on every response rather than only the ones we believe arrived over TLS.
# We deliberately cannot tell: uvicorn runs with --no-proxy-headers so that
# X-Forwarded-* stays untrusted (see ratelimit.client_ip), which leaves the
# request's scheme reading "http" behind Fly's TLS termination. Conditioning on
# it would therefore mean never sending this in production. It costs nothing to
# send unconditionally, because the spec requires browsers to ignore HSTS
# received over plain http — exactly the case we cannot identify.
HSTS_MAX_AGE = int(os.environ.get("NEWS_HSTS_MAX_AGE", "86400"))


@app.middleware("http")
async def time_requests(request: Request, call_next):
    started = time.monotonic()
    response = await call_next(request)
    took = time.monotonic() - started
    # Readable in the browser's network panel under Timing, so a user can see
    # whether the server was slow or the network was.
    response.headers["Server-Timing"] = f"app;dur={took * 1000:.0f}"
    if took >= SLOW_REQUEST_S and request.url.path.startswith("/api/"):
        _slow_log.warning("%.1fs %s %s%s", took, request.method, request.url.path,
                          f"?{request.url.query}" if request.url.query else "")
    return response


@app.middleware("http")
async def require_account(request: Request, call_next):
    """An account is required to use the system: every API route except
    /api/auth/* demands a valid session token. Static assets stay public so
    the sign-in page itself can load.

    The session token is only ever read from the Authorization header. It used
    to be accepted as ?token= as well, for the event stream — EventSource
    cannot set headers — but a query parameter is written into the access log
    of every proxy it passes, so that put a thirty-day credential into log
    files on every page load. The stream now presents a one-minute ticket
    instead (see /api/stream/ticket), which is the only thing this reads from
    the URL and opens nothing else.

    A signature is not sufficient on its own, so the account behind the token
    is re-read here on every request. That catches three things a token cannot
    know about: the account was deleted, the account was suspended, or its
    sessions were ended (by a password reset, or by signing out everywhere) —
    the last of which is a `token_version` the token no longer agrees with.
    Without this a token would keep working for weeks past any of them. It is a
    primary-key lookup, and it sits in the middleware so it covers routes that
    take no user_id dependency too.
    """
    path = request.url.path
    if path.startswith("/api") and not path.startswith("/api/auth/"):
        if path == "/api/stream":
            uid = auth.parse_stream_ticket(request.query_params.get("ticket", ""))
            claim = auth.Claim(uid, -1) if uid is not None else None
        else:
            authz = request.headers.get("authorization", "")
            token = authz[7:].strip() if authz.startswith("Bearer ") else ""
            claim = auth.parse_token(token) if token else None
        if claim is None:
            return JSONResponse({"detail": "Authentication required — sign in"}, status_code=401)
        uid = claim.user_id
        with SessionLocal() as session:
            user = session.get(User, uid)
            if user is None:
                return JSONResponse(
                    {"detail": "That account no longer exists — it was deleted while "
                               "you were signed in. Create a new one to continue."},
                    status_code=401)
            if user.disabled:
                return JSONResponse(
                    {"detail": "This account has been suspended by an operator. "
                               "Your feeds and alerts are intact; an operator can "
                               "restore access from the operator console."},
                    status_code=403)
            # A ticket is minted from a session that was checked moments ago and
            # lives for a minute, so it carries no version of its own (-1).
            if claim.token_version >= 0 and claim.token_version != user.token_version:
                return JSONResponse(
                    {"detail": "You were signed out of this device because the "
                               "account's password was changed or someone signed "
                               "it out everywhere. Sign in again to continue."},
                    status_code=401)
            # Named in the failure log, so a crash can be traced to a person
            # who reported it rather than to an anonymous request.
            request.state.user_id = f"acct:{uid}"

            # Which device this is, and whether the account may be in use on
            # it. Here rather than in a dependency for the same reason as the
            # account re-read above: it has to cover every route, including
            # those that take no user_id.
            #
            # A browser that sends no key is not blocked. The header comes from
            # our own script, so anything without one is a curl, a script, or
            # an older cached copy of the app, and refusing those would be a
            # lockout dressed up as a limit. It is not counted either, which is
            # the honest trade: this measures browsers, not API clients.
            key = request.headers.get("x-delphi-device", "").strip()
            if devices.valid_key(key):
                over = devices.would_exceed(session, user, key)
                if over:
                    return JSONResponse(
                        {"detail": f"This account is already in use on {over} "
                                   f"{'device' if over == 1 else 'devices'}, which is its "
                                   "limit. Stop using it on one of them and try again in "
                                   "a few minutes, or have a sign-out link emailed to you "
                                   "to clear them all at once.",
                         "code": "device_limit", "limit": over},
                        status_code=403)
                devices.touch(session, uid, key, request.headers.get("user-agent", ""))
    return await call_next(request)


# Defined last on purpose. Starlette wraps the most recently added middleware
# outermost, and this has to be outside require_account: that one answers 401
# and 403 by returning a response itself rather than calling through, so
# anything inner never runs for those. Declared earlier, the sign-in page — the
# one page an unauthenticated browser actually renders — was served with no
# policy at all.
@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Tell the browser what this page is allowed to do."""
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", CSP)
    # Stop a .txt or a JSON body being re-read as script because it looks like
    # one; the browser must believe the Content-Type we sent.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # A story URL can name what somebody is reading. Send the origin to other
    # sites, never the path.
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # Nothing here uses these, and the quietest way to keep it that way is to
    # say so rather than to rely on nobody adding a call.
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    if HSTS_MAX_AGE > 0:
        response.headers.setdefault(
            "Strict-Transport-Security", f"max-age={HSTS_MAX_AGE}")
    return response


def user_id_header(authorization: str = Header(default="")) -> str:
    """Resolve the caller's account key ("acct:<id>") from the Bearer token.

    The middleware guarantees one is present on protected routes; this is
    defense in depth. It no longer falls back to ?token=: the session token
    does not travel in URLs at all now, and leaving the fallback in would have
    kept every route quietly willing to accept one.
    """
    raw = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    claim = auth.parse_token(raw) if raw else None
    if claim is None:
        raise HTTPException(401, "Authentication required — sign in")
    return f"acct:{claim.user_id}"


def _events_for(db: Session, articles: list[Article]) -> dict[int, Event]:
    """The events behind a page of articles, in one query.

    Reading `article.event` per row is a lazy load each time — up to one query
    per article on a page of forty, and the reason a feed that opted into
    staleness hiding cost so much more than one that didn't."""
    ids = {a.event_id for a in articles if a.event_id is not None}
    if not ids:
        return {}
    return {e.id: e for e in db.scalars(select(Event).where(Event.id.in_(ids)))}


def _viewed_events(db: Session, user_id: str, articles: list[Article]) -> set[int]:
    """Event ids among these articles that the user has opened in Event Focus."""
    event_ids = {a.event_id for a in articles if a.event_id is not None}
    if not user_id or not event_ids:
        return set()
    return set(db.scalars(select(ViewedEvent.event_id).where(
        ViewedEvent.user_id == user_id, ViewedEvent.event_id.in_(event_ids))))


def _viewed_articles(db: Session, user_id: str, articles: list[Article]) -> set[int]:
    """Ids of the event-less articles among these that the user has read.

    Only asked about articles with no event: everything else is remembered by
    its story, which is what dims a report the reader has already seen no
    matter which column it turns up in."""
    ids = {a.id for a in articles if a.event_id is None}
    if not user_id or not ids:
        return set()
    return set(db.scalars(select(ViewedArticle.article_id).where(
        ViewedArticle.user_id == user_id, ViewedArticle.article_id.in_(ids))))


def _article_json(a: Article, tr: dict | None = None,
                  viewed: set[int] | None = None,
                  event_updated: dict[int, Event] | None = None,
                  viewed_articles: set[int] | None = None) -> dict:
    """Serialize an article; `tr` is a {article_id: {title, summary}} map of
    translations into the requester's language.

    Which favourite locations an article falls inside is deliberately *not*
    computed here. It is pure geometry over `places` and the reader's own saved
    locations, both of which the browser already holds, so the browser does it —
    that removes two queries and a per-article geometry pass from every feed
    request, and it means grouped feeds get the badges too, which they never did
    while this was server-side."""
    t = (tr or {}).get(a.id)
    event = (event_updated or {}).get(a.event_id)
    return {
        "id": a.id,
        "event_id": a.event_id,
        # Read means read, whether it was remembered as a story or — for an
        # article that never got one — on its own.
        "viewed": (a.event_id in (viewed or set()) if a.event_id is not None
                   else a.id in (viewed_articles or set())),
        # Only sent for feeds that hide stale events; the client applies the
        # threshold, which is a setting it owns.
        **({"event_updated_at": event.updated_at.isoformat() + "Z"} if event else {}),
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
        # Paywalled outlet: hand readers an archive.ph link to the full text.
        "paywall": bool(a.source and a.source.paywall),
        "archive_url": (f"https://archive.ph/newest/{a.url}"
                        if a.source and a.source.paywall and a.url.startswith("http") else None),
        "source": {
            "id": a.source.id, "name": a.source.name, "country": a.source.country,
            "scope": a.source.scope,
        } if a.source else None,
    }


# ---------- meta ----------

# The headline numbers on the dashboard: four aggregates, one of them a count
# of every article ever stored. They are read on every page load and they are
# the same for everybody, and while the poller has the machine busy this was
# measured at 31.8s live — past the browser's 30s patience, so the dashboard
# reported the server as unreachable when it was merely occupied. They describe
# a catalog that changes over minutes, so a minute-old answer is the same
# answer; recomputing it per reader is the only thing that ever made it slow.
_STATS_CACHE: tuple[float, dict] | None = None
STATS_CACHE_TTL = 60.0


def _invalidate_stats_cache() -> None:
    global _STATS_CACHE
    _STATS_CACHE = None


def _stats(db) -> dict:
    global _STATS_CACHE
    if _STATS_CACHE and time.monotonic() - _STATS_CACHE[0] < STATS_CACHE_TTL:
        return _STATS_CACHE[1]
    from datetime import timedelta
    day_ago = utcnow().replace(microsecond=0) - timedelta(hours=24)
    stats = {
        "total_articles": db.scalar(select(func.count(Article.id))) or 0,
        "articles_24h": db.scalar(
            select(func.count(Article.id)).where(Article.fetched_at >= day_ago)) or 0,
        "countries_24h": db.scalar(
            select(func.count(func.distinct(Article.country))).where(
                Article.fetched_at >= day_ago, Article.country != "")) or 0,
        "sources_ok": db.scalar(
            select(func.count(Source.id)).where(Source.enabled.is_(True),
                                                Source.last_status.startswith("ok"))) or 0,
        "sources_total": db.scalar(
            select(func.count(Source.id)).where(Source.enabled.is_(True))) or 0,
    }
    _STATS_CACHE = (time.monotonic(), stats)
    return stats


@app.get("/api/meta")
def meta(user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    gaz = load_gazetteer()
    me_user = db.get(User, _acct_id(user_id))
    return {
        "categories": STANDARD_CATEGORIES,
        "scopes": ["local", "national", "international"],
        "platforms": ["news", "reddit", "mastodon", "bluesky", "youtube"],
        # Coordinates ride along so the browser can do the country-centroid
        # fallback when it decides which favourite locations an article is near.
        "countries": [
            {"iso2": c["iso2"], "name": c["name"], "lat": c["lat"], "lon": c["lon"]}
            for c in sorted(gaz["countries"], key=lambda c: c["name"])
        ],
        "languages": sorted(set(db.scalars(select(func.distinct(Source.language)))) | {"en"}),
        "ui_languages": translate.UI_LANGUAGES,
        "translation": {"provider": translate.PROVIDER, "enabled": translate.enabled()},
        "stats": _stats(db),
        # A snapshot, not the poller's own dict. `ingest.status` starts with
        # four keys and grows to twenty as a cycle reports what it did, so
        # handing the live object to the response serializer means iterating a
        # dict another task is adding keys to — which raises "dictionary
        # changed size during iteration" and turns a page load into a 500. Rare
        # enough to look random, and likelier exactly when the machine is busy,
        # because that is when requests queue into the moment a cycle finishes.
        # /api/ingest/status already copied it; this one did not.
        "ingest": dict(ingest.status),
        "is_admin": bool(me_user and _is_admin(me_user)),
    }


# ---------- accounts ----------

_USERNAME_RE = _username_re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")
_EMAIL_RE = _username_re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

# Config-designated operators: usernames and/or emails listed in NEWS_ADMIN_USERS
# (comma-separated) are admins the moment they register — no password is baked
# into the code, and losing the DB can never lock the operator out. Matched
# case-insensitively against both username and email.
_ADMIN_HANDLES = frozenset(
    h.strip().lower() for h in os.environ.get("NEWS_ADMIN_USERS", "").split(",") if h.strip()
)


def _is_configured_admin(user: User) -> bool:
    return bool(_ADMIN_HANDLES) and (
        (user.username or "").lower() in _ADMIN_HANDLES
        or (user.email or "").lower() in _ADMIN_HANDLES
    )


def _is_admin(user: User) -> bool:
    """Effective admin: the persisted flag OR a NEWS_ADMIN_USERS designation."""
    return bool(user.is_admin) or _is_configured_admin(user)


def require_admin(user_id: str = Depends(user_id_header),
                  db: Session = Depends(get_db)) -> User:
    """Dependency for /api/admin/* — resolves the caller and demands admin.
    Config-designated operators are auto-promoted on first admin use so the DB
    reflects reality (and the last-admin guards see them)."""
    user = db.get(User, _acct_id(user_id))
    if user is None:
        raise HTTPException(401, "Account no longer exists")
    if user.disabled:
        raise HTTPException(403, "This account has been suspended by an operator")
    if not _is_admin(user):
        raise HTTPException(403, "Operator access required")
    if not user.is_admin and _is_configured_admin(user):
        user.is_admin = True
        db.commit()
    return user


def _action_base_url(request: Request) -> str:
    """Trusted origin for emailed action links (verification, password reset).

    The Host / X-Forwarded-Host headers are attacker-controlled, so trusting
    them lets someone request a victim's password reset while spoofing the
    host and receive an email whose token points at their own domain. To close
    that, set NEWS_PUBLIC_URL (e.g. https://delphi.example.com) — it always
    wins. NEWS_ALLOWED_HOSTS (comma-separated) instead allowlists hosts and
    rejects anything else. Only when neither is configured do we fall back to
    the request host, for zero-config local/Codespaces use where email links
    are typically disabled anyway."""
    public = os.environ.get("NEWS_PUBLIC_URL", "").strip().rstrip("/")
    if public:
        return public
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "localhost"))
    host = host.split(",")[0].strip()  # first hop only if a proxy chained them
    allowed = [h.strip() for h in os.environ.get("NEWS_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if allowed and host not in allowed:
        host = allowed[0]  # ignore the spoofed header; use a known-good host
    return f"{proto}://{host}"


def _action_link(request: Request, param: str, token: str) -> str:
    """Absolute link to the app carrying an action token, as a path segment.

    Path rather than query string or fragment, because mail systems rewrite
    links and only the path reliably survives. A query string is dropped by
    registrar domain-forwarding and some redirectors, landing the user on the
    sign-in page with no explanation. A fragment gets percent-encoded to %23 by
    link-rewriting scanners (Outlook Safe Links and similar), turning it into a
    nonsense path and a 404. Both were observed in practice.

    /reset/<token> and /verify/<token> are served the app itself (see the routes
    beside the static mount), and the client also still reads the older ?param=
    and #param= forms so links already sitting in inboxes keep working.
    """
    return f"{_action_base_url(request)}/{param}/{urllib.parse.quote(token, safe='')}"


def _acceptable_password(password: str, *, username: str = "", email: str = "") -> str:
    """A password Delphi will store, or a 422 explaining what is wrong with it.

    Applied everywhere a password is chosen — registration, a reset, a change,
    and an operator setting one — because a rule enforced at three of the four
    is not a rule. See backend/app/passwords.py for what it refuses and why.
    """
    try:
        passwords.check(password, username=username, email=email)
    except passwords.WeakPassword as exc:
        raise HTTPException(422, str(exc))
    return password


@app.post("/api/auth/register", status_code=201)
def register(body: dict, request: Request, background: BackgroundTasks,
             db: Session = Depends(get_db)):
    ratelimit.check("register", request)
    username = (body.get("username") or "").strip().lower()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not _USERNAME_RE.match(username):
        raise HTTPException(422, "Username must be 3-32 characters: letters, digits, _ . -")
    if not _EMAIL_RE.match(email):
        raise HTTPException(422, "A valid email address is required")
    _acceptable_password(password, username=username, email=email)
    if db.scalar(select(User).where(User.username == username)):
        # Usernames stay tellable. They have to be unique, so the form has to
        # say when one is gone or nobody could complete it — and they are
        # public anyway: a shared feed carries the username of whoever shared
        # it. Nothing is given away that the app does not already show.
        raise HTTPException(409, "That username is taken")

    # Hash before the duplicate-email check, not after, so both paths pay the
    # same 200k PBKDF2 rounds. Skipping it on the duplicate path would answer
    # noticeably faster and put the answer back in the response time.
    password_hash = auth.hash_password(password)

    existing = db.scalar(select(User).where(User.email == email))
    if existing:
        # Email addresses are different: "that address already has an account"
        # tells whoever asked whether a given person reads news here, which is
        # worth knowing about this tool in particular, and the asker need not
        # own the address. So the answer is the same either way, and the fact
        # goes to the address itself where only its owner can read it.
        #
        # Only possible with mail configured. Without it there is nowhere to
        # send the notice and no verification step to hide behind, so the
        # honest 409 stays — a self-hosted instance with no mail is a private
        # one, where the exposure is somebody who already has an account.
        if not mailer.enabled():
            raise HTTPException(409, "An account with that email already exists")
        link = _action_link(request, "reset",
                            auth.make_scoped_token("reset", existing.id, 3600))
        background.add_task(mailer.send_duplicate_registration,
                            existing.email, existing.username, link)
        return {"verification_sent": True, "username": username, "email": email}

    user = User(username=username, email=email, password_hash=password_hash,
                email_verified=not mailer.enabled())  # self-host mode: auto-verify
    user.is_admin = _is_configured_admin(user)  # NEWS_ADMIN_USERS → built-in operator
    if user.is_admin:
        # The designated operator is verified on sight. Setting NEWS_ADMIN_USERS
        # requires control of the deployment, which is stronger proof than
        # receiving an email — and without this, misconfigured SMTP locks
        # everyone out of their own instance: sign-up demands a verification
        # link that can never arrive, and no one can reach the console that
        # would fix it. This keeps a way in that email delivery cannot break.
        user.email_verified = True
    db.add(user)
    db.commit()
    if mailer.enabled():
        link = _action_link(request, "verify",
                            auth.make_scoped_token("verify", user.id, 48 * 3600))
        # Hand the SMTP conversation to a background task: talking to the relay
        # can take seconds (or hit smtplib's 20s timeout when the relay stalls),
        # and blocking the response that long makes the Create-account button
        # look dead. The account already exists, so delivery is independent.
        background.add_task(mailer.send_verification, email, username, link)
        return {"verification_sent": True, "username": username, "email": email}
    return {"token": auth.make_token(user.id, user.token_version), "username": username,
            "email": email, "user_key": f"acct:{user.id}", "is_admin": _is_admin(user)}


@app.post("/api/auth/login")
def login(body: dict, request: Request, db: Session = Depends(get_db)):
    ratelimit.check("login", request)
    ident = (body.get("username") or "").strip().lower()  # username or email
    user = db.scalar(select(User).where(
        or_(User.username == ident, User.email == ident)))
    if not user or not auth.verify_password(body.get("password") or "", user.password_hash):
        raise HTTPException(401, "Wrong username/email or password")
    if user.disabled:
        raise HTTPException(403, "This account has been suspended by an operator")
    # Keep the DB flags in step with a NEWS_ADMIN_USERS designation — including
    # for an operator account that registered before being designated, or while
    # mail was broken, so the console is always reachable.
    if _is_configured_admin(user) and not (user.is_admin and user.email_verified):
        user.is_admin = True
        user.email_verified = True
        db.commit()
    if mailer.enabled() and not user.email_verified:
        raise HTTPException(403, "unverified: check your inbox for the verification link")
    return {"token": auth.make_token(user.id, user.token_version), "username": user.username,
            "email": user.email, "user_key": f"acct:{user.id}", "is_admin": _is_admin(user)}


@app.get("/api/auth/verify")
def verify_email(token: str = Query(default=""), db: Session = Depends(get_db)):
    uid = auth.parse_scoped_token("verify", token)
    user = db.get(User, uid) if uid else None
    if not user:
        raise HTTPException(400, "This verification link is invalid or has expired")
    user.email_verified = True
    db.commit()
    return {"ok": True, "username": user.username}


@app.post("/api/auth/resend-verification")
def resend_verification(body: dict, request: Request, background: BackgroundTasks,
                        db: Session = Depends(get_db)):
    """Always answers 200 — no account enumeration."""
    ratelimit.check("resend", request)
    ident = (body.get("username") or "").strip().lower()
    user = db.scalar(select(User).where(or_(User.username == ident, User.email == ident)))
    if user and not user.email_verified and mailer.enabled():
        link = _action_link(request, "verify",
                            auth.make_scoped_token("verify", user.id, 48 * 3600))
        background.add_task(mailer.send_verification, user.email, user.username, link)
    return {"ok": True}


@app.post("/api/auth/forgot")
def forgot_password(body: dict, request: Request, background: BackgroundTasks,
                    db: Session = Depends(get_db)):
    """Always answers 200 — no account enumeration. Sending happens in the
    background so the response time can't reveal whether the address exists
    (and so a stalled relay doesn't hang the caller)."""
    ratelimit.check("forgot", request)
    email = (body.get("email") or "").strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user and mailer.enabled():
        link = _action_link(request, "reset",
                            auth.make_scoped_token("reset", user.id, 3600))
        background.add_task(mailer.send_password_reset, user.email, user.username, link)
    return {"ok": True, "mail_enabled": mailer.enabled()}


@app.post("/api/auth/devices/release-link")
def request_device_release(body: dict, request: Request, background: BackgroundTasks,
                           db: Session = Depends(get_db)):
    """Email a link that signs the account out of every device.

    Under /api/auth/ deliberately: someone asking for this has been refused by
    the device limit, so it must be reachable without getting past the very
    check that is blocking them. That means it is unauthenticated, and it is
    built like the other unauthenticated mail endpoint — always 200, no hint
    whether the address exists, sent in the background so the response time
    cannot answer that question either.
    """
    ratelimit.check("forgot", request)
    email = (body.get("email") or "").strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user and mailer.enabled():
        link = _action_link(request, "devices",
                            auth.make_scoped_token("devices", user.id, 3600))
        background.add_task(mailer.send_device_release, user.email, user.username,
                            link, devices.active_count(db, user.id))
    return {"ok": True, "mail_enabled": mailer.enabled()}


@app.post("/api/auth/devices/release")
def confirm_device_release(body: dict, request: Request, db: Session = Depends(get_db)):
    """Follow-through for that link: forget every device and end every session.

    Both, not either. Forgetting the devices alone frees the slots while the
    old sessions keep their tokens, so a device that was pushed out could come
    straight back and take the slot again — and the person who asked to be
    signed out everywhere would not have been.
    """
    ratelimit.check("reset", request)
    uid = auth.parse_scoped_token("devices", body.get("token") or "")
    user = db.get(User, uid) if uid else None
    if not user:
        raise HTTPException(400, "This sign-out link is invalid or has expired")
    released = devices.release_all(db, user)
    return {"ok": True, "released": released,
            "detail": f"Signed out of {released} {'device' if released == 1 else 'devices'}. "
                      "Sign in again on this one to continue."}


@app.post("/api/auth/reset")
def reset_password(body: dict, request: Request, db: Session = Depends(get_db)):
    ratelimit.check("reset", request)
    uid = auth.parse_scoped_token("reset", body.get("token") or "")
    user = db.get(User, uid) if uid else None
    if not user:
        raise HTTPException(400, "This reset link is invalid or has expired")
    password = body.get("password") or ""
    _acceptable_password(password, username=user.username, email=user.email)
    user.password_hash = auth.hash_password(password)
    user.email_verified = True  # proving inbox access verifies the email too
    # The reason most people reset a password is that they think someone else
    # has it. A new hash alone does nothing about the session that person is
    # already holding — it stays valid for the rest of its thirty days. Bumping
    # the version ends every session on the account, including the one doing
    # the reset, which is why the response says to sign in again.
    user.token_version += 1
    db.commit()
    return {"ok": True, "username": user.username, "sessions_ended": True}


def current_account(authorization: str = Header(default=""),
                    db: Session = Depends(get_db)) -> User:
    """The signed-in account, checked against the database.

    For the routes under /api/auth that need a session. The require_account
    middleware deliberately skips that whole prefix, because sign-in and
    registration have to be reachable without one — so anything there that
    *does* need a session has to do the check itself rather than inherit it.

    user_id_header is not enough on its own: it reads the token and stops,
    which is fine on the routes the middleware already vetted and wrong here.
    A token is only a claim, and three things it cannot know about are exactly
    what these endpoints must respect — the account was deleted, it was
    suspended, or its sessions were ended and this token predates that.
    """
    raw = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    claim = auth.parse_token(raw) if raw else None
    if claim is None:
        raise HTTPException(401, "Authentication required — sign in")
    user = db.get(User, claim.user_id)
    if user is None:
        raise HTTPException(401, "That account no longer exists")
    if user.disabled:
        raise HTTPException(403, "This account has been suspended by an operator")
    if claim.token_version != user.token_version:
        raise HTTPException(401, "This session was ended — sign in again")
    return user


@app.post("/api/auth/change-password")
def change_password(body: dict, request: Request,
                    user: User = Depends(current_account),
                    db: Session = Depends(get_db)):
    """Set a new password while signed in, without going through email.

    Delphi could only reset a password by emailing a link, which is the flow
    for someone locked out — a detour for someone who simply wants a different
    password, and no help at all on an instance with no SMTP, where it meant
    nobody could ever change one.

    The current password is required even though the caller is already signed
    in, and that is the point of the endpoint rather than an inconvenience in
    it. Holding a session is not proof of being the owner: it can be an
    unlocked laptop or a copied token. Without this check, brief access would
    be enough to set a new password and lock the real owner out permanently —
    a moment's lapse turned into a takeover. Knowing the current password is
    what a borrowed session does not come with.
    """
    ratelimit.check("change_password", request)
    if not auth.verify_password(body.get("current") or "", user.password_hash):
        raise HTTPException(403, "That isn't your current password")
    new = body.get("new") or ""
    _acceptable_password(new, username=user.username, email=user.email)
    if auth.verify_password(new, user.password_hash):
        # Not pedantry: going through with it would end this account's other
        # sessions for a change that changed nothing, which is a confusing way
        # to be signed out on your phone.
        raise HTTPException(422, "That is already your password — pick a different one")

    user.password_hash = auth.hash_password(new)
    # Every other device is signed out, because worrying about one is the usual
    # reason to be here. This browser is not: it just proved it knows the old
    # password, so it gets a token at the new version instead of being thrown
    # back to the sign-in screen for doing the right thing.
    user.token_version += 1
    db.commit()
    return {"ok": True, "sessions_ended": True,
            "token": auth.make_token(user.id, user.token_version),
            "username": user.username, "email": user.email,
            "user_key": f"acct:{user.id}", "is_admin": _is_admin(user)}


@app.post("/api/auth/sign-out-everywhere")
def sign_out_everywhere(user: User = Depends(current_account),
                        db: Session = Depends(get_db)):
    """End every session on this account, including the one asking.

    Signing out normally just forgets the token in this browser — which is the
    right thing when you are done, and no help at all when the worry is a
    device you no longer have, or a token someone else copied. Nothing on the
    server changed in that case; the token still works. This changes the
    server: every token issued before now stops being accepted.
    """
    user.token_version += 1
    db.commit()
    return {"ok": True, "sessions_ended": True}


@app.get("/api/auth/me")
def me(user: User = Depends(current_account)):
    return {"user_key": f"acct:{user.id}", "username": user.username,
            "email": user.email, "is_admin": _is_admin(user)}


# NOTE: the old /api/auth/claim endpoint was removed. It migrated feeds/alerts
# from a client-supplied anonymous id (X-User-Id) into the account, but that id
# carried no proof of possession — anyone knowing another browser's random id
# could claim its data. Since accounts became mandatory (every /api/* route
# demands an account token), no feed or alert is ever created under an
# anonymous id, so the endpoint migrated nothing and only presented risk.


# ---------- session onboarding ----------

def _pending_updates(user: User) -> list[dict]:
    """Changelog entries this account hasn't been shown yet, via per-entry
    fingerprints. Accounts predating fingerprint tracking fall back to the
    date-based comparison once; brand-new accounts see nothing (everything is
    new to them anyway). Caller must persist the returned marker."""
    if user.changelog_seen is not None:
        try:
            return unseen_entries(json.loads(user.changelog_seen))
        except ValueError:
            return []
    if user.last_seen_at is not None:
        return updates_since(user.last_seen_at)
    return []


@app.post("/api/session/hello")
def session_hello(user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    """Called once per app load. Records when the account was last seen and
    tells the client what onboarding to show: the FAQ on the very first visit
    (or after a week or more away), and a what's-new digest of changelog
    entries that shipped while the user was gone, grouped by date."""
    user = db.get(User, int(user_id.split(":", 1)[1]))
    if user is None:
        raise HTTPException(401, "Account no longer exists")
    prev = user.last_seen_at
    now = utcnow()
    updates = _pending_updates(user)
    user.last_seen_at = now
    user.changelog_seen = json.dumps(fingerprints())
    db.commit()
    away_days = None if prev is None else (now - prev).total_seconds() / 86400
    return {
        "first_visit": prev is None,
        "away_days": away_days,
        "faq_due": prev is None or away_days >= 7,
        "updates": updates,
    }


@app.post("/api/session/check-updates")
def session_check_updates(user_id: str = Depends(user_id_header),
                          db: Session = Depends(get_db)):
    """Polled by open sessions (and on stream reconnect after a redeploy):
    returns changelog entries that shipped since this account last saw the
    What's-new popup, so updates surface live without a fresh sign-in."""
    user = db.get(User, int(user_id.split(":", 1)[1]))
    if user is None:
        raise HTTPException(401, "Account no longer exists")
    updates = _pending_updates(user) if user.changelog_seen is not None else []
    user.last_seen_at = utcnow()  # an open session counts as presence
    user.changelog_seen = json.dumps(fingerprints())
    db.commit()
    return {"updates": updates}


@app.get("/api/changelog")
def full_changelog():
    """Every release-notes entry, newest first (Settings → What's new)."""
    return CHANGELOG


@app.get("/api/session/settings")
def get_user_settings(user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    """The account's saved display/notification preferences (may be empty)."""
    user = db.get(User, _acct_id(user_id))
    if user is None:
        raise HTTPException(401, "Account no longer exists")
    try:
        return {"settings": json.loads(user.settings) if user.settings else {}}
    except ValueError:
        return {"settings": {}}


@app.put("/api/session/settings")
def save_user_settings(body: dict, user_id: str = Depends(user_id_header),
                       db: Session = Depends(get_db)):
    """Persist preferences on the account so they follow the user across
    browsers, devices, and URL changes (Codespaces mint a new origin — and a
    new empty localStorage — every time)."""
    user = db.get(User, _acct_id(user_id))
    if user is None:
        raise HTTPException(401, "Account no longer exists")
    settings = body.get("settings")
    if not isinstance(settings, dict):
        raise HTTPException(422, "Settings must be an object")
    raw = json.dumps(settings)
    if len(raw) > 4096:
        raise HTTPException(422, "Settings payload too large")
    user.settings = raw
    db.commit()
    return {"ok": True}


# ---------- sources ----------

# The rendered source catalog, and when it was built.
#
# This is half a megabyte across more than a thousand outlets, and it was built
# from scratch for every caller: the ORM rows, a dict each, FastAPI's encoder
# walking all twenty-odd thousand values again, then JSON. About 80ms of pure
# Python — which is fine once and ruinous concurrently, because none of it
# releases the interpreter. Ten callers at once measured 2.9s each and forty
# never finished at all, and since a browser asks for this whenever the Sources
# panel or the feed editor opens, one reader with a few tabs is enough. While
# it ran, every other request queued behind it — which is why panels that touch
# none of this were slow to open too.
#
# So it is built once and handed out. The TTL is what bounds staleness of the
# poll status shown in the panel; sources are polled minutes apart, so seconds
# of lag there costs nothing, and anything that edits the catalog clears it
# outright so a change is never waited for.
_SOURCES_CACHE: dict[bool, tuple[float, bytes]] = {}
SOURCES_CACHE_TTL = 15.0


def _invalidate_sources_cache() -> None:
    _SOURCES_CACHE.clear()


@app.get("/api/sources")
def list_sources(slim: bool = Query(default=False), db: Session = Depends(get_db)):
    """Every source, or — with slim=1 — just enough to name one.

    The full catalog runs past a thousand outlets and half a megabyte, and the
    dashboard needs it only when the Sources panel or the wizard's picker is
    open. At startup all it has to do is put a name against the outlets a feed
    is restricted to, which is two fields.

    Served from a rendered cache — see the note above it."""
    fresh = _SOURCES_CACHE.get(slim)
    if fresh and time.monotonic() - fresh[0] < SOURCES_CACHE_TTL:
        return Response(fresh[1], media_type="application/json")

    if slim:
        rows = db.execute(select(Source.id, Source.name).order_by(Source.name)).all()
        payload = [{"id": i, "name": n} for i, n in rows]
    else:
        # Columns rather than whole ORM objects: nothing here needs a mapped
        # instance, and building 1,200 of them to read 19 attributes off each
        # is most of the query's cost.
        cols = (Source.id, Source.name, Source.rss_url, Source.homepage,
                Source.country, Source.region, Source.language, Source.scope,
                Source.categories, Source.tier, Source.platform, Source.paywall,
                Source.enabled, Source.added_by, Source.last_fetched_at,
                Source.last_status, Source.last_article_count,
                Source.consecutive_failures, Source.repaired_from)
        rows = db.execute(select(*cols).order_by(Source.name)).all()
        # Which of them have ever produced anything, in one pass over the index
        # rather than a query per source. A green health dot only says the feed
        # answered; this says whether answering ever amounted to news.
        producing = set(db.scalars(select(Article.source_id).distinct()))
        payload = [{
            "id": s.id, "name": s.name, "rss_url": s.rss_url, "homepage": s.homepage,
            "country": s.country, "region": s.region, "language": s.language,
            "scope": s.scope, "categories": s.categories or [], "tier": s.tier,
            "platform": s.platform or "news",
            "paywall": bool(s.paywall),
            "enabled": s.enabled, "added_by": s.added_by,
            "last_fetched_at": s.last_fetched_at.isoformat() + "Z" if s.last_fetched_at else None,
            "last_status": s.last_status, "last_article_count": s.last_article_count,
            "consecutive_failures": s.consecutive_failures or 0,
            "repaired_from": s.repaired_from or "",
            "has_produced": s.id in producing,
        } for s in rows]

    # Rendered here rather than returned as a list, so FastAPI's encoder does
    # not walk every value in it a second time on the way out. Everything in
    # the payload is already a JSON type.
    body = json.dumps(payload, separators=(",", ":")).encode()
    _SOURCES_CACHE[slim] = (time.monotonic(), body)
    return Response(body, media_type="application/json")


def _fetchable(url: str) -> str:
    """A URL Delphi is willing to point itself at, or a 422 explaining why not.

    The same check runs again at fetch time — that one covers redirects, which
    this cannot — but catching it here means someone who mistypes an address
    is told immediately instead of watching a source sit there never working.
    """
    try:
        # A name that will not resolve is left alone here: it may be a host
        # being set up, and the fetch-time guard refuses it if it ever points
        # inward. Saving rejects what is known to be bad; fetching fails closed.
        return safefetch.check_url(url, unresolvable_ok=True)
    except safefetch.BlockedURL as exc:
        raise HTTPException(422, str(exc))


@app.post("/api/sources", status_code=201)
def create_source(body: SourceIn, db: Session = Depends(get_db)):
    _fetchable(body.rss_url)
    if db.scalar(select(Source).where(Source.rss_url == body.rss_url)):
        raise HTTPException(409, "A source with this RSS URL already exists")
    source = Source(**body.model_dump(), added_by="user")
    db.add(source)
    db.commit()
    _invalidate_sources_cache()
    return {"id": source.id}


@app.post("/api/sources/seed-cities")
def seed_cities_endpoint(admin: User = Depends(require_admin),
                         db: Session = Depends(get_db)):
    """Add local-news sources for every world city in the catalog (idempotent).
    Lets an already-running instance pick up the city catalog without a restart."""
    from . import cities
    added = seed_city_sources(db)
    _invalidate_sources_cache()
    return {"added": added, "cities_in_catalog": cities.city_count()}


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
    _invalidate_sources_cache()
    return {"id": source.id, "rss_url": rss}


@app.patch("/api/sources/{source_id}")
def update_source(source_id: int, body: dict, db: Session = Depends(get_db)):
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    new_url = (body.get("rss_url") or "").strip()
    if new_url and new_url != source.rss_url:
        _fetchable(new_url)
        if db.scalar(select(Source).where(Source.rss_url == new_url)):
            raise HTTPException(409, "Another source already uses this RSS URL")
        source.rss_url = new_url
        source.last_status = ""  # health unknown until next poll
    for key in ("name", "enabled", "country", "language", "scope",
                "categories", "tier", "platform", "homepage", "region", "paywall"):
        if key in body:
            setattr(source, key, body[key])
    db.commit()
    _invalidate_sources_cache()
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
    _invalidate_sources_cache()
    return {"created": created}


@app.delete("/api/sources/{source_id}", status_code=204)
def delete_source(source_id: int, admin: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    """Operators only.

    The catalog is shared and collaborative on purpose — anyone may add a
    source or correct one. Deleting is different in kind: it removes the outlet
    for every reader on the server and takes its articles with it, and there is
    no undo. Everyone else can switch a source off instead, which is reversible
    and has the same effect on their board.
    """
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    db.delete(source)
    db.commit()
    _invalidate_sources_cache()


@app.post("/api/sources/{source_id}/repair")
async def repair_source(source_id: int, db: Session = Depends(get_db)):
    """Manual self-repair (the 🔧 button): re-check the current URL first —
    a source that merely had a bad day is marked healthy without changes —
    then hunt for a working replacement feed and ingest it immediately."""
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    old_url = source.rss_url
    async with safefetch.client(timeout=repair.REPAIR_TIMEOUT) as client:
        fixed, entries = await repair.attempt_repair(client, db, source, verify_current=True)
    if not fixed:
        db.commit()  # persist the attempt timestamp
        return {"repaired": False, "changed": False, "status": source.last_status,
                "detail": "No working feed found — the outlet may be gone. Edit the URL or delete the source."}
    new = ingest.process_entries(db, source, entries, ingest._recent_clusters(db), set())
    source.last_article_count = len(new)
    db.commit()
    assign_events(db, new)
    db.commit()
    _invalidate_sources_cache()
    return {"repaired": True, "changed": source.rss_url != old_url,
            "rss_url": source.rss_url, "repaired_from": source.repaired_from,
            "status": source.last_status, "new_articles": len(new)}


# ---------- articles / search ----------

# How many column scans may be in flight at once. The matching is Python, so
# the threads take turns at the interpreter anyway, and letting a whole board
# loose only adds contention: six columns measured 2.69s unbounded against
# 0.94s two at a time, on the same data. Two keeps a thread working while
# another waits on SQLite, and is few enough that the queue stays short.
SCAN_CONCURRENCY = 2

# A semaphore belongs to the loop that first waits on it, and raises if it is
# used from another. The server has one loop for its whole life, so this could
# have been a module-level constant — but each test runs its own loop, and a
# limiter that only works under the first of them is a limiter nobody can
# check. Built on demand, discarded when the loop changes.
_SCAN_SLOTS: asyncio.Semaphore | None = None
_SCAN_SLOTS_LOOP: asyncio.AbstractEventLoop | None = None


def _scan_slots() -> asyncio.Semaphore:
    global _SCAN_SLOTS, _SCAN_SLOTS_LOOP
    loop = asyncio.get_running_loop()
    if _SCAN_SLOTS is None or _SCAN_SLOTS_LOOP is not loop:
        _SCAN_SLOTS, _SCAN_SLOTS_LOOP = asyncio.Semaphore(SCAN_CONCURRENCY), loop
    return _SCAN_SLOTS


async def scan_articles(db: Session, criteria: dict, *, sort: str, limit: int):
    """`query_articles` on a worker thread instead of the event loop.

    Every column on the board is one of these, and each is a SQL read followed
    by the matcher sifting the rows in Python — hundreds of milliseconds on a
    full database, and none of it releases the loop. Called directly from an
    `async def` handler it stops the whole process: while one column is being
    matched, nothing else is served, not the other columns and not the cheap
    reads behind the panels.

    That is what a reader sees as the favourite-locations panel timing out.
    Locations are among the smallest queries in the app, so when one takes
    thirty seconds it is because it spent that time waiting for a turn. On a
    120,000-article copy, a board of six watched-place columns held an
    unrelated `GET /api/locations` for 719 ms; with the scan on a thread the
    same request comes back in 36 ms while the board loads around it.

    The session is safe here: the engine is opened with `check_same_thread`
    off, and each request has its own session used by one thread at a time.
    """
    async with _scan_slots():
        return await run_in_threadpool(query_articles, db, criteria,
                                       sort=sort, limit=limit)


@app.post("/api/articles/search")
async def search_articles(
    body: dict,
    request: Request,
    sort: str = Query(default="newest"),
    limit: int = Query(default=50, le=200),
    lang: str = Query(default=""),
    user_id: str = Depends(user_id_header),
    db: Session = Depends(get_db),
):
    """Ad-hoc search with a criteria object (used for feed preview and search bar)."""
    ratelimit.check("search", request)
    criteria = body.get("criteria", body)
    # One of Home's columns is the same query for every reader, so the poller
    # has already run it; everything below this line is still per-request.
    articles = home.take(db, criteria, sort, limit, grouped=False)
    if articles is None:
        articles = await scan_articles(db, criteria, sort=sort, limit=limit)
    tr = await translate.translate_articles(db, articles, lang)
    viewed = _viewed_events(db, user_id, articles)
    read_alone = _viewed_articles(db, user_id, articles)
    events = _events_for(db, articles) if (criteria or {}).get("hide_stale") else None
    return [_article_json(a, tr, viewed, events, read_alone) for a in articles]


@app.post("/api/query/validate")
def query_validate(body: QueryValidateIn):
    err = validate_query(body.query) if body.query.strip() else None
    # A query can parse and still not mean what it looks like it means; see
    # query_advisories. Reported separately from `error` so the builder can say
    # so without refusing to save.
    notes = query_advisories(body.query) if err is None and body.query.strip() else []
    return {"valid": err is None, "error": err, "advisories": notes}


# ---------- feeds ----------

@app.get("/api/feeds")
def list_feeds(user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    feeds = db.scalars(
        select(Feed).where(Feed.user_id == user_id, Feed.pantheon_id.is_(None))
        .order_by(Feed.position, Feed.id)
    ).all()
    return [{
        "id": f.id, "name": f.name, "criteria": f.criteria, "sort": f.sort,
        "position": f.position, "width": f.width, "group_events": f.group_events,
    } for f in feeds]


@app.post("/api/feeds", status_code=201)
def create_feed(body: FeedIn, user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    _reject_invalid_query(body.criteria)
    _ensure_coverage_source(db, body.criteria)
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
    feed = _shared_item_for_edit(db, Feed, feed_id, user_id)
    _reject_invalid_query(body.criteria)
    _ensure_coverage_source(db, body.criteria)
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
    feeds = {f.id: f for f in db.scalars(select(Feed).where(
        Feed.user_id == user_id, Feed.pantheon_id.is_(None)))}
    for pos, fid in enumerate(order):
        if fid in feeds:
            feeds[fid].position = pos
    db.commit()
    return {"ok": True}


@app.delete("/api/feeds/{feed_id}", status_code=204)
def delete_feed(feed_id: int, user_id: str = Depends(user_id_header),
                db: Session = Depends(get_db)):
    feed = _shared_item_for_edit(db, Feed, feed_id, user_id)
    db.delete(feed)
    db.commit()


# How many articles one export may contain. A column shows forty; an export is
# for taking the work elsewhere, so it reaches much further back — but not so
# far that one click builds a hundred-megabyte document on a small machine.
EXPORT_MAX = 2000


def _export_rows(articles: list[Article], tr: dict | None = None) -> list[dict]:
    """The articles of an export, flattened for a spreadsheet.

    Deliberately not `_article_json`: that shape serves the dashboard, and an
    export wants the outlet's name rather than its id, plain timestamps, and
    nothing about what the reader has already opened."""
    return [{
        "title": (tr or {}).get(a.id, {}).get("title") or a.title,
        "published_at": a.published_at,
        "source": a.source.name if a.source else "",
        "country": a.country or "",
        "categories": a.categories or [],
        "importance": a.importance,
        "scope": a.source.scope if a.source else "",
        "language": a.language or "",
        "event_id": a.event_id or "",
        "summary": (tr or {}).get(a.id, {}).get("summary") or a.summary or "",
        "url": a.url,
    } for a in articles]


def _export_response(fmt: str, name: str, rows: list[dict]):
    try:
        body, media, filename = export.build(fmt, name, rows)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return Response(
        content=body, media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                 # The browser reads this to name the toast; it is not sent
                 # cross-origin by default, so it has to be exposed.
                 "X-Export-Rows": str(len(rows)),
                 "Access-Control-Expose-Headers": "Content-Disposition, X-Export-Rows"})


@app.get("/api/feeds/{feed_id}/export")
async def export_feed(feed_id: int, request: Request,
                      fmt: str = Query(default="csv", alias="format"),
                      limit: int = Query(default=500, le=EXPORT_MAX),
                      lang: str = Query(default=""),
                      user_id: str = Depends(user_id_header),
                      db: Session = Depends(get_db)):
    """One saved feed's current matches, as a file."""
    ratelimit.check("export", request)
    feed = _shared_item_for_read(db, Feed, feed_id, user_id)
    articles = await scan_articles(db, feed.criteria, sort=feed.sort, limit=limit)
    tr = await translate.translate_articles(db, articles, lang)
    return _export_response(fmt, feed.name, _export_rows(articles, tr))


@app.post("/api/articles/export")
async def export_search(body: dict, request: Request,
                        fmt: str = Query(default="csv", alias="format"),
                        sort: str = Query(default="newest"),
                        limit: int = Query(default=500, le=EXPORT_MAX),
                        lang: str = Query(default=""),
                        user_id: str = Depends(user_id_header),
                        db: Session = Depends(get_db)):
    """The same, for a column that isn't a saved feed — Home's built-in columns
    and the results of a search from the rail."""
    ratelimit.check("export", request)
    criteria = body.get("criteria", body)
    name = (body.get("name") or "Delphi export").strip()[:120]
    articles = await scan_articles(db, criteria, sort=sort, limit=limit)
    tr = await translate.translate_articles(db, articles, lang)
    return _export_response(fmt, name, _export_rows(articles, tr))


@app.get("/api/feeds/{feed_id}/why/{article_id}")
def why_it_matched(feed_id: int, article_id: int,
                   user_id: str = Depends(user_id_header),
                   db: Session = Depends(get_db)):
    """Which of this feed's words are in this article, and where.

    A reader who opens a result and can find none of their terms in it cannot
    tell a broken search from a term sitting somewhere they are not looking —
    and the two are fixed in completely different places. This says which it
    is. A phrase in the headline is the search working; the same phrase only in
    the body is often the page's own furniture, which never appears on the card
    and is exactly what a reader means by "it matched nothing I asked for".
    """
    feed = _shared_item_for_read(db, Feed, feed_id, user_id)
    article = db.get(Article, article_id)
    if not article:
        raise HTTPException(404, "Article not found")
    hits = explain_text_match(feed.criteria or {}, article)
    return {
        "article_id": article.id,
        "feed_id": feed.id,
        "hits": hits,
        # True when nothing in the headline or summary put it here, which is
        # the case worth explaining.
        "body_only": bool(hits) and all(h["where"] == "body" for h in hits),
    }


@app.get("/api/feeds/{feed_id}/articles")
async def feed_articles(feed_id: int, limit: int = Query(default=40, le=200),
                        lang: str = Query(default=""),
                        user_id: str = Depends(user_id_header),
                        db: Session = Depends(get_db)):
    feed = _shared_item_for_read(db, Feed, feed_id, user_id)
    articles = await scan_articles(db, feed.criteria, sort=feed.sort, limit=limit)
    tr = await translate.translate_articles(db, articles, lang)
    viewed = _viewed_events(db, user_id, articles)
    read_alone = _viewed_articles(db, user_id, articles)
    events = _events_for(db, articles) if (feed.criteria or {}).get("hide_stale") else None
    return [_article_json(a, tr, viewed, events, read_alone) for a in articles]


async def _grouped_response(db: Session, articles: list[Article], lang: str, limit: int,
                            user_id: str = ""):
    """Cluster a result list into event groups (feed's order preserved by each
    event's first matching article; articles without an event are singletons)."""
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

    event_ids = [k for k in order if isinstance(k, int)]
    viewed: set[int] = set()
    if user_id and event_ids:
        viewed = set(db.scalars(select(ViewedEvent.event_id).where(
            ViewedEvent.user_id == user_id, ViewedEvent.event_id.in_(event_ids))))

    # One query for the page's events, rather than a lazy load per group.
    events = _events_for(db, [a for key in order for a in groups[key][:1]])

    out = []
    for key in order:
        members = groups[key]
        event = events.get(members[0].event_id)
        out.append({
            "event_id": members[0].event_id,
            "viewed": members[0].event_id in viewed,
            "matched_count": len(members),
            "total_count": event.article_count if event else len(members),
            "source_count": len({a.source_id for a in members}),
            "importance": event.importance if event else max(a.importance for a in members),
            "first_seen": event.first_seen.isoformat() + "Z" if event else None,
            # The client hides stale events, so it needs to know how fresh each is.
            "updated_at": event.updated_at.isoformat() + "Z" if event else None,
            "articles": [_article_json(a, tr) for a in members[:6]],
        })
    return out


@app.get("/api/feeds/{feed_id}/events")
async def feed_events(feed_id: int, limit: int = Query(default=30, le=100),
                      lang: str = Query(default=""),
                                        user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    feed = _shared_item_for_read(db, Feed, feed_id, user_id)
    articles = await scan_articles(db, feed.criteria, sort=feed.sort, limit=200)
    return await _grouped_response(db, articles, lang, limit, user_id)


@app.post("/api/articles/search-grouped")
async def search_grouped(
    body: dict,
    request: Request,
    sort: str = Query(default="newest"),
    limit: int = Query(default=30, le=100),
    lang: str = Query(default=""),
    user_id: str = Depends(user_id_header),
    db: Session = Depends(get_db),
):
    """Ad-hoc criteria search clustered into events (used by Home columns)."""
    ratelimit.check("search", request)
    criteria = body.get("criteria", body)
    articles = home.take(db, criteria, sort, limit, grouped=True)
    if articles is None:
        articles = await scan_articles(db, criteria, sort=sort,
                                       limit=home.GROUPED_QUERY_LIMIT)
    return await _grouped_response(db, articles, lang, limit, user_id)


@app.post("/api/articles/{article_id}/viewed")
def mark_article_viewed(article_id: int, user_id: str = Depends(user_id_header),
                        db: Session = Depends(get_db)):
    """Mark one article read, for the ones that belong to no event.

    An article with an event is remembered by the event instead — that is what
    dims a story wherever else it appears — so this is deliberately narrow."""
    article = db.get(Article, article_id)
    if not article:
        raise HTTPException(404, "That article is no longer in the archive — "
                                 "articles are removed after 30 days")
    if article.event_id is not None:
        # Not an error: the caller wanted it remembered, and it is, by story.
        exists = db.scalar(select(ViewedEvent).where(
            ViewedEvent.user_id == user_id, ViewedEvent.event_id == article.event_id))
        if not exists:
            db.add(ViewedEvent(user_id=user_id, event_id=article.event_id))
            db.commit()
        return {"ok": True, "remembered_as": "event"}
    exists = db.scalar(select(ViewedArticle).where(
        ViewedArticle.user_id == user_id, ViewedArticle.article_id == article_id))
    if not exists:
        db.add(ViewedArticle(user_id=user_id, article_id=article_id))
        db.commit()
    return {"ok": True, "remembered_as": "article"}


@app.post("/api/events/{event_id}/viewed")
def mark_event_viewed(event_id: int, user_id: str = Depends(user_id_header),
                      db: Session = Depends(get_db)):
    if not db.get(Event, event_id):
        raise HTTPException(404, "That event is no longer in the archive — "
                                 "events are removed with their articles after "
                                 "30 days")
    exists = db.scalar(select(ViewedEvent).where(
        ViewedEvent.user_id == user_id, ViewedEvent.event_id == event_id))
    if not exists:
        db.add(ViewedEvent(user_id=user_id, event_id=event_id))
        db.commit()
    return {"ok": True}


# ---------- alerts ----------

def _valid_webhook(url: str) -> str:
    """Accept only http(s) webhook URLs; reject anything else (a stored
    file://, gopher://, … could be abused). Empty string disables the webhook."""
    url = (url or "").strip()[:500]
    if not url:
        return ""
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(422, "Webhook URL must start with http:// or https://")
    # An alert's webhook is a URL the server posts to on the user's behalf,
    # which makes it the same kind of thing as a feed URL: refuse anything that
    # is not out on the public internet.
    return _fetchable(url)


@app.get("/api/alerts")
def list_alerts(user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    """The caller's own alerts plus every alert shared with their Pantheons."""
    own = db.scalars(
        select(Alert).where(Alert.user_id == user_id, Alert.pantheon_id.is_(None))
        .order_by(Alert.id)
    ).all()
    my_pantheons = {m.pantheon_id: m for m in db.scalars(
        select(PantheonMember).where(PantheonMember.user_id == _acct_id(user_id)))}
    shared = db.scalars(
        select(Alert).where(Alert.pantheon_id.in_(my_pantheons.keys())).order_by(Alert.id)
    ).all() if my_pantheons else []
    names = {p.id: p.name for p in db.scalars(
        select(Pantheon).where(Pantheon.id.in_(my_pantheons.keys())))} if my_pantheons else {}
    everything = [*own, *shared]
    # One grouped count instead of a query per alert (the bell refreshes often).
    unseen_by_alert: dict[int, int] = dict(db.execute(
        select(AlertEvent.alert_id, func.count(AlertEvent.id))
        .where(AlertEvent.alert_id.in_([a.id for a in everything]),
               AlertEvent.seen.is_(False))
        .group_by(AlertEvent.alert_id)
    ).all()) if everything else {}
    out = []
    for a in everything:
        unseen = unseen_by_alert.get(a.id, 0)
        row = {
            "id": a.id, "name": a.name, "criteria": a.criteria, "active": a.active,
            "notify_email": bool(a.notify_email), "webhook_url": a.webhook_url or "",
            "last_triggered_at": a.last_triggered_at.isoformat() + "Z" if a.last_triggered_at else None,
            "unseen": unseen,
        }
        if a.pantheon_id is not None:
            member = my_pantheons[a.pantheon_id]
            row.update({
                "pantheon_id": a.pantheon_id,
                "pantheon_name": names.get(a.pantheon_id, ""),
                "shared_by": a.shared_by,
                "can_edit": a.user_id == user_id or member.role in ("owner", "admin"),
            })
        out.append(row)
    return out


@app.post("/api/alerts", status_code=201)
def create_alert(body: AlertIn, user_id: str = Depends(user_id_header),
                 db: Session = Depends(get_db)):
    _reject_invalid_query(body.criteria)
    _ensure_coverage_source(db, body.criteria)
    alert = Alert(user_id=user_id, name=body.name,
                  criteria=body.criteria.model_dump(), active=body.active,
                  notify_email=body.notify_email,
                  webhook_url=_valid_webhook(body.webhook_url))
    db.add(alert)
    db.commit()
    return {"id": alert.id}


@app.put("/api/alerts/{alert_id}")
def update_alert(alert_id: int, body: AlertIn, user_id: str = Depends(user_id_header),
                 db: Session = Depends(get_db)):
    alert = _shared_item_for_edit(db, Alert, alert_id, user_id)
    _reject_invalid_query(body.criteria)
    _ensure_coverage_source(db, body.criteria)
    alert.name = body.name
    alert.criteria = body.criteria.model_dump()
    alert.active = body.active
    alert.notify_email = body.notify_email
    alert.webhook_url = _valid_webhook(body.webhook_url)
    db.commit()
    return {"ok": True}


@app.delete("/api/alerts/{alert_id}", status_code=204)
def delete_alert(alert_id: int, user_id: str = Depends(user_id_header),
                 db: Session = Depends(get_db)):
    alert = _shared_item_for_edit(db, Alert, alert_id, user_id)
    db.delete(alert)
    db.commit()


@app.get("/api/alerts/{alert_id}/events")
async def alert_events(alert_id: int, limit: int = Query(default=50, le=200),
                       lang: str = Query(default=""),
                       user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    _shared_item_for_read(db, Alert, alert_id, user_id)
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
    _shared_item_for_read(db, Alert, alert_id, user_id)
    for e in db.scalars(select(AlertEvent).where(
            AlertEvent.alert_id == alert_id, AlertEvent.seen.is_(False))):
        e.seen = True
    db.commit()
    return {"ok": True}


# ---------- pantheons (organizations) ----------

_DEFAULT_PANTHEON_SETTINGS = {"who_can_invite": "members", "who_can_share": "members"}


def _pantheon_json(db: Session, p: Pantheon, member: PantheonMember | None) -> dict:
    member_count = db.scalar(select(func.count(PantheonMember.id)).where(
        PantheonMember.pantheon_id == p.id)) or 0
    feed_count = db.scalar(select(func.count(Feed.id)).where(Feed.pantheon_id == p.id)) or 0
    alert_count = db.scalar(select(func.count(Alert.id)).where(Alert.pantheon_id == p.id)) or 0
    owner = db.get(User, p.owner_id)
    out = {
        "id": p.id, "name": p.name, "description": p.description,
        "visibility": p.visibility, "owner_name": owner.username if owner else "",
        "member_count": member_count, "feed_count": feed_count, "alert_count": alert_count,
        "role": member.role if member else None,
    }
    if member:  # settings are members-only information
        out["settings"] = {**_DEFAULT_PANTHEON_SETTINGS, **(p.settings or {})}
    return out


@app.get("/api/pantheons")
def list_pantheons(user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    """The caller's Pantheons and their pending invitations."""
    uid = _acct_id(user_id)
    memberships = db.scalars(select(PantheonMember).where(
        PantheonMember.user_id == uid)).all()
    mine = []
    for m in memberships:
        p = db.get(Pantheon, m.pantheon_id)
        if p:
            mine.append(_pantheon_json(db, p, m))
    invites = []
    for inv in db.scalars(select(PantheonInvite).where(PantheonInvite.user_id == uid)):
        p = db.get(Pantheon, inv.pantheon_id)
        by = db.get(User, inv.invited_by)
        if p:
            invites.append({"id": inv.id, "pantheon_id": p.id, "name": p.name,
                            "invited_by": by.username if by else "?",
                            "member_count": db.scalar(select(func.count(PantheonMember.id))
                                                      .where(PantheonMember.pantheon_id == p.id)) or 0})
    return {"mine": sorted(mine, key=lambda x: x["name"].lower()), "invites": invites}


@app.post("/api/pantheons", status_code=201)
def create_pantheon(body: dict, user_id: str = Depends(user_id_header),
                    db: Session = Depends(get_db)):
    name = (body.get("name") or "").strip()
    if not 2 <= len(name) <= 80:
        raise HTTPException(422, "A Pantheon name needs 2-80 characters")
    visibility = body.get("visibility", "private")
    if visibility not in ("private", "public"):
        raise HTTPException(422, "Visibility must be private or public")
    uid = _acct_id(user_id)
    p = Pantheon(name=name, description=(body.get("description") or "").strip()[:500],
                 visibility=visibility, owner_id=uid,
                 settings=dict(_DEFAULT_PANTHEON_SETTINGS))
    db.add(p)
    db.flush()
    member = PantheonMember(pantheon_id=p.id, user_id=uid, role="owner")
    db.add(member)
    db.commit()
    return _pantheon_json(db, p, member)


@app.get("/api/pantheons/public")
def public_pantheons(user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    """Directory of public Pantheons anyone may join."""
    uid = _acct_id(user_id)
    joined = set(db.scalars(select(PantheonMember.pantheon_id).where(
        PantheonMember.user_id == uid)))
    # Minimal projection for non-members: only what's needed to decide to
    # join. Internal activity (feed/alert counts) and the owner's username are
    # withheld until you're actually a member — see _pantheon_json.
    out = []
    for p in db.scalars(select(Pantheon).where(Pantheon.visibility == "public")
                        .order_by(Pantheon.name).limit(200)):
        member_count = db.scalar(select(func.count(PantheonMember.id)).where(
            PantheonMember.pantheon_id == p.id)) or 0
        out.append({
            "id": p.id, "name": p.name, "description": p.description,
            "visibility": "public", "member_count": member_count,
            "joined": p.id in joined,
        })
    return out


@app.get("/api/pantheons/{pantheon_id}")
def pantheon_detail(pantheon_id: int, user_id: str = Depends(user_id_header),
                    db: Session = Depends(get_db)):
    pantheon, member = _require_membership(db, pantheon_id, user_id)
    out = _pantheon_json(db, pantheon, member)
    out["members"] = [{
        "user_id": m.user_id,
        "username": (u.username if (u := db.get(User, m.user_id)) else "?"),
        "role": m.role,
    } for m in db.scalars(select(PantheonMember).where(
        PantheonMember.pantheon_id == pantheon_id).order_by(PantheonMember.joined_at))]
    if member.role in ("owner", "admin"):
        out["pending_invites"] = [
            (u.username if (u := db.get(User, inv.user_id)) else "?")
            for inv in db.scalars(select(PantheonInvite).where(
                PantheonInvite.pantheon_id == pantheon_id))
        ]
    return out


@app.patch("/api/pantheons/{pantheon_id}")
def update_pantheon(pantheon_id: int, body: dict,
                    user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    pantheon, member = _require_membership(db, pantheon_id, user_id)
    if member.role not in ("owner", "admin"):
        raise HTTPException(403, "Only the owner or an admin can change Pantheon settings")
    if "name" in body:
        name = (body["name"] or "").strip()
        if not 2 <= len(name) <= 80:
            raise HTTPException(422, "A Pantheon name needs 2-80 characters")
        pantheon.name = name
    if "description" in body:
        pantheon.description = (body["description"] or "").strip()[:500]
    if "visibility" in body:
        if body["visibility"] not in ("private", "public"):
            raise HTTPException(422, "Visibility must be private or public")
        pantheon.visibility = body["visibility"]
    if "settings" in body and isinstance(body["settings"], dict):
        merged = {**_DEFAULT_PANTHEON_SETTINGS, **(pantheon.settings or {})}
        for key in ("who_can_invite", "who_can_share"):
            if key in body["settings"]:
                if body["settings"][key] not in ("members", "admins"):
                    raise HTTPException(422, f"{key} must be members or admins")
                merged[key] = body["settings"][key]
        pantheon.settings = merged
    db.commit()
    return _pantheon_json(db, pantheon, member)


@app.delete("/api/pantheons/{pantheon_id}", status_code=204)
def delete_pantheon(pantheon_id: int, user_id: str = Depends(user_id_header),
                    db: Session = Depends(get_db)):
    pantheon, member = _require_membership(db, pantheon_id, user_id)
    if member.role != "owner":
        raise HTTPException(403, "Only the owner can delete a Pantheon")
    for alert in db.scalars(select(Alert).where(Alert.pantheon_id == pantheon_id)):
        db.delete(alert)  # cascades its AlertEvents
    db.execute(sa_delete(Feed).where(Feed.pantheon_id == pantheon_id))
    db.execute(sa_delete(PantheonInvite).where(PantheonInvite.pantheon_id == pantheon_id))
    db.execute(sa_delete(PantheonMember).where(PantheonMember.pantheon_id == pantheon_id))
    db.delete(pantheon)
    db.commit()


@app.post("/api/pantheons/{pantheon_id}/invite")
def invite_to_pantheon(pantheon_id: int, body: dict,
                       user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    pantheon, member = _require_membership(db, pantheon_id, user_id)
    if not _pantheon_allows(pantheon, member, "invite"):
        raise HTTPException(403, "This Pantheon lets only admins send invitations")
    handle = (body.get("user") or "").strip()
    if not handle:
        raise HTTPException(422, "Give a username or email address to invite")
    target = db.scalar(select(User).where(or_(
        func.lower(User.username) == handle.lower(),
        func.lower(User.email) == handle.lower())))
    if not target:
        raise HTTPException(404, f"No account named {handle!r}")
    if target.id == _acct_id(user_id):
        raise HTTPException(422, "You are already a member of this Pantheon")
    if db.scalar(select(PantheonMember).where(
            PantheonMember.pantheon_id == pantheon_id, PantheonMember.user_id == target.id)):
        raise HTTPException(409, f"{target.username} is already a member")
    if db.scalar(select(PantheonInvite).where(
            PantheonInvite.pantheon_id == pantheon_id, PantheonInvite.user_id == target.id)):
        raise HTTPException(409, f"{target.username} already has a pending invitation")
    db.add(PantheonInvite(pantheon_id=pantheon_id, user_id=target.id,
                          invited_by=_acct_id(user_id)))
    db.commit()
    return {"ok": True, "username": target.username}


@app.post("/api/pantheons/invites/{invite_id}/accept")
def accept_invite(invite_id: int, user_id: str = Depends(user_id_header),
                  db: Session = Depends(get_db)):
    inv = db.get(PantheonInvite, invite_id)
    if not inv or inv.user_id != _acct_id(user_id):
        raise HTTPException(404, "Invitation not found")
    pid = inv.pantheon_id
    db.add(PantheonMember(pantheon_id=pid, user_id=inv.user_id, role="member"))
    db.delete(inv)
    db.commit()
    return {"ok": True, "pantheon_id": pid}


@app.post("/api/pantheons/invites/{invite_id}/decline")
def decline_invite(invite_id: int, user_id: str = Depends(user_id_header),
                   db: Session = Depends(get_db)):
    inv = db.get(PantheonInvite, invite_id)
    if not inv or inv.user_id != _acct_id(user_id):
        raise HTTPException(404, "Invitation not found")
    db.delete(inv)
    db.commit()
    return {"ok": True}


@app.post("/api/pantheons/{pantheon_id}/join")
def join_pantheon(pantheon_id: int, user_id: str = Depends(user_id_header),
                  db: Session = Depends(get_db)):
    pantheon = db.get(Pantheon, pantheon_id)
    if not pantheon:
        raise HTTPException(404, "Pantheon not found")
    if pantheon.visibility != "public":
        raise HTTPException(403, "This Pantheon is private — you need an invitation")
    uid = _acct_id(user_id)
    if _membership(db, pantheon_id, user_id):
        raise HTTPException(409, "You are already a member of this Pantheon")
    db.execute(sa_delete(PantheonInvite).where(  # a pending invite is now moot
        PantheonInvite.pantheon_id == pantheon_id, PantheonInvite.user_id == uid))
    db.add(PantheonMember(pantheon_id=pantheon_id, user_id=uid, role="member"))
    db.commit()
    return {"ok": True}


@app.post("/api/pantheons/{pantheon_id}/leave")
def leave_pantheon(pantheon_id: int, user_id: str = Depends(user_id_header),
                   db: Session = Depends(get_db)):
    _, member = _require_membership(db, pantheon_id, user_id)
    if member.role == "owner":
        raise HTTPException(422, "The owner cannot leave — delete the Pantheon "
                                 "or promote another owner first")
    db.delete(member)
    db.commit()
    return {"ok": True}


@app.delete("/api/pantheons/{pantheon_id}/members/{member_uid}", status_code=204)
def remove_member(pantheon_id: int, member_uid: int,
                  user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    _, actor = _require_membership(db, pantheon_id, user_id)
    if actor.role not in ("owner", "admin"):
        raise HTTPException(403, "Only the owner or an admin can remove members")
    target = db.scalar(select(PantheonMember).where(
        PantheonMember.pantheon_id == pantheon_id, PantheonMember.user_id == member_uid))
    if not target:
        raise HTTPException(404, "Member not found")
    if target.role == "owner":
        raise HTTPException(403, "The owner cannot be removed")
    if target.role == "admin" and actor.role != "owner":
        raise HTTPException(403, "Only the owner can remove an admin")
    db.delete(target)
    db.commit()


@app.post("/api/pantheons/{pantheon_id}/members/{member_uid}/role")
def set_member_role(pantheon_id: int, member_uid: int, body: dict,
                    user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    pantheon, actor = _require_membership(db, pantheon_id, user_id)
    if actor.role != "owner":
        raise HTTPException(403, "Only the owner can change roles")
    role = body.get("role")
    target = db.scalar(select(PantheonMember).where(
        PantheonMember.pantheon_id == pantheon_id, PantheonMember.user_id == member_uid))
    if not target:
        raise HTTPException(404, "Member not found")
    if role == "owner":
        # ownership transfer: the current owner steps down to admin
        actor.role = "admin"
        target.role = "owner"
        pantheon.owner_id = target.user_id
    elif role in ("admin", "member"):
        if target.role == "owner":
            raise HTTPException(403, "Transfer ownership by promoting someone to owner")
        target.role = role
    else:
        raise HTTPException(422, "Role must be owner, admin, or member")
    db.commit()
    return {"ok": True}


@app.get("/api/pantheons/{pantheon_id}/feeds")
def pantheon_feeds(pantheon_id: int, user_id: str = Depends(user_id_header),
                   db: Session = Depends(get_db)):
    _, member = _require_membership(db, pantheon_id, user_id)
    feeds = db.scalars(select(Feed).where(Feed.pantheon_id == pantheon_id)
                       .order_by(Feed.position, Feed.id)).all()
    return [{
        "id": f.id, "name": f.name, "criteria": f.criteria, "sort": f.sort,
        "position": f.position, "width": f.width, "group_events": f.group_events,
        "pantheon_id": pantheon_id, "shared_by": f.shared_by,
        "can_edit": f.user_id == user_id or member.role in ("owner", "admin"),
    } for f in feeds]


def _share_copy(db, item, model, pantheon_id: int, user_id: str):
    """Common guts of sharing a feed/alert into a Pantheon."""
    pantheon, member = _require_membership(db, pantheon_id, user_id)
    if not _pantheon_allows(pantheon, member, "share"):
        raise HTTPException(403, "This Pantheon lets only admins share items")
    me = db.get(User, _acct_id(user_id))
    existing = db.scalar(select(model).where(
        model.pantheon_id == pantheon_id, model.name == item.name,
        model.user_id == user_id))
    if existing:
        raise HTTPException(409, f"You already shared “{item.name}” with this Pantheon")
    return member, (me.username if me else "")


@app.post("/api/feeds/{feed_id}/share", status_code=201)
def share_feed(feed_id: int, body: dict, user_id: str = Depends(user_id_header),
               db: Session = Depends(get_db)):
    feed = _owned(db, Feed, feed_id, user_id)
    if feed.pantheon_id is not None:
        raise HTTPException(422, "This feed is already a shared copy")
    pantheon_id = int(body.get("pantheon_id") or 0)
    _, username = _share_copy(db, feed, Feed, pantheon_id, user_id)
    max_pos = db.scalar(select(func.max(Feed.position)).where(Feed.pantheon_id == pantheon_id))
    copy = Feed(user_id=user_id, pantheon_id=pantheon_id, shared_by=username,
                name=feed.name, criteria=dict(feed.criteria), sort=feed.sort,
                group_events=feed.group_events, width=1,
                position=(max_pos + 1) if max_pos is not None else 0)
    db.add(copy)
    db.commit()
    return {"id": copy.id}


@app.post("/api/alerts/{alert_id}/share", status_code=201)
def share_alert(alert_id: int, body: dict, user_id: str = Depends(user_id_header),
                db: Session = Depends(get_db)):
    alert = _owned(db, Alert, alert_id, user_id)
    if alert.pantheon_id is not None:
        raise HTTPException(422, "This alert is already a shared copy")
    pantheon_id = int(body.get("pantheon_id") or 0)
    _, username = _share_copy(db, alert, Alert, pantheon_id, user_id)
    copy = Alert(user_id=user_id, pantheon_id=pantheon_id, shared_by=username,
                 name=alert.name, criteria=dict(alert.criteria), active=True)
    db.add(copy)
    db.commit()
    return {"id": copy.id}


# ---------- ingest control & live stream ----------

@app.post("/api/ingest/run")
async def ingest_run(request: Request):
    """Poll every due source now — the ⟳ Refresh in Troubleshooting.

    Deliberately not operator-only: it is the one thing a reader can do when a
    feed looks stale, and one cycle runs at a time, so it cannot pile up. It is
    rate-limited instead, because a caller looping on it could keep the cycle
    permanently busy and starve the scheduled polling behind it.
    """
    ratelimit.check("ingest", request)
    if ingest.cycle_lock.locked():
        raise HTTPException(
            409, "A poll cycle is already running — new articles will appear when it finishes.")
    try:
        result = await ingest.run_ingest_cycle()
        # Pressing Refresh means "show me current news", so Home's shared
        # columns are re-matched before the answer goes back, rather than on
        # the poller's own schedule.
        await ingest.warm_home()
        return result
    except Exception as exc:
        ingest.status["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
        raise HTTPException(500, f"Ingest cycle failed: {type(exc).__name__}: {exc}")


def _reach_status(db: Session) -> dict:
    """The two numbers that decide whether Delphi needs a new way in.

    Both answer questions that were otherwise a matter of opinion. How many
    publishers Delphi has met that have no feed at all is the exact size of what
    another intake path — sitemaps, an API — would be for; if it is small, there
    is nothing to build. And how many articles are queued for a body says
    whether fetching them is the binding constraint, because an article without
    one matches on its headline alone.
    """
    from datetime import timedelta

    by_status = dict(db.execute(
        select(DiscoveredDomain.status, func.count(DiscoveredDomain.id))
        .group_by(DiscoveredDomain.status)).all())
    # The domains themselves, newest first: a list of outlets nobody is reading
    # in full is worth looking at, not just counting.
    feedless = list(db.scalars(
        select(DiscoveredDomain.domain)
        .where(DiscoveredDomain.status == "no-feed")
        .order_by(DiscoveredDomain.checked_at.desc()).limit(12)))

    window = utcnow() - timedelta(hours=ingest.BACKFILL_HOURS)
    waiting = db.scalar(
        select(func.count(Article.id)).join(Source, Source.id == Article.source_id)
        .where(Article.content == "", Article.published_at >= window,
               Source.paywall.is_(False))) or 0
    untried = db.scalar(
        select(func.count(Article.id)).join(Source, Source.id == Article.source_id)
        .where(Article.content == "", Article.published_at >= window,
               Source.paywall.is_(False), Article.content_tried_at.is_(None))) or 0
    # Which sources are actually carrying their weight. A catalog that grows by
    # itself is easy to mistake for a catalog that works: a thousand outlets
    # reads like breadth, but a source that has been polled and has never once
    # produced an article is a row in a table, not a source of news. "Silent"
    # counts exactly those — polled at least once, nothing to show for it.
    total_sources = db.scalar(select(func.count(Source.id))) or 0
    with_articles = db.scalar(
        select(func.count(func.distinct(Article.source_id)))) or 0
    polled = db.scalar(select(func.count(Source.id)).where(
        Source.last_fetched_at.is_not(None))) or 0
    # Only the ones still in the rotation. A retired source has also produced
    # nothing, but it is already reported as retired and nothing more needs
    # doing about it; "silent" is meant to be the list worth acting on.
    silent = db.scalar(select(func.count(Source.id)).where(
        Source.enabled.is_(True),
        Source.last_fetched_at.is_not(None),
        ~select(Article.id).where(Article.source_id == Source.id).exists())) or 0
    return {
        "sources": {
            "total": total_sources,
            "enabled": db.scalar(select(func.count(Source.id)).where(
                Source.enabled.is_(True))) or 0,
            "never_polled": total_sources - polled,
            "producing": with_articles,
            "silent": silent,
            "retired": db.scalar(select(func.count(Source.id)).where(
                Source.enabled.is_(False),
                Source.last_status.like("retired:%"))) or 0,
            "removed_since_start": ingest.status.get("removed_total", 0),
            "remove_after": ingest.REMOVE_AFTER,
        },
        "discovery": {
            "sources_found": db.scalar(select(func.count(Source.id)).where(
                Source.added_by == "auto-discovered")) or 0,
            "domains_probed": sum(by_status.values()),
            "with_a_feed": by_status.get("added", 0),
            "without_a_feed": by_status.get("no-feed", 0),
            "feedless_examples": feedless,
        },
        "content": {
            "waiting_for_a_body": waiting,
            "never_tried": untried,
            "tried_and_failed": waiting - untried,
            "per_cycle": ingest.CONTENT_MAX_PER_CYCLE,
            "window_hours": ingest.BACKFILL_HOURS,
        },
    }


@app.get("/api/ingest/status")
def ingest_status(db: Session = Depends(get_db)):
    # The warm Home board goes with it: both are the poller's work, and an
    # operator looking at one wants to know the state of the other. The address
    # lookup is here too, so "no address suggestions" can be told apart from
    # "nobody searched for one" without reading the log.
    return {**ingest.status, "home": home.status(),
            # Every moment this process could not answer, newest first, with
            # what it was doing. The reason it is here rather than only in the
            # log: the log holds about a minute, and by the time anybody looks
            # at an outage it has scrolled away.
            "stalls": watchdog.stalls(),
            "worst_stall_s": watchdog.worst(),
            "doing": watchdog.activity(),
            # So a reader quoting "reference bd0de7" can be answered, rather
            # than the reference naming a log line that expired minutes later.
            "failures": list(recent_failures),
            "geocoder": {"provider": geocode.PROVIDER if geocode.enabled() else "off",
                         **geocode.status},
            # Mail is sent in the background so a reader can't time the reply
            # to learn whether an address exists; the price is that a broken
            # relay is invisible to everyone. This is where it becomes visible.
            "mail": {"configured": mailer.enabled(),
                     "host": f"{mailer.HOST}:{mailer.PORT}" if mailer.enabled() else "",
                     **mailer.status},
            # Fly's nightly volume snapshots are off, so this is the only
            # backup there is. A backup that silently stopped is the failure
            # mode of every backup that has ever mattered, so the date of the
            # last one is reported rather than assumed.
            "account_backup": {
                "every_s": accounts_backup.EVERY_SECONDS,
                "recipients": len(accounts_backup.recipients(db)),
                **accounts_backup.status},
            # The disk. Delphi filled its volume once and the only symptom was
            # that the app stopped starting: SQLite could not enable WAL, so it
            # never opened a port and there was nothing left running to report
            # it. A number here is the difference between noticing at 70% and
            # finding out at 100%.
            "storage": {**storage.disk(),
                        "ceiling_bytes": storage.db_ceiling(),
                        "over_ceiling_bytes": storage.over_ceiling(),
                        "reclaimable": storage.auto_vacuum_mode() == 2},
            **_reach_status(db)}


@app.post("/api/maintenance/fetch-content")
async def fetch_content_backfill(body: dict | None = None,
                                 admin: User = Depends(require_admin),
                                 db: Session = Depends(get_db)):
    """Fetch article bodies for recent stored articles that don't have one yet
    (feed criteria then match against full text). Use after upgrading, or to
    deepen coverage of a busy window."""
    from datetime import timedelta
    body = body or {}
    hours = float(body.get("hours", 48))
    limit = min(int(body.get("limit", 300)), 1000)
    candidates = db.scalars(
        select(Article).where(
            Article.content == "",
            Article.published_at >= utcnow() - timedelta(hours=hours))
        .order_by(Article.published_at.desc()).limit(limit)
    ).all()
    fetched = await ingest.enrich_with_content(db, candidates, cap=limit)
    return {"candidates": len(candidates), "content_fetched": fetched}


@app.post("/api/maintenance/reclaim-space")
async def reclaim_space(admin: User = Depends(require_admin)):
    """Convert the database so deleted news gives its disk space back.

    Operator-only and deliberately manual. It runs a VACUUM, which rewrites the
    whole database while holding a lock that blocks readers as well as writers
    — minutes of a stalled site on a large archive. This shipped running
    automatically on the first poll after a deploy, which is the same work with
    nobody told and no moment chosen; that was a mistake and this is it fixed.

    Needed once, and only for a database created before Delphi started making
    them this way. Afterwards freed pages return to the disk as pruning runs,
    with no lock and no downtime.
    """
    before = storage.db_bytes()
    result = await asyncio.to_thread(storage.ensure_incremental_vacuum)
    freed = max(0, before - storage.db_bytes())
    return {**result, "freed_bytes": freed, "db_bytes": storage.db_bytes(),
            "mode": storage.auto_vacuum_mode()}


@app.post("/api/maintenance/audit-sources")
async def audit_sources(admin: User = Depends(require_admin),
                        db: Session = Depends(get_db)):
    """Re-check the catalog against the current adoption rules, now.

    The same sweep the poller runs daily and on every restart. Here so an
    operator who has just recognised something that should never have been
    adopted does not have to wait a day to see it acted on.

    Disables; never deletes. The source stays visible in the Sources panel
    saying why it was switched off, and anything a person added by hand is
    left alone.
    """
    result = await asyncio.to_thread(discovery.audit_catalog, db)
    return result


@app.post("/api/maintenance/reclassify")
def reclassify_articles(admin: User = Depends(require_admin),
                        db: Session = Depends(get_db)):
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


@app.post("/api/maintenance/detect-languages")
def detect_languages(admin: User = Depends(require_admin),
                     db: Session = Depends(get_db)):
    """Re-detect the language of every stored article from its text (use after
    the detector improves, or to fix a backlog tagged with the source's
    language). Corrected articles then auto-translate for users whose reading
    language differs."""
    changed = total = 0
    for article in db.scalars(select(Article)):
        total += 1
        text = f"{article.title}\n{article.summary}\n{article.content or ''}"
        lang = langdetect.detect(text, article.language or "en")
        if lang != article.language:
            article.language = lang
            changed += 1
    db.commit()
    return {"articles": total, "relabeled": changed}


@app.post("/api/events/rebuild")
def events_rebuild(admin: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    """Recluster every stored article from scratch."""
    return {"events": rebuild_events(db)}


def _story_related(db: Session, event: Event) -> list[dict]:
    """Events adjacent to this one: similar headlines first, then same region."""
    from datetime import timedelta

    from .scoring import tokens_similarity

    def brief(e: Event, why: str) -> dict:
        return {"id": e.id, "title": e.title, "importance": e.importance,
                "article_count": e.article_count,
                "updated_at": e.updated_at.isoformat() + "Z", "why": why}

    recent = db.scalars(select(Event).where(
        Event.updated_at >= utcnow() - timedelta(days=14), Event.id != event.id
    ).order_by(Event.updated_at.desc()).limit(500)).all()
    scored = sorted(
        ((tokens_similarity(event.cluster_tokens, e.cluster_tokens), e) for e in recent),
        key=lambda pair: -pair[0],
    )
    related = [brief(e, "similar story") for sim, e in scored[:5] if sim >= 0.2]
    if len(related) < 5 and (event.countries or []):
        have = {r["id"] for r in related}
        same_place = [e for _, e in scored
                      if e.id not in have and set(e.countries or []) & set(event.countries)]
        same_place.sort(key=lambda e: -e.importance)
        related += [brief(e, "same region") for e in same_place[:5 - len(related)]]
    return related


async def _story(db: Session, article: Article, lang: str, user_id: str) -> dict:
    """One story, centred on one report of it.

    A story with a single report and a story carried by forty outlets are the
    same thing seen from different distances, so they are one payload: the
    report a reader picked, and — when other outlets have it — the whole event
    around it. `articles` is empty for an unclustered report, which is simply a
    story nobody else has yet."""
    event = db.get(Event, article.event_id) if article.event_id else None
    articles: list[Article] = []
    sources: list[dict] = []
    related: list[dict] = []
    if event:
        articles = db.scalars(
            select(Article).where(Article.event_id == event.id)
            .order_by(Article.published_at.desc()).limit(STORY_TIMELINE_MAX)
        ).all()
        seen = set()
        for a in articles:
            if a.source and a.source.id not in seen:
                seen.add(a.source.id)
                sources.append({"id": a.source.id, "name": a.source.name,
                                "platform": a.source.platform or "news",
                                "country": a.source.country})
        related = _story_related(db, event)

    # The focused report is translated with the rest, so the timeline and the
    # headline at the top can never disagree about what it says.
    tr = await translate.translate_articles(db, articles or [article], lang)
    viewed = _viewed_events(db, user_id, [article])
    read_alone = _viewed_articles(db, user_id, [article])
    focus = _article_json(article, tr, viewed, None)

    # A column truncates the summary to 400 characters because a column is
    # narrow; here there is room for the publisher's summary in full.
    t = (tr or {}).get(article.id)
    focus["summary"] = t["summary"] if t else (article.summary or "")

    # An extract of the story body, which is fetched for matching anyway. It is
    # deliberately bounded and always shown beside a link to the outlet: a
    # reading aid for deciding whether to go there, not a copy of the article.
    body = (article.content or "").strip()
    focus["excerpt"] = body[:ARTICLE_EXCERPT_CHARS]
    focus["excerpt_truncated"] = len(body) > ARTICLE_EXCERPT_CHARS
    focus["fetched_at"] = article.fetched_at.isoformat() + "Z" if article.fetched_at else None
    if article.source:
        focus["source"] = {**focus["source"], "platform": article.source.platform or "news",
                           "language": article.source.language,
                           "homepage": article.source.homepage or ""}

    return {
        "article": focus,
        "event": {
            "id": event.id,
            "title": event.title,
            "importance": event.importance,
            # Counted from the timeline the reader can actually see, so the
            # badge can never claim fewer reports than outlets. The stored
            # count only stands in when the timeline was truncated.
            "article_count": (event.article_count if len(articles) >= STORY_TIMELINE_MAX
                              else len(articles)),
            "source_count": len(sources),
            "countries": event.countries or [],
            "categories": event.categories or [],
            "first_seen": event.first_seen.isoformat() + "Z",
            "updated_at": event.updated_at.isoformat() + "Z",
        } if event else None,
        "articles": [_article_json(a, tr, viewed, None, read_alone) for a in articles],
        "sources": sources,
        "related": related,
    }


@app.get("/api/story/{article_id}")
async def story(article_id: int, lang: str = Query(default=""),
                user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    """The focused view, opened at one report."""
    article = db.get(Article, article_id)
    if not article:
        raise HTTPException(404, "Article not found")
    return await _story(db, article, lang, user_id)


@app.get("/api/story/{article_id}/export")
async def export_story(article_id: int, request: Request,
                       fmt: str = Query(default="docx", alias="format"),
                       lang: str = Query(default=""),
                       user_id: str = Depends(user_id_header),
                       db: Session = Depends(get_db)):
    """A focused story as a file: the report, and every outlet carrying it.

    The reason to export a Focus rather than a column is that it is already the
    unit somebody wants to hand on — one thing that happened, with the coverage
    of it gathered. A column is a standing interest; a Focus is a finding.

    The picked report leads, whatever its timestamp, because it is the one the
    reader chose; the rest of the event follows newest-first. An unclustered
    report exports as itself, which is simply a story nobody else has yet.

    Defaults to docx rather than csv — the other exports feed a spreadsheet,
    this one gets read.
    """
    ratelimit.check("export", request)
    article = db.get(Article, article_id)
    if not article:
        raise HTTPException(404, "Article not found")

    reports = [article]
    if article.event_id:
        rest = db.scalars(
            select(Article).where(Article.event_id == article.event_id)
            .order_by(Article.published_at.desc()).limit(STORY_TIMELINE_MAX)).all()
        reports += [a for a in rest if a.id != article.id]

    tr = await translate.translate_articles(db, reports, lang)
    name = ((tr.get(article.id, {}).get("title") or article.title) or "Story")[:120]
    return _export_response(fmt, name, _export_rows(reports, tr))


@app.get("/api/story/by-event/{event_id}")
async def story_by_event(event_id: int, lang: str = Query(default=""),
                         user_id: str = Depends(user_id_header),
                         db: Session = Depends(get_db)):
    """The same view, opened at an event rather than a report — from a grouped
    card or a related-event link. It centres on the latest report, which is what
    a reader arriving at a story wants first."""
    if not db.get(Event, event_id):
        raise HTTPException(404, "Event not found")
    article = db.scalars(
        select(Article).where(Article.event_id == event_id)
        .order_by(Article.published_at.desc()).limit(1)
    ).first()
    if not article:
        raise HTTPException(404, "That event has no reports left")
    return await _story(db, article, lang, user_id)


@app.post("/api/stream/ticket")
def stream_ticket(user_id: str = Depends(user_id_header)):
    """A one-minute credential for opening the event stream.

    Reached with the Authorization header like everything else; the ticket it
    returns is what goes in the stream URL, because EventSource cannot set
    headers and anything in a URL ends up in access logs. A minute is long
    enough to open a connection on a slow phone and short enough that a logged
    copy is worthless — and it opens the stream and nothing else.
    """
    return {"ticket": auth.make_stream_ticket(_acct_id(user_id)),
            "expires_in": auth.STREAM_TICKET_TTL_SECONDS}


@app.get("/api/stream")
async def stream():
    """Server-Sent Events: new-article batches and alert hits, pushed live.

    Authenticated by the ?ticket= the middleware checks, not a session token.
    """
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


def _db_readable() -> tuple[bool, str]:
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        return True, ""
    except Exception as exc:
        return False, f"database unreadable: {type(exc).__name__}"


# The last answer the database gave, and when. Health checks arrive every 30s
# and the answer changes rarely, so it is remembered between them.
_DB_STATE: tuple[float, bool, str] = (0.0, True, "")
HEALTH_RECHECK_S = 5.0
HEALTH_PROBE_TIMEOUT_S = 3.0


@app.get("/healthz")
async def healthz():
    """Is this process actually able to serve? For Fly's health check.

    Outside /api on purpose, so it needs no session — the checker has none.

    It answers on exactly one question: can the database be read. That is what
    was wrong when the app died, and it is the only condition where restarting
    is the right response.

    *Busy is not broken.* This used to be a plain sync endpoint, so it ran in
    the same worker pool as everything else and queued behind whatever was in
    it. Right after a deploy every source is overdue at once, and catching up —
    a thousand new articles scored, indexed and clustered — saturates two
    shared vCPUs for minutes. The check then failed to answer inside its 10s
    timeout, Fly stopped routing to the only machine, and the site was
    unreachable with nothing actually wrong with it. That is the outage this
    was written to prevent, caused by the check itself.

    So the probe is bounded, and a probe that runs out of time leaves the
    previous answer standing. An unreadable database raises immediately —
    SQLite does not sit there thinking about it — so a *timeout* means the
    machine is busy and a restart would only mean another cold start into the
    same catch-up. An *error* still fails the check, at once.

    Deliberately *not* unhealthy when the disk is nearly full. The app pauses
    ingestion and prunes its way back down in that state, which needs it to
    stay running; failing the check would restart it into the same condition
    repeatedly and reach the max-restart limit — turning a recoverable squeeze
    into the outage it is meant to prevent. Storage is reported through
    /api/ingest/status and the operator console instead, where it informs
    somebody rather than triggering something.
    """
    global _DB_STATE
    checked_at, ok, detail = _DB_STATE
    if time.monotonic() - checked_at >= HEALTH_RECHECK_S:
        try:
            ok, detail = await asyncio.wait_for(
                run_in_threadpool(_db_readable), HEALTH_PROBE_TIMEOUT_S)
            _DB_STATE = (time.monotonic(), ok, detail)
        except asyncio.TimeoutError:
            pass          # too busy to ask; the last answer still stands
    if not ok:
        return JSONResponse({"ok": False, "detail": detail}, status_code=503)
    return {"ok": True}


# ---------- helpers & static frontend ----------

def _owned(db: Session, model, obj_id: int, user_id: str):
    obj = db.get(model, obj_id)
    if not obj or obj.user_id != user_id:
        raise HTTPException(404, f"{model.__name__} not found")
    return obj


def _acct_id(user_id: str) -> int:
    return int(user_id.split(":", 1)[1])


def _membership(db: Session, pantheon_id: int, user_id: str) -> PantheonMember | None:
    return db.scalar(select(PantheonMember).where(
        PantheonMember.pantheon_id == pantheon_id,
        PantheonMember.user_id == _acct_id(user_id)))


def _require_membership(db: Session, pantheon_id: int, user_id: str):
    pantheon = db.get(Pantheon, pantheon_id)
    if not pantheon:
        raise HTTPException(404, "Pantheon not found")
    member = _membership(db, pantheon_id, user_id)
    if not member:
        raise HTTPException(403, "You are not a member of this Pantheon")
    return pantheon, member


def _pantheon_allows(pantheon: Pantheon, member: PantheonMember, action: str) -> bool:
    """Settings-gated actions ("invite", "share"): owner/admins always may;
    plain members only when the Pantheon's setting says "members"."""
    if member.role in ("owner", "admin"):
        return True
    return (pantheon.settings or {}).get(f"who_can_{action}", "members") == "members"


def _can_manage_shared(db: Session, item, user_id: str) -> bool:
    """Edit/delete rights on a Pantheon-shared feed/alert: its sharer, or a
    Pantheon owner/admin."""
    if item.user_id == user_id:
        return True
    member = _membership(db, item.pantheon_id, user_id)
    return bool(member and member.role in ("owner", "admin"))


def _shared_item_for_read(db: Session, model, obj_id: int, user_id: str):
    """A feed/alert the caller may VIEW: their own, or shared with a Pantheon
    they belong to."""
    obj = db.get(model, obj_id)
    if not obj:
        raise HTTPException(404, f"{model.__name__} not found")
    if obj.pantheon_id is None:
        if obj.user_id != user_id:
            raise HTTPException(404, f"{model.__name__} not found")
    elif not _membership(db, obj.pantheon_id, user_id):
        raise HTTPException(403, "You are not a member of this Pantheon")
    return obj


def _shared_item_for_edit(db: Session, model, obj_id: int, user_id: str):
    """A feed/alert the caller may CHANGE: their own, or a Pantheon item they
    shared themselves / administer."""
    obj = db.get(model, obj_id)
    if not obj:
        raise HTTPException(404, f"{model.__name__} not found")
    if obj.pantheon_id is None:
        if obj.user_id != user_id:
            raise HTTPException(404, f"{model.__name__} not found")
    elif not _can_manage_shared(db, obj, user_id):
        raise HTTPException(403, "Only the sharer or a Pantheon admin can change this")
    return obj


def _google_queries(criteria) -> list[str]:
    """Convert feed criteria into Google News search strings — one per
    boolean query (each runs as its own tracker), or one from keywords."""
    import re as _re

    def convert(q: str) -> str:
        q = normalize_quotes(q)
        q = _re.sub(r"[()]", " ", q)
        q = _re.sub(r"\bAND\b", " ", q, flags=_re.IGNORECASE)
        q = _re.sub(r"\bNOT\s+", "-", q, flags=_re.IGNORECASE)
        return _re.sub(r"\s+", " ", q).strip()[:200]

    raw = [q for q in [criteria.query, *criteria.queries] if q and q.strip()]
    if not raw:
        kw = " OR ".join(k for k in criteria.keywords if k.strip())
        raw = [kw] if kw else []
    return [c for c in (convert(q) for q in raw[:5]) if c]


def _ensure_coverage_source(db: Session, criteria) -> None:
    """Create (idempotently) Google News tracker sources for the criteria's
    queries/keywords when auto_coverage is on."""
    if not getattr(criteria, "auto_coverage", False):
        return
    for q in _google_queries(criteria):
        rss = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
               + "&hl=en-US&gl=US&ceid=US:en")
        if db.scalar(select(Source).where(Source.rss_url == rss)):
            continue
        db.add(Source(
            name=f"Topic: {q}", rss_url=rss, homepage="https://news.google.com",
            country="", region="Global", language="en", scope="international",
            categories=[], tier=2, added_by="topic-tracker",
        ))


def _reject_invalid_query(criteria):
    """Validate the legacy single query and every entry in queries[]."""
    for q in [criteria.query, *criteria.queries]:
        if q and q.strip():
            err = validate_query(q)
            if err:
                raise HTTPException(422, f"Invalid boolean query “{q}”: {err}")


# ---------- favourite locations ----------

@app.get("/api/geo/search")
async def geo_search(q: str = Query(default=""), request: Request = None,
                     user_id: str = Depends(user_id_header)):
    """Place lookup for the location picker.

    The bundled gazetteer answers first: instant, offline, and nothing leaves
    this server. It knows about 480 cities and 154 countries, though, so a
    street address, a town or a district finds nothing in it — those come from
    an address lookup, appended below the local matches and marked as such.
    Delphi's server makes that call, never the reader's browser."""
    local = [{**hit, "source": "local"} for hit in search_places(q)]
    if not geocode.enabled():
        return {"results": local, "attribution": ""}

    # Only when the gazetteer has no good answer. A reader typing "tok" wants
    # Tokyo, and that is a prefix match, so nothing leaves the server; "1600
    # Pennsylvania Ave" matches nothing here, and that is what a lookup is for.
    # Rate-limited per caller, since it spends somebody else's quota.
    strong = any(h.get("match") in ("exact", "prefix") for h in local)
    remote = []
    if not strong and len((q or "").strip()) >= 3:
        if request is not None:
            ratelimit.check("geocode", request)
        remote = await geocode.search(q)
        seen = {(round(h["lat"], 3), round(h["lon"], 3)) for h in local}
        remote = [h for h in remote
                  if (round(h["lat"], 3), round(h["lon"], 3)) not in seen]
    return {"results": local + remote,
            "attribution": geocode.ATTRIBUTION if remote else ""}


def _location_json(loc: FavoriteLocation, mine: bool = True) -> dict:
    return {
        "id": loc.id, "name": loc.name,
        # What it was picked as, so editing round-trips it rather than losing
        # the searchable name the moment somebody renames their own label.
        "place_name": loc.place_name or "", "country": loc.country or "",
        "lat": loc.lat, "lon": loc.lon, "radius_km": loc.radius_km,
        "color": loc.color, "feed_id": loc.feed_id,
        # Whether this place has news of its own being gathered, or is only
        # filtering what the catalog happens to bring in.
        "has_source": bool(loc.source_id),
        "pantheon_id": loc.pantheon_id, "shared_by": loc.shared_by,
        "mine": mine,
    }


def _visible_locations(db: Session, user_id: str) -> list[FavoriteLocation]:
    """The caller's own locations plus any shared into a Pantheon they're in."""
    pantheon_ids = list(db.scalars(select(PantheonMember.pantheon_id).where(
        PantheonMember.user_id == _acct_id(user_id))))
    clause = FavoriteLocation.user_id == user_id
    if pantheon_ids:
        clause = or_(clause, FavoriteLocation.pantheon_id.in_(pantheon_ids))
    return list(db.scalars(select(FavoriteLocation).where(clause)
                           .order_by(FavoriteLocation.name)))


def _location_circle(loc: FavoriteLocation) -> dict:
    """The watched area, with the name it was picked under.

    The name is not decoration. An article is only given coordinates when its
    text names one of the 577 cities in the bundled gazetteer, so a circle
    around anywhere else could never contain anything — while the picker
    happily offers any address OpenStreetMap knows. Carrying the name lets the
    matcher recognise "Reading" in a headline without Delphi needing to hold a
    coordinate for Reading.
    """
    return {"type": "Circle", "center": [loc.lat, loc.lon], "radius_km": loc.radius_km,
            "name": loc.place_name or "", "country": (loc.country or "").upper()}


def _location_query(loc: FavoriteLocation) -> str:
    """What to ask a news search for, to actually gather coverage of a place.

    The place's own name, never the reader's label for it: "Dad's house" is a
    perfectly good thing to call a location and a useless thing to search for.
    """
    return (loc.place_name or "").strip()


def _sync_location_source(db: Session, loc: FavoriteLocation) -> None:
    """Keep a news-search source pointed at this location, and only this one.

    Saving a location used to create a filter and nothing else, so it could
    only ever surface news the catalog had already gathered for other reasons.
    For anywhere without its own local outlet in the catalog — which is most
    places — that is an empty column by construction. This gives every watched
    place a source of its own, the way a topic tracker does.

    Moved and removed rather than accumulated: the source id lives on the
    location, so renaming edits that source instead of minting a second one,
    and deleting takes it away. A source nothing points at is still polled
    forever, which is how a catalog quietly fills with orphans.
    """
    query = _location_query(loc)
    existing = db.get(Source, loc.source_id) if loc.source_id else None

    if not query:
        # A point clicked on the map has no name to search for. Nothing to
        # gather; the circle still flags whatever the catalog brings in.
        if existing is not None and not _source_shared_with_others(db, loc):
            db.delete(existing)
        loc.source_id = None
        return

    from . import cities
    country = (loc.country or "").upper()
    lang = cities.language_for(country)
    rss = cities.search_feed_url(query, country)

    if existing is not None and existing.rss_url == rss:
        return                                        # already pointed there

    # Somebody else may already watch this place; one source serves them all.
    shared = db.scalar(select(Source).where(Source.rss_url == rss))
    if shared is not None:
        if existing is not None and existing.id != shared.id \
                and not _source_shared_with_others(db, loc):
            db.delete(existing)
        loc.source_id = shared.id
        return

    if existing is not None and not _source_shared_with_others(db, loc):
        existing.rss_url = rss                        # a rename: move it
        existing.name = f"Local: {query}"
        existing.country = country
        existing.language = lang
        return

    source = Source(name=f"Local: {query}", rss_url=rss,
                    homepage="https://news.google.com", country=country,
                    region="Local", language=lang, scope="local",
                    categories=[], tier=3, added_by="location")
    db.add(source)
    db.flush()
    loc.source_id = source.id


def _source_shared_with_others(db: Session, loc: FavoriteLocation) -> bool:
    """Is another location relying on this same source?"""
    if not loc.source_id:
        return False
    return bool(db.scalar(select(FavoriteLocation.id).where(
        FavoriteLocation.source_id == loc.source_id,
        FavoriteLocation.id != loc.id).limit(1)))


LOCATIONS_FEED_NAME = "📍 Favourite Locations"


def _sync_locations_feed(db: Session, user_id: str, keep_empty: bool = False,
                         also: int | None = None):
    """Keep one feed covering every location the account owns.

    Each location used to get a column of its own, which turned a handful of
    watched places into a board nobody could read. They now share a single feed
    whose areas are OR'd, so it carries news near any of them. Called after
    every change to a location; also folds the old per-location feeds together,
    which is what migrates an existing account the first time it is touched.
    """
    locs = list(db.scalars(select(FavoriteLocation).where(
        FavoriteLocation.user_id == user_id,
        FavoriteLocation.pantheon_id.is_(None)).order_by(FavoriteLocation.name)))

    # Any feed a location still points at is a candidate; the first survives and
    # the rest — the per-location columns from before this change — are removed.
    # `also` carries the feed of a location just deleted, which nothing points
    # at any more but which still has to be cleaned up.
    feed_ids = list(dict.fromkeys(
        [loc.feed_id for loc in locs if loc.feed_id] + ([also] if also else [])))
    feed = next((f for f in (db.get(Feed, fid) for fid in feed_ids) if f), None)
    for extra_id in feed_ids:
        if feed is not None and extra_id == feed.id:
            continue
        extra = db.get(Feed, extra_id)
        if extra is not None:
            db.delete(extra)

    if not locs:
        if feed is not None and not keep_empty:
            db.delete(feed)
        return None

    if feed is None:
        max_pos = db.scalar(select(func.max(Feed.position)).where(Feed.user_id == user_id))
        feed = Feed(user_id=user_id, name=LOCATIONS_FEED_NAME, criteria={},
                    sort="newest", position=(max_pos + 1) if max_pos is not None else 0)
        db.add(feed)
        db.flush()

    feed.name = LOCATIONS_FEED_NAME
    feed.criteria = {**(feed.criteria or {}), "geos": [_location_circle(l) for l in locs],
                     "geo": None}
    for loc in locs:
        loc.feed_id = feed.id
    return feed


def _consolidate_location_feeds(db: Session) -> int:
    """Startup migration: fold each account's per-location feeds into one.
    Idempotent — an account already holding a single feed is left alone."""
    user_ids = [u for (u,) in db.execute(
        select(FavoriteLocation.user_id).where(
            FavoriteLocation.pantheon_id.is_(None)).distinct())]
    changed = 0
    for user_id in user_ids:
        before = {loc.feed_id for loc in db.scalars(select(FavoriteLocation).where(
            FavoriteLocation.user_id == user_id,
            FavoriteLocation.pantheon_id.is_(None)))}
        feed = _sync_locations_feed(db, user_id)
        if len(before) > 1 or (feed is not None and feed.id not in before):
            changed += 1
    if changed:
        db.commit()
    return changed


@app.get("/api/locations")
def list_locations(user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    return [_location_json(loc, mine=loc.user_id == user_id)
            for loc in _visible_locations(db, user_id)]


@app.post("/api/locations", status_code=201)
def create_location(body: dict, user_id: str = Depends(user_id_header),
                    db: Session = Depends(get_db)):
    """Save a favourite location; the account's locations feed covers it."""
    name = (body.get("name") or "").strip()
    if not 1 <= len(name) <= 120:
        raise HTTPException(422, "Give the location a name")
    try:
        lat, lon = float(body.get("lat")), float(body.get("lon"))
        radius = float(body.get("radius_km") or 25)
    except (TypeError, ValueError):
        raise HTTPException(422, "A latitude, longitude, and radius are required")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(422, "That point isn't on the map")
    if not 0 < radius <= 5000:
        raise HTTPException(422, "Radius must be between 0 and 5000 km")

    # `place_name` is what the picked place is called; `name` is the reader's
    # label for it. When the point came from the search box they are usually
    # the same string, but only the first is worth searching news for — and a
    # map click has no place name at all, which is a real and supported case.
    place_name = (body.get("place_name") or "").strip()[:120]
    loc = FavoriteLocation(user_id=user_id, name=name, lat=lat, lon=lon,
                           radius_km=radius, color=(body.get("color") or "gold")[:16],
                           place_name=place_name,
                           country=(body.get("country") or "").strip().upper()[:2])
    db.add(loc)
    db.flush()
    # Gather coverage of the place, rather than only filtering for it.
    _sync_location_source(db, loc)
    # One board for every watched place, so the locations are somewhere you
    # can read as well as markers on other feeds.
    if body.get("create_feed", True):
        _sync_locations_feed(db, user_id)
    db.commit()
    return _location_json(loc)


@app.patch("/api/locations/{loc_id}")
def update_location(loc_id: int, body: dict, user_id: str = Depends(user_id_header),
                    db: Session = Depends(get_db)):
    loc = db.get(FavoriteLocation, loc_id)
    if not loc or loc.user_id != user_id:
        raise HTTPException(404, "Location not found")
    if "name" in body:
        name = (body["name"] or "").strip()
        if not 1 <= len(name) <= 120:
            raise HTTPException(422, "Give the location a name")
        loc.name = name
    for key in ("lat", "lon", "radius_km"):
        if key in body:
            try:
                setattr(loc, key, float(body[key]))
            except (TypeError, ValueError):
                raise HTTPException(422, f"{key} must be a number")
    if "color" in body:
        loc.color = (body["color"] or "gold")[:16]
    if "place_name" in body:
        loc.place_name = (body["place_name"] or "").strip()[:120]
    if "country" in body:
        loc.country = (body["country"] or "").strip().upper()[:2]
    # Move the source with it rather than leaving the old one polling forever.
    _sync_location_source(db, loc)
    # Keep the locations feed pointing at the areas it now covers.
    _sync_locations_feed(db, user_id)
    db.commit()
    return _location_json(loc)


@app.delete("/api/locations/{loc_id}", status_code=204)
def delete_location(loc_id: int, keep_feed: bool = Query(default=False),
                    user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    loc = db.get(FavoriteLocation, loc_id)
    if not loc or loc.user_id != user_id:
        raise HTTPException(404, "Location not found")
    orphan = loc.feed_id
    # Take the location's news source with it, unless somebody else's location
    # watches the same place. Left behind, it would be polled every few minutes
    # for a place nobody is watching, for as long as the server runs.
    stale_source = (db.get(Source, loc.source_id)
                    if loc.source_id and not _source_shared_with_others(db, loc) else None)
    db.delete(loc)
    db.flush()
    if stale_source is not None:
        db.delete(stale_source)
    # The feed covers whatever is left; it only goes when nothing is, and
    # keep_feed holds on to an emptied one.
    _sync_locations_feed(db, user_id, keep_empty=keep_feed, also=orphan)
    db.commit()


@app.post("/api/locations/{loc_id}/share", status_code=201)
def share_location(loc_id: int, body: dict, user_id: str = Depends(user_id_header),
                   db: Session = Depends(get_db)):
    """Copy a location into a Pantheon so it flags articles for every member."""
    loc = db.get(FavoriteLocation, loc_id)
    if not loc or loc.user_id != user_id:
        raise HTTPException(404, "Location not found")
    if loc.pantheon_id is not None:
        raise HTTPException(422, "This location is already a shared copy")
    pantheon_id = int(body.get("pantheon_id") or 0)
    pantheon, member = _require_membership(db, pantheon_id, user_id)
    if not _pantheon_allows(pantheon, member, "share"):
        raise HTTPException(403, "This Pantheon lets only admins share items")
    if db.scalar(select(FavoriteLocation).where(
            FavoriteLocation.pantheon_id == pantheon_id,
            FavoriteLocation.name == loc.name,
            FavoriteLocation.user_id == user_id)):
        raise HTTPException(409, f"You already shared “{loc.name}” with this Pantheon")
    me = db.get(User, _acct_id(user_id))
    copy = FavoriteLocation(
        user_id=user_id, pantheon_id=pantheon_id,
        shared_by=(me.username if me else ""), name=loc.name,
        lat=loc.lat, lon=loc.lon, radius_km=loc.radius_km, color=loc.color)
    db.add(copy)
    db.commit()
    return _location_json(copy)


# ---------- admin / operator console ----------

def _user_admin_json(db: Session, u: User) -> dict:
    acct = f"acct:{u.id}"
    return {
        "id": u.id, "username": u.username, "email": u.email,
        "email_verified": bool(u.email_verified),
        "disabled": bool(u.disabled),
        "is_admin": _is_admin(u),
        "config_admin": _is_configured_admin(u),  # designated in NEWS_ADMIN_USERS
        "created_at": u.created_at.isoformat() + "Z" if u.created_at else None,
        "last_seen_at": u.last_seen_at.isoformat() + "Z" if u.last_seen_at else None,
        "feeds": db.scalar(select(func.count(Feed.id)).where(Feed.user_id == acct)) or 0,
        "alerts": db.scalar(select(func.count(Alert.id)).where(Alert.user_id == acct)) or 0,
        "pantheons": db.scalar(select(func.count(PantheonMember.id)).where(
            PantheonMember.user_id == u.id)) or 0,
        # Devices the account is being used on *now* — not how many it is
        # signed in on, which a thirty-day token makes a much larger and much
        # less interesting number.
        "active_devices": devices.active_count(db, u.id),
        "known_devices": db.scalar(select(func.count(Device.id)).where(
            Device.user_id == u.id)) or 0,
        # None means "follow the server default", which is reported alongside
        # so the console can show what that currently resolves to.
        "device_limit": u.device_limit,
        "effective_device_limit": devices.limit_for(u),
    }


def _admin_count(db: Session) -> int:
    """How many accounts can currently reach the operator console (persisted
    flag or a NEWS_ADMIN_USERS designation)."""
    return sum(1 for u in db.scalars(select(User)) if _is_admin(u))


def _successor(db: Session, pantheon_id: int, leaving_user_id: int) -> PantheonMember | None:
    """The member who should inherit a Pantheon when its owner goes away:
    existing admins first (they were already trusted with it), then the
    longest-standing member. None when nobody else is left."""
    members = db.scalars(select(PantheonMember).where(
        PantheonMember.pantheon_id == pantheon_id,
        PantheonMember.user_id != leaving_user_id)).all()
    return min(members, key=lambda m: (m.role != "admin", m.joined_at, m.id), default=None)


def _close_pantheon(db: Session, p: Pantheon) -> None:
    """Delete a Pantheon that has no members left, with its shared content."""
    db.execute(sa_delete(Feed).where(Feed.pantheon_id == p.id))
    for a in db.scalars(select(Alert).where(Alert.pantheon_id == p.id)).all():
        db.delete(a)  # per-row so AlertEvent children cascade
    db.execute(sa_delete(PantheonMember).where(PantheonMember.pantheon_id == p.id))
    db.execute(sa_delete(PantheonInvite).where(PantheonInvite.pantheon_id == p.id))
    db.delete(p)


def _delete_user(db: Session, user: User) -> None:
    """Remove an account and everything personal to it, while leaving the groups
    it belonged to intact.

    Personal feeds/alerts (alert events cascade through the ORM), viewed-event
    history, memberships, and invites all go. Pantheons do not: an organization
    other people depend on should survive one member leaving, so each Pantheon
    this account owned is handed to the most senior remaining member and its
    shared content is reassigned to the new owner. Only a Pantheon with nobody
    else left in it is closed.
    """
    acct = f"acct:{user.id}"

    for p in db.scalars(select(Pantheon).where(Pantheon.owner_id == user.id)).all():
        heir = _successor(db, p.id, user.id)
        if heir is None:
            _close_pantheon(db, p)
        else:
            p.owner_id = heir.user_id
            heir.role = "owner"
    db.flush()  # ownership must be settled before content is reassigned below

    # Content this account shared into a Pantheon belongs to the group, not to
    # the departing member: hand it to that Pantheon's (possibly new) owner so
    # the shared board keeps working. Anything whose Pantheon was just closed
    # falls through to the personal-content deletion below.
    for feed in db.scalars(select(Feed).where(
            Feed.user_id == acct, Feed.pantheon_id.isnot(None))).all():
        p = db.get(Pantheon, feed.pantheon_id)
        if p:
            feed.user_id = f"acct:{p.owner_id}"
    for alert in db.scalars(select(Alert).where(
            Alert.user_id == acct, Alert.pantheon_id.isnot(None))).all():
        p = db.get(Pantheon, alert.pantheon_id)
        if p:
            alert.user_id = f"acct:{p.owner_id}"
            # Out-of-app delivery pointed at the departing member — an inherited
            # alert must not start emailing or POSTing to someone who never
            # configured it. The alert keeps firing in-app for the group.
            alert.notify_email = False
            alert.webhook_url = ""
    db.flush()

    db.execute(sa_delete(PantheonMember).where(PantheonMember.user_id == user.id))
    db.execute(sa_delete(PantheonInvite).where(
        or_(PantheonInvite.user_id == user.id, PantheonInvite.invited_by == user.id)))
    db.execute(sa_delete(Feed).where(Feed.user_id == acct))
    for a in db.scalars(select(Alert).where(Alert.user_id == acct)).all():
        db.delete(a)
    db.execute(sa_delete(ViewedEvent).where(ViewedEvent.user_id == acct))
    db.delete(user)


def _admin_target(db: Session, uid: int) -> User:
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "No such account")
    return u


@app.get("/api/admin/users")
def admin_list_users(admin: User = Depends(require_admin), db: Session = Depends(get_db),
                     q: str = Query(default="")):
    """Every account, newest first, with per-account content counts. Optional
    ?q= substring-filters username/email."""
    stmt = select(User).order_by(User.created_at.desc())
    term = q.strip().lower()
    if term:
        like = f"%{term}%"
        stmt = stmt.where(or_(func.lower(User.username).like(like),
                              func.lower(User.email).like(like)))
    users = db.scalars(stmt.limit(500)).all()
    return {"users": [_user_admin_json(db, u) for u in users],
            "admin_count": _admin_count(db), "me": admin.id,
            "device_default_limit": devices.DEFAULT_LIMIT,
            "device_active_window_s": devices.ACTIVE_WINDOW_S}


@app.get("/api/admin/users/{uid}/devices")
def admin_list_devices(uid: int, admin: User = Depends(require_admin),
                       db: Session = Depends(get_db)):
    """What an account is being used on, and what those things are.

    Every device ever seen, in-use ones first, each saying whether it is in use
    now. Showing only the active ones would answer the count but not the
    question behind it — an operator looking at "3 devices" wants to know
    whether that is a phone, a laptop and a tablet, or the same laptop counted
    three times because its browser storage keeps being cleared.
    """
    user = _admin_target(db, uid)
    rows = devices.all_devices(db, user.id)
    return {
        "user_id": user.id,
        "username": user.username,
        "active": devices.active_count(db, user.id),
        "limit": user.device_limit,
        "effective_limit": devices.limit_for(user),
        "default_limit": devices.DEFAULT_LIMIT,
        "active_window_s": devices.ACTIVE_WINDOW_S,
        "devices": [{
            "id": d.id,
            # Never the key itself: it is the value a browser presents to say
            # which device it is, and an operator has no use for it.
            "kind": d.kind,
            "platform": d.platform,
            "browser": d.browser,
            "label": devices.describe(d),
            "in_use": devices.is_active(d),
            "first_seen_at": d.first_seen_at.isoformat() + "Z" if d.first_seen_at else None,
            "last_seen_at": d.last_seen_at.isoformat() + "Z" if d.last_seen_at else None,
        } for d in rows],
    }


@app.post("/api/admin/users/{uid}/device-limit")
def admin_set_device_limit(uid: int, body: dict, admin: User = Depends(require_admin),
                           db: Session = Depends(get_db)):
    """Cap how many devices an account may be in use on at once.

    `limit: null` hands the account back to the server default rather than
    setting it to the default's current value, so changing the default later
    still moves it. `0` is unlimited, spelled the same way as the default is.
    """
    user = _admin_target(db, uid)
    raw = body.get("limit", None)
    if raw is None:
        user.device_limit = None
    else:
        # Strictly a whole number. int() would take 1.5 and quietly store 1 —
        # an operator who typed a wrong thing would be told it worked and get
        # a different limit than the one they set.
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise HTTPException(400, "The limit must be a whole number, or null to "
                                     "follow the server default")
        limit = raw
        if limit < 0 or limit > 100:
            raise HTTPException(400, "The limit must be between 0 (unlimited) and 100")
        user.device_limit = limit
    db.commit()
    return {"ok": True, "limit": user.device_limit,
            "effective_limit": devices.limit_for(user),
            "active": devices.active_count(db, user.id)}


@app.get("/api/admin/syndication")
def admin_syndication(admin: User = Depends(require_admin),
                      db: Session = Depends(get_db)):
    """The newsroom families, so a learned grouping can be argued with.

    Corroboration counts newsrooms rather than mastheads, which means something
    decided this masthead does not vote separately. That decision has to be
    visible: it is inferred from published copy, and an inference nobody can
    inspect is indistinguishable from a bug.
    """
    fams = syndication.families(db)
    return {"families": fams,
            "groups": len(fams),
            "sources": sum(len(f["members"]) for f in fams),
            "min_shared_headlines": syndication.MIN_SHARED,
            "window_hours": syndication.WINDOW_HOURS,
            "last_run": ingest.status.get("syndication")}


@app.get("/api/admin/backup/accounts")
def admin_backup_accounts(admin: User = Depends(require_admin),
                          db: Session = Depends(get_db)):
    """Download the part of the database the feeds cannot hand back.

    Automatic volume snapshots are off because copying six gigabytes of news
    every night took the site down (DEPLOY.md). The news survives that decision
    by being re-fetchable; accounts, feeds, alerts, pantheons, watched places
    and hand-added sources do not, and they are a few megabytes. This is them.

    Served through the app, signed in, rather than by any route that writes to
    a log: the file holds password hashes, email addresses and alert webhook
    URLs, and this repository — including its Actions logs — is public.
    """
    doc = accounts_backup.build(db)
    body = accounts_backup.to_json(doc)
    logging.getLogger("admin").info(
        "accounts backup downloaded by %s: %d bytes, %s",
        admin.username, len(body), doc["counts"])
    return Response(
        content=body, media_type="application/json",
        headers={
            "Content-Disposition":
                f'attachment; filename="{accounts_backup.filename(doc)}"',
            # Never a shared cache's business, and not this browser's either.
            "Cache-Control": "no-store",
            "X-Backup-Accounts": str(doc["counts"]["users"]),
            "Access-Control-Expose-Headers":
                "Content-Disposition, X-Backup-Accounts"})


@app.post("/api/admin/users/{uid}/devices/release")
def admin_release_devices(uid: int, admin: User = Depends(require_admin),
                          db: Session = Depends(get_db)):
    """Clear an account's devices and end its sessions.

    The operator-side equivalent of the emailed link, for the case where
    somebody cannot receive the mail — and the only way to free a slot held by
    a device that no longer exists.
    """
    user = _admin_target(db, uid)
    released = devices.release_all(db, user)
    return {"ok": True, "released": released}


@app.post("/api/admin/users/{uid}/disable")
def admin_set_disabled(uid: int, body: dict, admin: User = Depends(require_admin),
                       db: Session = Depends(get_db)):
    """Suspend (disabled=true) or reinstate an account. Suspended users can't
    sign in and lose admin access; existing sessions stop at the next login."""
    u = _admin_target(db, uid)
    disabled = bool(body.get("disabled", True))
    if disabled:
        if u.id == admin.id:
            raise HTTPException(400, "You can't suspend your own account")
        if _is_configured_admin(u):
            raise HTTPException(400, "This operator is set in NEWS_ADMIN_USERS and can't be suspended")
    u.disabled = disabled
    db.commit()
    return _user_admin_json(db, u)


@app.post("/api/admin/users/{uid}/verify")
def admin_verify_user(uid: int, admin: User = Depends(require_admin),
                      db: Session = Depends(get_db)):
    """Force-verify an email (useful when SMTP is unconfigured or bouncing)."""
    u = _admin_target(db, uid)
    u.email_verified = True
    db.commit()
    return _user_admin_json(db, u)


@app.post("/api/admin/users/{uid}/admin")
def admin_set_admin(uid: int, body: dict, admin: User = Depends(require_admin),
                    db: Session = Depends(get_db)):
    """Grant or revoke operator access via the persisted is_admin flag."""
    u = _admin_target(db, uid)
    make = bool(body.get("is_admin", True))
    if not make:
        if _is_configured_admin(u):
            raise HTTPException(400, "Designated in NEWS_ADMIN_USERS — remove them there instead")
        if _is_admin(u) and _admin_count(db) <= 1:
            raise HTTPException(400, "Can't remove the last operator")
    u.is_admin = make
    db.commit()
    return _user_admin_json(db, u)


@app.post("/api/admin/users/{uid}/reset-password")
def admin_reset_password(uid: int, body: dict, admin: User = Depends(require_admin),
                         db: Session = Depends(get_db)):
    """Set a new password for an account — the way to recover a locked-out user
    when email delivery isn't configured."""
    u = _admin_target(db, uid)
    pw = body.get("password") or ""
    # An operator picking a temporary password is exactly when "Welcome123"
    # gets typed, so the same rules apply here as anywhere else.
    _acceptable_password(pw, username=u.username, email=u.email)
    u.password_hash = auth.hash_password(pw)
    u.email_verified = True
    # An operator resetting somebody's password is often responding to a
    # compromise, so it has to end that account's existing sessions too —
    # otherwise the intruder keeps the one they already have.
    u.token_version += 1
    db.commit()
    return {"ok": True, "sessions_ended": True}


@app.delete("/api/admin/users/{uid}")
def admin_delete_user(uid: int, admin: User = Depends(require_admin),
                      db: Session = Depends(get_db)):
    """Permanently delete an account and all of its content."""
    u = _admin_target(db, uid)
    if u.id == admin.id:
        raise HTTPException(400, "You can't delete your own account from here")
    if _is_configured_admin(u):
        raise HTTPException(400, "This operator is set in NEWS_ADMIN_USERS and can't be deleted")
    if _is_admin(u) and _admin_count(db) <= 1:
        raise HTTPException(400, "Can't delete the last operator")
    _delete_user(db, u)
    db.commit()
    return {"ok": True}


# Unknown /api/* paths get a clear JSON 404 (registered after all real API
# routes). Without this they fall through to the static mount, which answers
# POSTs with a baffling 405 "Method Not Allowed" — typically seen when the
# client calls an endpoint newer than the running server.
@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def api_fallback(path: str):
    raise HTTPException(
        404, f"Unknown API endpoint: /api/{path}. If this endpoint should exist, "
             "the server may be running an older version — git pull and restart.")


@app.get("/reset/{token:path}")
@app.get("/verify/{token:path}")
@app.get("/devices/{token:path}")
def action_link_page(token: str):
    """Serve the app for emailed action links.

    The token is read from the path by the client; the server does nothing with
    it here. These have to be real routes because the static mount would 404 on
    a path that is not a file on disk.
    """
    return FileResponse(FRONTEND_DIR / "index.html",
                        headers={"Cache-Control": "no-cache, must-revalidate"})


class RevalidatingStatic(StaticFiles):
    """Serve the frontend with must-revalidate caching.

    Browsers cache app.js/styles.css aggressively by default, so after a deploy
    a returning user can keep running the *previous* build — bugs appear fixed
    in the repo but not in the browser, and the only workaround is telling
    people to hard-refresh. "no-cache" still allows caching but forces an ETag
    revalidation on every load, so updates land immediately and unchanged files
    cost only a 304.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app.mount("/", RevalidatingStatic(directory=str(FRONTEND_DIR), html=True), name="frontend")
