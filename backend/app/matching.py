"""Shared criteria matching for feeds, alerts, and ad-hoc searches.

Criteria shape (every key optional):
{
  "countries": ["US", "FR"],          # article country OR any tagged place country
  "scopes": ["local", "national", "international"],
  "categories": ["politics", ...],
  "languages": ["en", ...],
  "source_ids": [1, 2],
  "keywords": ["earthquake", ...],    # match ANY (word-boundary, case-insensitive)
  "exclude_keywords": ["opinion"],    # match NONE
  "query": "(a OR b) AND NOT c",      # user boolean string
  "min_importance": 60,
  "hours": 48,                        # recency window
  "geo": {...}                        # GeoJSON Polygon/MultiPolygon or Circle (see geo.py)
}
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, defer, joinedload

from sqlalchemy import text

from .boolean_query import QueryError, compile_query, covering_terms
from .geo import places_match_geo
from .models import Article, Source, utcnow

# Scripts SQLite's unicode61 tokenizer doesn't word-segment — FTS can't
# reliably match these, so queries containing them fall back to a full scan.
_FTS_UNSAFE = re.compile(r"[぀-ヿ㐀-鿿가-힣฀-๿*?]")

# Rows fetched per round trip while scanning candidates. Small enough that a
# query filling its page early reads a fraction of the scan cap, large enough
# that a selective query doesn't pay for many round trips.
_STREAM_BATCH = 256

# How far the FTS path may scan before giving up on filling a page. Reached only
# by a feed whose Python-side predicates reject nearly everything the index
# offers; see the ladder in query_articles.
_FTS_SCAN_CAP = 20000

# Scan caps tried in order. Each rung is only paid for if the one before it
# couldn't fill the page, and the ladder always ends at the caller's cap.
_SCAN_LADDER = (400, 4000)


def _kw_regex(kw: str) -> re.Pattern:
    parts = [re.escape(p) for p in kw.split()]
    return re.compile(r"\b" + r"\s+".join(parts) + r"\b", re.IGNORECASE)


class CriteriaMatcher:
    """Compile criteria once, then test many articles."""

    def __init__(self, criteria: dict):
        self.criteria = criteria or {}
        self.countries = set(self.criteria.get("countries") or [])
        self.scopes = set(self.criteria.get("scopes") or [])
        self.platforms = set(self.criteria.get("platforms") or [])
        self.categories = set(self.criteria.get("categories") or [])
        self.languages = set(self.criteria.get("languages") or [])
        self.source_ids = set(self.criteria.get("source_ids") or [])
        self.min_importance = int(self.criteria.get("min_importance") or 0)
        # Any number of areas, matched as OR. `geo` was the original single-area
        # key and is still written by older clients and stored on saved feeds,
        # so it is folded in rather than replaced.
        self.geos = [g for g in (self.criteria.get("geos") or []) if g]
        legacy = self.criteria.get("geo") or None
        if legacy:
            self.geos.append(legacy)
        self.geo = self.geos[0] if self.geos else None
        self.keywords = [k for k in (self.criteria.get("keywords") or []) if k.strip()]
        self.keyword_res = [_kw_regex(k) for k in self.keywords]
        self.exclude_res = [_kw_regex(k) for k in (self.criteria.get("exclude_keywords") or []) if k.strip()]
        # Multiple boolean strings run independently and OR together: an
        # article matches if ANY compiles+matches (other criteria still AND).
        query_strings = [q for q in (self.criteria.get("queries") or []) if q.strip()]
        legacy = (self.criteria.get("query") or "").strip()
        if legacy:
            query_strings.append(legacy)
        self.query_strings = query_strings
        self.query_preds = []
        for q in query_strings:
            try:
                self.query_preds.append(compile_query(q))
            except QueryError:
                self.query_preds.append(lambda text: False)  # invalid saved query matches nothing

        hours = self.criteria.get("hours")
        self.since: datetime | None = None
        if hours:
            self.since = utcnow() - timedelta(hours=float(hours))

        # Text (title+summary+body) is only assembled when some predicate
        # reads it, so callers can skip loading article bodies otherwise.
        self.needs_text = bool(self.keyword_res or self.exclude_res or self.query_preds)

        # Explicit date range (inclusive calendar days, UTC)
        self.date_from: datetime | None = None
        self.date_to: datetime | None = None
        try:
            if self.criteria.get("date_from"):
                self.date_from = datetime.fromisoformat(self.criteria["date_from"][:10])
            if self.criteria.get("date_to"):
                self.date_to = (datetime.fromisoformat(self.criteria["date_to"][:10])
                                + timedelta(days=1))  # end of that day
        except ValueError:
            pass  # malformed dates: ignore the bound rather than match nothing

    def matches(self, article: Article, source: Source | None = None) -> bool:
        source = source or article.source
        # Full recall: criteria see the fetched article body, not just the
        # headline and feed summary. Assembled only when a predicate needs it —
        # touching .content would otherwise force-load a deferred column.
        text = (f"{article.title}\n{article.summary}\n{article.content or ''}"
                if self.needs_text else "")

        if self.since and article.published_at and article.published_at < self.since:
            return False
        if self.date_from and article.published_at and article.published_at < self.date_from:
            return False
        if self.date_to and article.published_at and article.published_at >= self.date_to:
            return False
        if article.importance < self.min_importance:
            return False
        if self.source_ids and article.source_id not in self.source_ids:
            return False
        if self.scopes and (not source or source.scope not in self.scopes):
            return False
        if self.platforms and (not source or (source.platform or "news") not in self.platforms):
            return False
        if self.languages and article.language not in self.languages:
            return False
        if self.countries:
            place_countries = {p.get("country") for p in (article.places or [])}
            place_countries.add(article.country)
            if not (place_countries & self.countries):
                return False
        if self.categories and not (set(article.categories or []) & self.categories):
            return False
        if self.keyword_res and not any(r.search(text) for r in self.keyword_res):
            return False
        if any(r.search(text) for r in self.exclude_res):
            return False
        if self.query_preds and not any(pred(text) for pred in self.query_preds):
            return False
        if self.geos and not any(
                places_match_geo(article.places or [], article.country, g) for g in self.geos):
            return False
        return True


def _fts_terms(matcher: CriteriaMatcher) -> set[str] | None:
    """Terms every match must contain, for an FTS prefilter — or None to fall
    back to a full scan. Keywords are OR-required, so they alone cover a match
    even alongside a boolean query; otherwise derive a covering set from the
    (OR-combined) boolean queries. Anything FTS can't tokenize reliably
    (wildcards, CJK/Thai) forces a full scan for correctness."""
    if matcher.keywords:
        terms = set(matcher.keywords)
    elif matcher.query_strings:
        covers = [covering_terms(q) for q in matcher.query_strings]
        if any(c is None for c in covers):
            return None
        terms = set().union(*covers) if covers else set()
    else:
        return None
    if not terms or any(_FTS_UNSAFE.search(t) for t in terms):
        return None
    return terms


def _fts_match_expr(terms: set[str]) -> str:
    """FTS5 MATCH string: OR of quoted phrases (quotes doubled to escape)."""
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in sorted(terms))


def query_articles(
    db: Session,
    criteria: dict,
    sort: str = "newest",
    limit: int = 50,
    scan_cap: int = 2000,
) -> list[Article]:
    """SQL prefilter on cheap columns, then compiled criteria over candidates.

    When the criteria require text and a safe covering term-set exists, an FTS5
    index narrows candidates to articles actually containing those terms — far
    fewer rows, and it reaches the whole table instead of only the newest
    `scan_cap`. The Python matcher below still decides exact membership, so FTS
    only ever needs to return a superset."""
    matcher = CriteriaMatcher(criteria)

    stmt = select(Article).options(joinedload(Article.source))
    # Article bodies run to ~20 KB each and this scans up to `scan_cap` rows —
    # only pay to load them when some predicate actually reads the text.
    if not matcher.needs_text:
        stmt = stmt.options(defer(Article.content))

    fts_terms = _fts_terms(matcher) if matcher.needs_text else None
    if fts_terms:
        # Deliberately unbounded. Capping this subquery (ORDER BY rowid DESC
        # LIMIT n) roughly halves the cost of searching a very common term, but
        # rowid is insertion order, not publication order: a newly added source
        # backfilling old stories, or repair re-ingesting an archive, gives old
        # articles high rowids. The cap then discards the newest matches — a
        # silent recall failure, and missing a recent story is the worst thing
        # this system can do. Speed here comes from streaming instead (below).
        sub = (text("SELECT rowid FROM articles_fts WHERE articles_fts MATCH :ftsq")
               .bindparams(ftsq=_fts_match_expr(fts_terms)).columns(rowid=Article.id.type))
        stmt = stmt.where(Article.id.in_(select(sub.subquery().c.rowid)))
        scan_cap = max(scan_cap, _FTS_SCAN_CAP)
    if matcher.since:
        stmt = stmt.where(Article.published_at >= matcher.since)
    if matcher.date_from:
        stmt = stmt.where(Article.published_at >= matcher.date_from)
    if matcher.date_to:
        stmt = stmt.where(Article.published_at < matcher.date_to)
    if matcher.min_importance:
        stmt = stmt.where(Article.importance >= matcher.min_importance)
    if matcher.source_ids:
        stmt = stmt.where(Article.source_id.in_(matcher.source_ids))

    if sort == "importance":
        stmt = stmt.order_by(Article.importance.desc(), Article.published_at.desc())
    else:
        stmt = stmt.order_by(Article.published_at.desc())

    # Stream the candidates instead of materializing all of them: the loop stops
    # as soon as `limit` matches are found, so a query that fills its page early
    # pays for only the rows it read.
    def scan(cap: int) -> list[Article]:
        found: list[Article] = []
        rows = db.scalars(stmt.limit(cap).execution_options(yield_per=_STREAM_BATCH))
        for article in rows:
            if matcher.matches(article):
                found.append(article)
                if len(found) >= limit:
                    rows.close()      # stop the server-side cursor, don't drain it
                    break
        return found

    # Widen the scan only when a page couldn't be filled.
    #
    # The LIMIT is what a large scan cap actually costs: SQLite has to produce
    # the top `cap` rows in published_at order before it yields the first one, so
    # a cap of 20k builds a 20k-row sort that spills to a temp file — measured at
    # 1.9s to serve a page of 40, of which the matcher was 0.02s and the FTS
    # lookup itself 0.01s. The same query capped at 400 takes 0.33s and returns
    # the identical page, because only 40 rows are ever examined either way.
    #
    # A small cap alone would be a recall bug: predicates the SQL can't express
    # (excluded keywords, map areas, staleness) reject candidates in Python, so a
    # narrow feed may need to look at thousands of rows to find forty. Hence the
    # ladder — the last rung is the old behaviour, so nothing that used to be
    # findable stops being findable; the fast rungs just spare the common case a
    # sort it never needed.
    for cap in _SCAN_LADDER:
        if cap >= scan_cap:
            return scan(scan_cap)
        results = scan(cap)
        if len(results) >= limit:
            return results
    return scan(scan_cap)
