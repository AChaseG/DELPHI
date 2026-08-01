"""D.E.L.P.H.I. — Digital Exploration and Layout for Publicly Harvested Intelligence."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.parse
import uuid

import httpx
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

import re as _username_re

from . import (auth, export, geocode, home, ingest, langdetect, mailer, ratelimit,
               repair, translate)
from .boolean_query import normalize_quotes, validate_query
from .catalog import seed_city_sources, seed_sources
from .changelog import CHANGELOG, fingerprints, unseen_entries, updates_since
from .clustering import assign_events, rebuild_events
from .database import Base, SessionLocal, engine, get_db
from .events import broadcaster
from .geo import load_gazetteer, search_places
from .matching import query_articles
from .models import (Alert, AlertEvent, Article, DiscoveredDomain, Event,
                     FavoriteLocation, Feed,
                     Pantheon, PantheonInvite, PantheonMember, Source,
                     Translation, User, ViewedArticle, ViewedEvent, utcnow)
from .schemas import AlertIn, FeedIn, QueryValidateIn, SocialTrackerIn, SourceIn, TopicTrackerIn
from .scoring import STANDARD_CATEGORIES, classify_categories

logging.basicConfig(level=logging.INFO)

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
                    "last_modified": "VARCHAR(80) DEFAULT ''"},
        "users": {"email": "VARCHAR(200) DEFAULT ''",
                  "email_verified": "BOOLEAN DEFAULT 0",
                  "last_seen_at": "DATETIME",
                  "changelog_seen": "TEXT",
                  "settings": "TEXT",
                  "is_admin": "BOOLEAN DEFAULT 0",
                  "disabled": "BOOLEAN DEFAULT 0"},
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
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="D.E.L.P.H.I.", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
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

    The reference is deliberately not an id anyone can look up — it identifies
    one moment in the log, nothing else — and the exception's own text never
    reaches the browser, because tracebacks name file paths and query shapes.
    """
    reference = uuid.uuid4().hex[:6]
    _fail_log.exception(
        "[%s] unhandled %s on %s %s%s (account %s)",
        reference, type(exc).__name__, request.method, request.url.path,
        f"?{request.url.query}" if request.url.query else "",
        getattr(request.state, "user_id", None) or "anonymous")
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
    the sign-in page itself can load. The SSE stream may pass the token as a
    ?token= query parameter (EventSource cannot set headers).

    The token's signature is not sufficient on its own: tokens live for 30 days
    and carry no revocation, so the account behind one is re-checked here on
    every request. Without that, an operator suspending or deleting an account
    would only stop future sign-ins while the holder's current token kept
    working for weeks. The check is a primary-key lookup, and it sits in the
    middleware so it covers routes that take no user_id dependency too.
    """
    path = request.url.path
    if path.startswith("/api") and not path.startswith("/api/auth/"):
        token = ""
        authz = request.headers.get("authorization", "")
        if authz.startswith("Bearer "):
            token = authz[7:].strip()
        elif "token" in request.query_params:
            token = request.query_params["token"]
        uid = auth.parse_token(token) if token else None
        if uid is None:
            return JSONResponse({"detail": "Authentication required — sign in"}, status_code=401)
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
            # Named in the failure log, so a crash can be traced to a person
            # who reported it rather than to an anonymous request.
            request.state.user_id = f"acct:{uid}"
    return await call_next(request)


def user_id_header(authorization: str = Header(default=""),
                   token: str = Query(default="")) -> str:
    """Resolve the caller's account key ("acct:<id>") from the Bearer token
    (or ?token= for SSE). The middleware guarantees one is present on
    protected routes; this is defense in depth."""
    raw = authorization[7:].strip() if authorization.startswith("Bearer ") else token
    uid = auth.parse_token(raw) if raw else None
    if uid is None:
        raise HTTPException(401, "Authentication required — sign in")
    return f"acct:{uid}"


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

@app.get("/api/meta")
def meta(user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    gaz = load_gazetteer()
    me_user = db.get(User, _acct_id(user_id))
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
        select(func.count(Source.id)).where(Source.enabled.is_(True),
                                            Source.last_status.startswith("ok"))
    ) or 0
    sources_total = db.scalar(select(func.count(Source.id)).where(Source.enabled.is_(True))) or 0
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
        "stats": {
            "total_articles": total_articles,
            "articles_24h": articles_24h,
            "countries_24h": countries_24h,
            "sources_ok": sources_ok,
            "sources_total": sources_total,
        },
        "ingest": ingest.status,
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
    if len(password) < 8:
        raise HTTPException(422, "Password must be at least 8 characters")
    if db.scalar(select(User).where(User.username == username)):
        raise HTTPException(409, "That username is taken")
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "An account with that email already exists")
    user = User(username=username, email=email, password_hash=auth.hash_password(password),
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
    return {"token": auth.make_token(user.id), "username": username,
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
    return {"token": auth.make_token(user.id), "username": user.username,
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


@app.post("/api/auth/reset")
def reset_password(body: dict, request: Request, db: Session = Depends(get_db)):
    ratelimit.check("reset", request)
    uid = auth.parse_scoped_token("reset", body.get("token") or "")
    user = db.get(User, uid) if uid else None
    if not user:
        raise HTTPException(400, "This reset link is invalid or has expired")
    password = body.get("password") or ""
    if len(password) < 8:
        raise HTTPException(422, "Password must be at least 8 characters")
    user.password_hash = auth.hash_password(password)
    user.email_verified = True  # proving inbox access verifies the email too
    db.commit()
    return {"ok": True, "username": user.username}


@app.get("/api/auth/me")
def me(authorization: str = Header(default=""), db: Session = Depends(get_db)):
    raw = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    uid = auth.parse_token(raw) if raw else None
    if uid is None:
        raise HTTPException(401, "Authentication required — sign in")
    user = db.get(User, uid)
    if not user:
        raise HTTPException(401, "Account no longer exists")
    return {"user_key": f"acct:{uid}", "username": user.username, "email": user.email,
            "is_admin": _is_admin(user)}


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

@app.get("/api/sources")
def list_sources(slim: bool = Query(default=False), db: Session = Depends(get_db)):
    """Every source, or — with slim=1 — just enough to name one.

    The full catalog runs past a thousand outlets and half a megabyte, and the
    dashboard needs it only when the Sources panel or the wizard's picker is
    open. At startup all it has to do is put a name against the outlets a feed
    is restricted to, which is two fields."""
    if slim:
        rows = db.execute(select(Source.id, Source.name).order_by(Source.name)).all()
        return [{"id": i, "name": n} for i, n in rows]
    sources = db.scalars(select(Source).order_by(Source.name)).all()
    return [{
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
    } for s in sources]


@app.post("/api/sources", status_code=201)
def create_source(body: SourceIn, db: Session = Depends(get_db)):
    if db.scalar(select(Source).where(Source.rss_url == body.rss_url)):
        raise HTTPException(409, "A source with this RSS URL already exists")
    source = Source(**body.model_dump(), added_by="user")
    db.add(source)
    db.commit()
    return {"id": source.id}


@app.post("/api/sources/seed-cities")
def seed_cities_endpoint(db: Session = Depends(get_db)):
    """Add local-news sources for every world city in the catalog (idempotent).
    Lets an already-running instance pick up the city catalog without a restart."""
    from . import cities
    added = seed_city_sources(db)
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
                "categories", "tier", "platform", "homepage", "region", "paywall"):
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


@app.post("/api/sources/{source_id}/repair")
async def repair_source(source_id: int, db: Session = Depends(get_db)):
    """Manual self-repair (the 🔧 button): re-check the current URL first —
    a source that merely had a bad day is marked healthy without changes —
    then hunt for a working replacement feed and ingest it immediately."""
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    old_url = source.rss_url
    async with httpx.AsyncClient(timeout=repair.REPAIR_TIMEOUT, follow_redirects=True) as client:
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
    return {"repaired": True, "changed": source.rss_url != old_url,
            "rss_url": source.rss_url, "repaired_from": source.repaired_from,
            "status": source.last_status, "new_articles": len(new)}


# ---------- articles / search ----------

@app.post("/api/articles/search")
async def search_articles(
    body: dict,
    sort: str = Query(default="newest"),
    limit: int = Query(default=50, le=200),
    lang: str = Query(default=""),
    user_id: str = Depends(user_id_header),
    db: Session = Depends(get_db),
):
    """Ad-hoc search with a criteria object (used for feed preview and search bar)."""
    criteria = body.get("criteria", body)
    # One of Home's columns is the same query for every reader, so the poller
    # has already run it; everything below this line is still per-request.
    articles = home.take(db, criteria, sort, limit, grouped=False)
    if articles is None:
        articles = query_articles(db, criteria, sort=sort, limit=limit)
    tr = await translate.translate_articles(db, articles, lang)
    viewed = _viewed_events(db, user_id, articles)
    read_alone = _viewed_articles(db, user_id, articles)
    events = _events_for(db, articles) if (criteria or {}).get("hide_stale") else None
    return [_article_json(a, tr, viewed, events, read_alone) for a in articles]


@app.post("/api/query/validate")
def query_validate(body: QueryValidateIn):
    err = validate_query(body.query) if body.query.strip() else None
    return {"valid": err is None, "error": err}


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
async def export_feed(feed_id: int, fmt: str = Query(default="csv", alias="format"),
                      limit: int = Query(default=500, le=EXPORT_MAX),
                      lang: str = Query(default=""),
                      user_id: str = Depends(user_id_header),
                      db: Session = Depends(get_db)):
    """One saved feed's current matches, as a file."""
    feed = _shared_item_for_read(db, Feed, feed_id, user_id)
    articles = query_articles(db, feed.criteria, sort=feed.sort, limit=limit)
    tr = await translate.translate_articles(db, articles, lang)
    return _export_response(fmt, feed.name, _export_rows(articles, tr))


@app.post("/api/articles/export")
async def export_search(body: dict, fmt: str = Query(default="csv", alias="format"),
                        sort: str = Query(default="newest"),
                        limit: int = Query(default=500, le=EXPORT_MAX),
                        lang: str = Query(default=""),
                        user_id: str = Depends(user_id_header),
                        db: Session = Depends(get_db)):
    """The same, for a column that isn't a saved feed — Home's built-in columns
    and the results of a search from the rail."""
    criteria = body.get("criteria", body)
    name = (body.get("name") or "Delphi export").strip()[:120]
    articles = query_articles(db, criteria, sort=sort, limit=limit)
    tr = await translate.translate_articles(db, articles, lang)
    return _export_response(fmt, name, _export_rows(articles, tr))


@app.get("/api/feeds/{feed_id}/articles")
async def feed_articles(feed_id: int, limit: int = Query(default=40, le=200),
                        lang: str = Query(default=""),
                                            user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    feed = _shared_item_for_read(db, Feed, feed_id, user_id)
    articles = query_articles(db, feed.criteria, sort=feed.sort, limit=limit)
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
    articles = query_articles(db, feed.criteria, sort=feed.sort, limit=200)
    return await _grouped_response(db, articles, lang, limit, user_id)


@app.post("/api/articles/search-grouped")
async def search_grouped(
    body: dict,
    sort: str = Query(default="newest"),
    limit: int = Query(default=30, le=100),
    lang: str = Query(default=""),
    user_id: str = Depends(user_id_header),
    db: Session = Depends(get_db),
):
    """Ad-hoc criteria search clustered into events (used by Home columns)."""
    criteria = body.get("criteria", body)
    articles = home.take(db, criteria, sort, limit, grouped=True)
    if articles is None:
        articles = query_articles(db, criteria, sort=sort, limit=home.GROUPED_QUERY_LIMIT)
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
    return url


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
async def ingest_run():
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
    return {
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
            "geocoder": {"provider": geocode.PROVIDER if geocode.enabled() else "off",
                         **geocode.status},
            # Mail is sent in the background so a reader can't time the reply
            # to learn whether an address exists; the price is that a broken
            # relay is invisible to everyone. This is where it becomes visible.
            "mail": {"configured": mailer.enabled(),
                     "host": f"{mailer.HOST}:{mailer.PORT}" if mailer.enabled() else "",
                     **mailer.status},
            **_reach_status(db)}


@app.post("/api/maintenance/fetch-content")
async def fetch_content_backfill(body: dict | None = None, db: Session = Depends(get_db)):
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


@app.post("/api/maintenance/detect-languages")
def detect_languages(db: Session = Depends(get_db)):
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
def events_rebuild(db: Session = Depends(get_db)):
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
        "lat": loc.lat, "lon": loc.lon, "radius_km": loc.radius_km,
        "color": loc.color, "feed_id": loc.feed_id,
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
    return {"type": "Circle", "center": [loc.lat, loc.lon], "radius_km": loc.radius_km}


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

    loc = FavoriteLocation(user_id=user_id, name=name, lat=lat, lon=lon,
                           radius_km=radius, color=(body.get("color") or "gold")[:16])
    db.add(loc)
    db.flush()
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
    db.delete(loc)
    db.flush()
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
            "admin_count": _admin_count(db), "me": admin.id}


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
    if len(pw) < 8:
        raise HTTPException(422, "Password must be at least 8 characters")
    u.password_hash = auth.hash_password(pw)
    u.email_verified = True
    db.commit()
    return {"ok": True}


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
