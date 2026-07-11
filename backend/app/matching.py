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
from sqlalchemy.orm import Session, joinedload

from .boolean_query import QueryError, compile_query
from .geo import places_match_geo
from .models import Article, Source, utcnow


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
        self.geo = self.criteria.get("geo") or None
        self.keyword_res = [_kw_regex(k) for k in (self.criteria.get("keywords") or []) if k.strip()]
        self.exclude_res = [_kw_regex(k) for k in (self.criteria.get("exclude_keywords") or []) if k.strip()]
        # Multiple boolean strings run independently and OR together: an
        # article matches if ANY compiles+matches (other criteria still AND).
        query_strings = [q for q in (self.criteria.get("queries") or []) if q.strip()]
        legacy = (self.criteria.get("query") or "").strip()
        if legacy:
            query_strings.append(legacy)
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
        # headline and feed summary.
        text = f"{article.title}\n{article.summary}\n{article.content or ''}"

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
        if self.geo and not places_match_geo(article.places or [], article.country, self.geo):
            return False
        return True


def query_articles(
    db: Session,
    criteria: dict,
    sort: str = "newest",
    limit: int = 50,
    scan_cap: int = 2000,
) -> list[Article]:
    """SQL prefilter on cheap columns, then compiled criteria over candidates."""
    matcher = CriteriaMatcher(criteria)

    stmt = select(Article).options(joinedload(Article.source))
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
    stmt = stmt.limit(scan_cap)

    results: list[Article] = []
    for article in db.scalars(stmt):
        if matcher.matches(article):
            results.append(article)
            if len(results) >= limit:
                break
    return results
