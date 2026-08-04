"""The curated Home board, matched ahead of time rather than on arrival.

Home's columns are the same for everybody: fixed criteria over the shared
corpus, with nothing in them that depends on who is asking. They were still
matched from scratch on every request, so the first person to open Delphi after
a quiet spell paid for the whole board — and on a 250,000-article database that
is most of a second of pure scanning (the "Breaking now" column alone measured
569ms, against 2ms to serialize what it found).

So the poller runs those queries itself, each time it brings in news, and keeps
the article ids it matched. A request for one of these columns then loads rows
by primary key instead of scanning.

Only the *matching* is shared. Translation, which events the reader has already
opened, and staleness are still applied per request against live rows, so no
part of one account's view can reach another's.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session, defer, joinedload

from .matching import query_articles
from .models import Article

log = logging.getLogger("home")

# How many articles each kind of column asks for. A grouped column clusters a
# wide draw down to a handful of events, so it matches far more than it shows;
# both numbers are the client's, and both have to be here for a request to be
# recognized as one of these columns.
PLAIN_LIMIT = 40
GROUPED_REQUEST_LIMIT = 30    # events the client asks for
GROUPED_QUERY_LIMIT = 200     # articles the server clusters to produce them

# Mirrors DELPHI_FEEDS in frontend/js/app.js — same criteria, same sort, same
# grouping. A column whose definition drifts from the client's simply stops
# being recognized and is matched live, so drift costs speed, never correctness.
# tests/test_home_cache.py compares the two lists so it cannot drift unnoticed.
HOME_COLUMNS: list[dict] = [
    {"id": "top", "criteria": {"min_importance": 55},
     "sort": "importance", "grouped": True},
    {"id": "breaking", "criteria": {"hours": 6, "min_importance": 40},
     "sort": "importance", "grouped": False},
    {"id": "conflict", "criteria": {"categories": ["conflict", "disaster"]},
     "sort": "newest", "grouped": True},
    {"id": "politics", "criteria": {"categories": ["politics"]},
     "sort": "newest", "grouped": False},
    {"id": "business", "criteria": {"categories": ["business", "economy"]},
     "sort": "newest", "grouped": False},
    {"id": "scitech", "criteria": {"categories": ["science", "technology"]},
     "sort": "newest", "grouped": False},
    {"id": "social", "criteria": {"platforms": ["reddit", "mastodon", "bluesky", "youtube"]},
     "sort": "newest", "grouped": False},
]

# How old a warmed list may be before a request matches it live instead. The
# poller refreshes every cycle that ingests anything — about every 15 seconds —
# so this is a safety valve for a stalled or disabled poller, not the normal
# path. It also bounds how far a time-relative column ("published in the last
# six hours") can drift from what asking outright would return.
MAX_AGE_S = 120.0

# home id -> (article ids in match order, monotonic time the list was built)
_warm: dict[str, tuple[list[int], float]] = {}


def _normalize(criteria: dict) -> tuple:
    """Order-independent form of a criteria object.

    The client builds these by hand, so a list may arrive in any order and an
    empty value may or may not be present at all; neither should stop a column
    from being recognized."""
    out = []
    for field, value in sorted((criteria or {}).items()):
        if value in (None, "", [], {}, False):
            continue
        out.append((field, tuple(sorted(value)) if isinstance(value, list) else value))
    return tuple(out)


def _key(criteria: dict, sort: str, limit: int, grouped: bool) -> str:
    return repr((_normalize(criteria), sort, limit, grouped))


def _request_limit(col: dict) -> int:
    return GROUPED_REQUEST_LIMIT if col["grouped"] else PLAIN_LIMIT


def _query_limit(col: dict) -> int:
    return GROUPED_QUERY_LIMIT if col["grouped"] else PLAIN_LIMIT


_BY_KEY = {
    _key(c["criteria"], c["sort"], _request_limit(c), c["grouped"]): c["id"]
    for c in HOME_COLUMNS
}


def refresh(db: Session) -> int:
    """Re-match every Home column. Returns how many were stored.

    Built into a fresh dict and swapped in one assignment, so a request reading
    the cache mid-refresh sees either the whole previous generation or the whole
    new one, never a half-written mixture."""
    global _warm
    built: dict[str, tuple[list[int], float]] = {}
    started = time.perf_counter()
    for col in HOME_COLUMNS:
        try:
            articles = query_articles(db, col["criteria"], sort=col["sort"],
                                      limit=_query_limit(col))
        except Exception:
            log.exception("could not warm Home column %s", col["id"])
            continue    # a column that fails is matched live, like any other
        built[col["id"]] = ([a.id for a in articles], time.monotonic())
    _warm = built
    log.debug("warmed %d Home columns in %.0fms", len(built),
              (time.perf_counter() - started) * 1000)
    return len(built)


def warm_article_ids() -> list[int]:
    """Every article id currently sitting in a warmed column, de-duplicated.

    These are exactly the stories the next reader to open Home will be shown,
    which makes them the ones worth translating before anyone asks rather than
    while they wait.
    """
    seen: dict[int, None] = {}
    for ids, _ in _warm.values():
        for article_id in ids:
            seen[article_id] = None
    return list(seen)


def clear() -> None:
    """Drop everything warmed — for tests, and after a bulk deletion."""
    global _warm
    _warm = {}


def take(db: Session, criteria: dict, sort: str, limit: int,
         grouped: bool) -> list[Article] | None:
    """Rows for this column if it is one of Home's and warm, else None.

    Loading by primary key is what makes this worth doing: no scan, no sort and
    no criteria pass — SQLite fetches the rows it was told to and nothing more."""
    hid = _BY_KEY.get(_key(criteria, sort, limit, grouped))
    if hid is None:
        return None
    entry = _warm.get(hid)
    if entry is None:
        return None
    ids, built_at = entry
    if time.monotonic() - built_at > MAX_AGE_S:
        return None
    if not ids:
        return []
    stmt = (select(Article).options(joinedload(Article.source), defer(Article.content))
            .where(Article.id.in_(ids)))
    by_id = {a.id: a for a in db.scalars(stmt)}
    # Ordered as the match ordered them, and quietly dropping anything pruned or
    # deleted since the list was built.
    return [by_id[i] for i in ids if i in by_id]


def status() -> dict:
    """What the operator console reports about the warm board."""
    now = time.monotonic()
    return {
        "columns": len(_warm),
        "oldest_age_s": round(max((now - at for _, at in _warm.values()), default=0), 1),
        "articles": sum(len(ids) for ids, _ in _warm.values()),
    }
