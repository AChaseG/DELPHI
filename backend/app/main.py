"""D.E.L.P.H.I. — Digital Exploration and Layout for Publicly Harvested Intelligence."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.parse

import httpx
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete as sa_delete, func, or_, select
from sqlalchemy.orm import Session

import re as _username_re

from . import auth, ingest, langdetect, mailer, ratelimit, repair, translate
from .boolean_query import normalize_quotes, validate_query
from .catalog import seed_city_sources, seed_demo_articles, seed_sources
from .changelog import CHANGELOG, fingerprints, unseen_entries, updates_since
from .clustering import assign_events, rebuild_events
from .database import Base, engine, get_db
from .events import broadcaster
from .geo import load_gazetteer
from .matching import query_articles
from .models import (Alert, AlertEvent, Article, Event, Feed, Pantheon,
                     PantheonInvite, PantheonMember, Source, Translation,
                     User, ViewedEvent, utcnow)
from .schemas import AlertIn, FeedIn, QueryValidateIn, SocialTrackerIn, SourceIn, TopicTrackerIn
from .scoring import STANDARD_CATEGORIES, classify_categories

logging.basicConfig(level=logging.INFO)

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
DISABLE_INGEST = os.environ.get("NEWS_DISABLE_INGEST", "") == "1"


def _ensure_schema():
    """Additive migrations for databases created by earlier versions."""
    wanted = {
        "articles": {"event_id": "INTEGER", "content": "TEXT DEFAULT ''"},
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
                    "paywall": "BOOLEAN DEFAULT 0"},
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    _ensure_schema()
    db = next(get_db())
    try:
        seed_sources(db)
        if os.environ.get("NEWS_SEED_CITIES", "1") != "0":
            added = seed_city_sources(db)
            if added:
                logging.getLogger("catalog").info("seeded %d city local-news sources", added)
    finally:
        db.close()
    task = None
    if not DISABLE_INGEST:
        task = asyncio.create_task(ingest.ingest_loop())
    yield
    if task:
        task.cancel()


app = FastAPI(title="D.E.L.P.H.I.", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@app.middleware("http")
async def require_account(request: Request, call_next):
    """An account is required to use the system: every API route except
    /api/auth/* demands a valid session token. Static assets stay public so
    the sign-in page itself can load. The SSE stream may pass the token as a
    ?token= query parameter (EventSource cannot set headers)."""
    path = request.url.path
    if path.startswith("/api") and not path.startswith("/api/auth/"):
        token = ""
        authz = request.headers.get("authorization", "")
        if authz.startswith("Bearer "):
            token = authz[7:].strip()
        elif "token" in request.query_params:
            token = request.query_params["token"]
        if not token or auth.parse_token(token) is None:
            return JSONResponse({"detail": "Authentication required — sign in"}, status_code=401)
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


def _drop_stale(articles: list[Article], criteria: dict, stale_hours: float) -> list[Article]:
    """When the feed opted into hide_stale, drop articles whose event has had
    no update within the user's staleness threshold. Display-only: clustering
    still attaches new reports to hidden events, which then resurface with
    their full timeline (guaranteed while the threshold stays within the 72h
    clustering window)."""
    if not (criteria or {}).get("hide_stale") or not stale_hours:
        return articles
    from datetime import timedelta
    cutoff = utcnow() - timedelta(hours=stale_hours)
    return [a for a in articles
            if a.event_id is None or (a.event and a.event.updated_at >= cutoff)]


def _viewed_events(db: Session, user_id: str, articles: list[Article]) -> set[int]:
    """Event ids among these articles that the user has opened in Event Focus."""
    event_ids = {a.event_id for a in articles if a.event_id is not None}
    if not user_id or not event_ids:
        return set()
    return set(db.scalars(select(ViewedEvent.event_id).where(
        ViewedEvent.user_id == user_id, ViewedEvent.event_id.in_(event_ids))))


def _article_json(a: Article, tr: dict | None = None,
                  viewed: set[int] | None = None) -> dict:
    """Serialize an article; `tr` is a {article_id: {title, summary}} map of
    translations into the requester's language."""
    t = (tr or {}).get(a.id)
    return {
        "id": a.id,
        "event_id": a.event_id,
        "viewed": a.event_id in (viewed or set()),
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
        "countries": [
            {"iso2": c["iso2"], "name": c["name"]}
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
        raise HTTPException(403, "This account is suspended")
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
    """Absolute link to the app carrying an action token."""
    return f"{_action_base_url(request)}/?{param}={urllib.parse.quote(token, safe='')}"


@app.post("/api/auth/register", status_code=201)
def register(body: dict, request: Request, db: Session = Depends(get_db)):
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
    db.add(user)
    db.commit()
    if mailer.enabled():
        link = _action_link(request, "verify",
                            auth.make_scoped_token("verify", user.id, 48 * 3600))
        mailer.send_verification(email, username, link)
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
    if mailer.enabled() and not user.email_verified:
        raise HTTPException(403, "unverified: check your inbox for the verification link")
    # Keep the DB flag in step with a NEWS_ADMIN_USERS designation.
    if not user.is_admin and _is_configured_admin(user):
        user.is_admin = True
        db.commit()
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
def resend_verification(body: dict, request: Request, db: Session = Depends(get_db)):
    """Always answers 200 — no account enumeration."""
    ratelimit.check("resend", request)
    ident = (body.get("username") or "").strip().lower()
    user = db.scalar(select(User).where(or_(User.username == ident, User.email == ident)))
    if user and not user.email_verified and mailer.enabled():
        link = _action_link(request, "verify",
                            auth.make_scoped_token("verify", user.id, 48 * 3600))
        mailer.send_verification(user.email, user.username, link)
    return {"ok": True}


@app.post("/api/auth/forgot")
def forgot_password(body: dict, request: Request, db: Session = Depends(get_db)):
    """Always answers 200 — no account enumeration."""
    ratelimit.check("forgot", request)
    email = (body.get("email") or "").strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user and mailer.enabled():
        link = _action_link(request, "reset",
                            auth.make_scoped_token("reset", user.id, 3600))
        mailer.send_password_reset(user.email, user.username, link)
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
        raise HTTPException(401, "Not signed in")
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
        raise HTTPException(422, "settings must be an object")
    raw = json.dumps(settings)
    if len(raw) > 4096:
        raise HTTPException(422, "Settings payload too large")
    user.settings = raw
    db.commit()
    return {"ok": True}


# ---------- sources ----------

@app.get("/api/sources")
def list_sources(db: Session = Depends(get_db)):
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
    stale: float = Query(default=0),
    user_id: str = Depends(user_id_header),
    db: Session = Depends(get_db),
):
    """Ad-hoc search with a criteria object (used for feed preview and search bar)."""
    criteria = body.get("criteria", body)
    articles = query_articles(db, criteria, sort=sort, limit=limit)
    articles = _drop_stale(articles, criteria, stale)
    tr = await translate.translate_articles(db, articles, lang)
    viewed = _viewed_events(db, user_id, articles)
    return [_article_json(a, tr, viewed) for a in articles]


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


@app.get("/api/feeds/{feed_id}/articles")
async def feed_articles(feed_id: int, limit: int = Query(default=40, le=200),
                        lang: str = Query(default=""),
                        stale: float = Query(default=0),
                        user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    feed = _shared_item_for_read(db, Feed, feed_id, user_id)
    articles = query_articles(db, feed.criteria, sort=feed.sort, limit=limit)
    articles = _drop_stale(articles, feed.criteria, stale)
    tr = await translate.translate_articles(db, articles, lang)
    viewed = _viewed_events(db, user_id, articles)
    return [_article_json(a, tr, viewed) for a in articles]


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

    out = []
    for key in order:
        members = groups[key]
        event = members[0].event if members[0].event_id else None
        out.append({
            "event_id": members[0].event_id,
            "viewed": members[0].event_id in viewed,
            "matched_count": len(members),
            "total_count": event.article_count if event else len(members),
            "source_count": len({a.source_id for a in members}),
            "importance": event.importance if event else max(a.importance for a in members),
            "first_seen": event.first_seen.isoformat() + "Z" if event else None,
            "articles": [_article_json(a, tr) for a in members[:6]],
        })
    return out


@app.get("/api/feeds/{feed_id}/events")
async def feed_events(feed_id: int, limit: int = Query(default=30, le=100),
                      lang: str = Query(default=""),
                      stale: float = Query(default=0),
                      user_id: str = Depends(user_id_header), db: Session = Depends(get_db)):
    feed = _shared_item_for_read(db, Feed, feed_id, user_id)
    articles = query_articles(db, feed.criteria, sort=feed.sort, limit=200)
    articles = _drop_stale(articles, feed.criteria, stale)
    return await _grouped_response(db, articles, lang, limit, user_id)


@app.post("/api/articles/search-grouped")
async def search_grouped(
    body: dict,
    sort: str = Query(default="newest"),
    limit: int = Query(default=30, le=100),
    lang: str = Query(default=""),
    stale: float = Query(default=0),
    user_id: str = Depends(user_id_header),
    db: Session = Depends(get_db),
):
    """Ad-hoc criteria search clustered into events (used by Home columns)."""
    criteria = body.get("criteria", body)
    articles = query_articles(db, criteria, sort=sort, limit=200)
    articles = _drop_stale(articles, criteria, stale)
    return await _grouped_response(db, articles, lang, limit, user_id)


@app.post("/api/events/{event_id}/viewed")
def mark_event_viewed(event_id: int, user_id: str = Depends(user_id_header),
                      db: Session = Depends(get_db)):
    if not db.get(Event, event_id):
        raise HTTPException(404, "Event not found")
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
        raise HTTPException(422, "You are already here")
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
        raise HTTPException(409, "Already a member")
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


@app.get("/api/events/{event_id}")
async def event_detail(event_id: int, lang: str = Query(default=""),
                       db: Session = Depends(get_db)):
    """One event in full: article timeline (newest first), the outlets
    covering it, and related events (headline similarity, with a shared-
    country fallback)."""
    from datetime import timedelta

    from .scoring import tokens_similarity

    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    articles = db.scalars(
        select(Article).where(Article.event_id == event_id)
        .order_by(Article.published_at.desc()).limit(100)
    ).all()
    tr = await translate.translate_articles(db, articles, lang)

    sources = []
    seen_sources = set()
    for a in articles:
        if a.source and a.source.id not in seen_sources:
            seen_sources.add(a.source.id)
            sources.append({"id": a.source.id, "name": a.source.name,
                            "platform": a.source.platform or "news",
                            "country": a.source.country})

    def _event_brief(e: Event, why: str) -> dict:
        return {"id": e.id, "title": e.title, "importance": e.importance,
                "article_count": e.article_count,
                "updated_at": e.updated_at.isoformat() + "Z", "why": why}

    recent = db.scalars(select(Event).where(
        Event.updated_at >= utcnow() - timedelta(days=14), Event.id != event_id
    ).order_by(Event.updated_at.desc()).limit(500)).all()
    scored = sorted(
        ((tokens_similarity(event.cluster_tokens, e.cluster_tokens), e) for e in recent),
        key=lambda pair: -pair[0],
    )
    related = [_event_brief(e, "similar story") for sim, e in scored[:5] if sim >= 0.2]
    if len(related) < 5 and (event.countries or []):
        have = {r["id"] for r in related}
        same_place = [e for _, e in scored
                      if e.id not in have and set(e.countries or []) & set(event.countries)]
        same_place.sort(key=lambda e: -e.importance)
        related += [_event_brief(e, "same region") for e in same_place[:5 - len(related)]]

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
        "sources": sources,
        "related": related,
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
        raise HTTPException(403, "Not a member of this Pantheon")
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


def _delete_user(db: Session, user: User) -> None:
    """Remove an account and everything referencing it: personal feeds/alerts
    (alert events cascade through the ORM), viewed-event history, Pantheon
    memberships and invites, and any Pantheon they own (with its shared
    feeds/alerts, members, and invites)."""
    acct = f"acct:{user.id}"
    for p in db.scalars(select(Pantheon).where(Pantheon.owner_id == user.id)).all():
        db.execute(sa_delete(Feed).where(Feed.pantheon_id == p.id))
        for a in db.scalars(select(Alert).where(Alert.pantheon_id == p.id)).all():
            db.delete(a)  # per-row so AlertEvent children cascade
        db.execute(sa_delete(PantheonMember).where(PantheonMember.pantheon_id == p.id))
        db.execute(sa_delete(PantheonInvite).where(PantheonInvite.pantheon_id == p.id))
        db.delete(p)
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


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
