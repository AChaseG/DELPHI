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

import os
import re
from datetime import datetime, timedelta
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session, defer, joinedload, selectinload

from sqlalchemy import text

from .boolean_query import (FTS_UNSAFE, QueryError, Text, _edge, compile_query,
                            fts_expression, host_of, matches_everything,
                            plain_terms, query_terms, validate_query)
from .geo import places_match_geo
from .models import Article, Source, utcnow

# Scripts SQLite's unicode61 tokenizer doesn't word-segment — FTS can't
# reliably match these, so criteria containing them fall back to a full scan.
# Imported rather than restated: this file used to keep its own copy of the
# range list, `boolean_query` kept a second, and `scoring` a third. They
# disagreed about Hangul, which is how a Korean term came to be handed to an
# index that could not match it.
_FTS_UNSAFE = FTS_UNSAFE

# Rows fetched per round trip while scanning candidates. Small enough that a
# query filling its page early reads a fraction of the scan cap, large enough
# that a selective query doesn't pay for many round trips.
_STREAM_BATCH = 256

# The same idea on the index path, where candidates arrive as bare ids: ids are
# small, so read plenty per round trip, and fetch articles for them a page's
# worth at a time — enough that most searches hydrate exactly one batch.
_ID_BATCH = 1024
_HYDRATE_BATCH = 120

# How far the FTS path may scan before giving up on filling a page. Reached only
# by a feed whose Python-side predicates reject nearly everything the index
# offers; see the ladder in query_articles.
_FTS_SCAN_CAP = 20000
# Above this many index matches, the index is not narrowing anything.
#
# Reading the candidates the index offers costs about 2µs each; a page instead
# fills off the newest few hundred rows whenever matches are dense enough that
# forty of them turn up there — which, on a 30-day archive of a quarter of a
# million articles, is roughly one article in ten. Below that the index wins,
# above it the recency scan does, and this is where the two meet.
_FTS_NARROW_MAX = 20000

# Scan caps tried in order. Each rung is only paid for if the one before it
# couldn't fill the page, and the ladder always ends at the caller's cap.
_SCAN_LADDER = (400, 4000)

# How deep a watched-place feed reads. It has no text to hand the index — a
# place is matched by name *or* by coordinates, and the index cannot express
# the second — so the recency route is the only route, and the default 2,000
# rows is a couple of hours on a busy catalog. Measured on 50,000 articles with
# the place appearing in one in five hundred:
#
#     2,000 rows ->  4 articles,  56ms      12,000 -> 24 articles, 340ms
#     8,000 rows -> 16 articles, 218ms      20,000 -> 40 articles, 598ms
#
# Cost is linear at ~28µs a row, all of it streaming rows rather than testing
# them, so there is no cleverness to buy back. 8,000 is where four times the
# reach still costs a fifth of a second on one column.
_GEO_SCAN_CAP = 8000

# ...but a rung has to be big enough to hold the page being asked for. The
# grouped views ask for 200 articles to cluster, and a rung of 400 rows only
# holds 200 matches if half the news matches — so those queries always fell
# through to the slow route. Rungs grow with the page: measured on 244,000
# articles, a 200-article earthquake feed went 220ms → 47ms.
_ROWS_PER_MATCH = 8


def _kw_regex(kw: str) -> re.Pattern:
    """A plain keyword as a regex.

    Boundaries come from `boolean_query._edge` rather than being written here,
    because a keyword and a Boolean term are the same thing to a reader and
    must not match differently. That is also how the CJK bug survived: the
    Boolean engine, the keyword path and `scoring` each wrote their own
    boundary rule, and only one of the three had ever been told that Chinese,
    Japanese, Korean and Thai do not put spaces between words.
    """
    parts = [re.escape(p) for p in kw.split()]
    body = r"\s+".join(parts)
    return re.compile(_edge(kw, "start") + body + _edge(kw, "end"), re.IGNORECASE)


def article_text(article: Article) -> str:
    """Everything an unscoped text predicate searches: headline, summary, body."""
    return f"{article.title}\n{article.summary}\n{article.content or ''}"


# How much of an article counts as its lede.
#
# Sixty words is roughly the headline, the standfirst and the opening
# paragraph — the part a news story uses to say what it is about, and the part
# a subeditor writes last precisely so that it does. Factiva's own positional
# operator defaults to a comparable window.
LEDE_WORDS = int(os.environ.get("NEWS_LEDE_WORDS", "60"))


def lede_text(article: Article) -> str:
    """The headline, the summary, and the opening of the body."""
    opening = " ".join((article.content or "").split()[:LEDE_WORDS])
    return f"{article.title or ''}\n{article.summary or ''}\n{opening}"


def translated_text(article: Article) -> str:
    """Every stored translation of this article, as one blob.

    All languages, not the reader's. A feed is a standing question, not a
    session — it does not know who will read it, and "also search
    translations" means the article's words in whatever languages Delphi holds
    them, which is the only reading that gives the same feed the same contents
    for everybody.
    """
    parts = []
    for tr in (article.translations or []):
        if tr.title:                      # an empty title is a failure record
            parts.append(tr.title)
            if tr.summary:
                parts.append(tr.summary)
    return "\n".join(parts)


def match_fields(article: Article, source: Source | None = None,
                 translations: bool = False) -> Text:
    """The article split into the parts a scoped term can name.

    `source:` and `site:` are the two places a query may deliberately look
    outside the article, and they are deliberate: a reader who writes
    `source:Reuters` has asked for the masthead. Nothing reaches them without
    being asked for by name, which is what separates this from the accidental
    matching that put an outlet's own name in every one of its summaries.
    """
    source = source or article.source
    summary_and_body = f"{article.summary}\n{article.content or ''}"
    # A translated headline is still a headline: a reader asking for
    # `intitle:earthquake` wants the Japanese story whose headline says so,
    # and putting the translation only in the body would answer a different
    # question.
    extra = "\n" + translated_text(article) if translations else ""
    return Text(
        all=f"{article.title}\n{summary_and_body}{extra}",
        headline=(article.title or "") + extra,
        text=summary_and_body + extra,
        lede=lede_text(article) + extra,
        source=(source.name if source else "") or "",
        site=host_of(article.url or ""),
    )


# How many times a lone word has to appear in the body before it counts as what
# the article is about. Twice, because once is what a passing mention looks
# like: a colour called "Solar Yellow" in a kit review, a song called "Innocent
# Wind" in a concert listing, "coal" in the name of a road. A story that is
# actually about the thing says the word again.
_PROMINENT_REPEATS = 2

# The most a feed may raise it to. A word demanded thirty times is not a
# prominence rule any more, it is a way to build a feed that never fills and
# cannot be debugged from the outside.
MAX_MENTIONS = int(os.environ.get("NEWS_MAX_MENTIONS", "10"))


def _count_upto(pattern: re.Pattern, text: str, limit: int) -> int:
    """Occurrences, stopping as soon as `limit` is reached."""
    seen = 0
    for _ in pattern.finditer(text):
        seen += 1
        if seen >= limit:
            break
    return seen


def explain_text_match(criteria: dict, article: Article) -> list[dict]:
    """Which words put this article in this feed, and where they are.

    A reader who opens a result and cannot find any of their terms in it has no
    way to tell a broken search from a term sitting somewhere they are not
    looking. Both happen, and they are fixed in completely different places, so
    the answer has to say *where*: a phrase in the headline is the search
    working, and the same phrase found only in the body is usually the page's
    own furniture — a section link, a "more from" rail, a newsletter promo —
    which is worth knowing because it is not visible from the card.

    Returns one entry per matching term: the term, the field it was found in,
    and enough of the surrounding text to recognise it.
    """
    fields = (("headline", article.title or ""),
              ("summary", article.summary or ""),
              ("body", article.content or ""))

    terms: list[tuple[str, re.Pattern]] = []
    for kw in (criteria or {}).get("keywords") or []:
        if kw.strip():
            terms.append((kw, _kw_regex(kw)))
    queries = [q for q in ((criteria or {}).get("queries") or []) if q.strip()]
    legacy = ((criteria or {}).get("query") or "").strip()
    if legacy:
        queries.append(legacy)
    for q in queries:
        terms.extend(query_terms(q))

    out: list[dict] = []
    for term, pattern in terms:
        for field, value in fields:
            m = pattern.search(value)
            if not m:
                continue
            start, end = max(0, m.start() - 60), min(len(value), m.end() + 60)
            snippet = " ".join(value[start:end].split())
            out.append({
                "term": term,
                "where": field,
                "snippet": ("…" if start else "") + snippet + ("…" if end < len(value) else ""),
                # How often it is said at all. One mention deep in a 20 KB page
                # is what an incidental match looks like, and the reader asking
                # "why is this here" is owed that number rather than left to
                # infer it from a single quoted line.
                "count": _count_upto(pattern, article_text(article), 9),
            })
            break        # the first place it appears is the one worth showing

    # A query that does not parse has nothing to say about anything, and
    # guessing on its behalf would be worse than silence.
    usable = [q for q in queries if validate_query(q) is None]
    if not out and (usable or terms):
        # Nothing of the query is in the article, and the article is here
        # anyway. That is not a mystery to leave with the reader: it happens
        # when a query has an alternative that is only an exclusion, so the
        # branch that admitted this article was "not something else" and none
        # of the reader's terms ever had to appear.
        widest = next((q for q in usable if matches_everything(q)), "")
        if widest:
            out.append({
                "term": widest,
                "where": "query",
                "snippet": ("This query has an alternative that is only an "
                            "exclusion, so it matches every article that is "
                            "not excluded. None of your terms are in this "
                            "article, and none had to be."),
                "count": 0,
            })
        else:
            out.append({
                "term": "",
                "where": "elsewhere",
                "snippet": ("None of the query's words are in this article's "
                            "headline, summary or body. It is here on a "
                            "proximity pair, an exclusion, or one of the "
                            "feed's non-text filters — country, category, "
                            "source or area."),
                "count": 0,
            })
    return out


def headline_text(article: Article) -> str:
    """Headline and summary only — no body.

    What a watched place is matched against. Two reasons, and they agree: a
    story *about* somewhere names it up front, while a passing mention buried
    in the body is usually somebody's dateline or a list of other places; and
    leaving the body out keeps it deferred in SQL, which is what lets a
    location feed scan deeply enough to find anything at all.
    """
    return f"{article.title}\n{article.summary}"


@lru_cache(maxsize=1024)
def _place_re(name: str) -> re.Pattern | None:
    """Word-boundary matcher for a place name, compiled once per name."""
    name = (name or "").strip()
    # Two characters match half the world; a place that short is not a signal.
    return _kw_regex(name) if len(name) >= 3 else None


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
        # Opt-in, and off by default on purpose: switching it on widens every
        # existing feed, and that should be the reader's decision rather than
        # a surprise the next time they open the board.
        self.search_translations = bool(self.criteria.get("search_translations"))
        # Factiva exposes this as `atleast10 term` and Delphi kept it hidden at
        # two. Two is right for most feeds and wrong for a feed built on a word
        # that is common in another sense — the reader who hit that had no dial
        # to turn, only a heuristic they could not see.
        self.min_mentions = max(1, min(MAX_MENTIONS,
                                       int(self.criteria.get("min_mentions") or
                                           _PROMINENT_REPEATS)))
        # How many outlets must be carrying the story.
        #
        # The lesson from how Dataminr is built, in the cheapest form Delphi
        # can take it. Their pipeline detects events by corroboration across
        # sources first and applies the reader's boolean afterwards, as a
        # filter on things already judged real — which is why their queries do
        # not need prominence heuristics. Delphi's boolean *is* the detector,
        # so a single outlet's passing mention has always been enough.
        #
        # Delphi already clusters coverage and already counts how many outlets
        # are in a cluster; that number simply played no part in matching. Now
        # a feed can ask for it, and "only stories somebody else is also
        # running" turns out to suppress a whole class of noise with no new
        # heuristic at all.
        self.min_sources = max(0, int(self.criteria.get("min_sources") or 0))
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

        # Every literal word or phrase the reader asked for, across keywords and
        # every boolean query, with the pattern that finds it. Used to judge
        # whether the words that matched are what the article is about — see
        # `_is_about_it`. NEAR pairs have no single literal to point at and are
        # left out, exactly as they are when explaining a match.
        # De-duplicated, because the same word often arrives twice — once as a
        # keyword and once inside a query — and two entries for one word would
        # let it count as "two of the words agree" with itself.
        self.terms: list[tuple[str, re.Pattern]] = []
        seen_terms: set[str] = set()
        for term, pattern in (list(zip(self.keywords, self.keyword_res))
                              + [t for q in query_strings for t in plain_terms(q)]):
            key = " ".join(term.lower().split())
            if key and key not in seen_terms:
                seen_terms.add(key)
                self.terms.append((term, pattern))
        # Opt out per feed: keep articles where a term appears once, in the
        # body, and nowhere else. Off by default, because that is the shape of
        # a false match rather than a story.
        self.passing_mentions = bool(self.criteria.get("passing_mentions"))

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

    def matches(self, article: Article, source: Source | None = None,
                text: Text | str | None = None) -> bool:
        """Does this article satisfy the criteria?

        `text` is what the text predicates search — a `Text`, which is the
        article split into the parts `intitle:`, `intext:`, `source:` and
        `site:` can name. Pass it when testing one article against many
        criteria: it runs to 20 KB, and assembling it once per matcher is the
        bulk of evaluating a board's worth of alerts. A plain string is accepted
        and read as an article with no fields."""
        source = source or article.source
        # Full recall: criteria see the fetched article body, not just the
        # headline and feed summary. Assembled only when a predicate needs it —
        # touching .content would otherwise force-load a deferred column.
        if text is None:
            fields = (match_fields(article, source, self.search_translations)
                      if self.needs_text else Text())
        elif isinstance(text, Text):
            fields = text
        else:
            fields = Text.plain(text)
        text = fields.all

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
        if self.query_preds and not any(pred(fields) for pred in self.query_preds):
            return False
        if not self.passing_mentions and self.terms and not self._is_about_it(article, text):
            return False
        if self.geos and not any(self._in_area(article, g) for g in self.geos):
            return False
        if self.min_sources > 1 and not self._corroborated(article):
            return False
        return True

    def _corroborated(self, article: Article) -> bool:
        """Are enough separate outlets carrying this story?

        Counted over the article's cluster, because that is what "the story"
        means here — one outlet filing three times is one outlet, and the
        cluster already knows the difference. An article in no cluster has one
        source by definition, which is the honest answer rather than a
        generous one: nothing has corroborated it yet.

        Deliberately not a live query. `Event.article_count` is maintained on
        the way in and the source count is read from the same row, so this
        costs nothing per candidate — a feed that scanned thousands of rows and
        issued a query for each would be a different kind of feature.
        """
        event = article.event
        if event is None:
            return self.min_sources <= 1
        # A cluster from before this column existed has no list. Its
        # article_count is the only thing known about how widely it was
        # carried, and reading it as an upper bound admits a few stories a
        # stricter answer would drop — which is the right way round: a filter
        # that silently emptied every feed on old data would be far worse than
        # one that is briefly generous about it.
        ids = event.source_ids
        if ids is None:
            return (event.article_count or 1) >= self.min_sources
        return len(ids) >= self.min_sources

    def _is_about_it(self, article: Article, text: str) -> bool:
        """Is one of the words that matched what this article is about?

        The boolean has already said yes. This asks a second, narrower question,
        and it exists because of a class of result that is correct and useless:
        a football-kit review in an energy feed, because a colourway is called
        "Solar Yellow"; a concert listing in the same feed, because a song is
        called "Innocent Wind". The word really is there. Nothing about the
        article is.

        No dictionary of senses could settle those — "solar" means what the
        sentence around it means — so the test is about *prominence* instead,
        and it is the same judgement a person makes skimming a page:

          · a phrase passes on its own. "Power plant" is not said in passing.
          · a word in the headline or the summary passes. That is where a story
            announces what it is about.
          · two of the query's words agreeing passes. An energy story says
            "solar" and "grid"; a kit review says one energy word, once.
          · otherwise a lone word has to be used more than once in the body.

        Everything a reader wants keeps arriving: a story about a solar farm
        says "solar" in its headline, and if it somehow doesn't, it says it
        again in the second paragraph. What stops arriving is the single
        incidental mention — which is the whole of the complaint.
        """
        # Headline and summary first, and not only because that is the strongest
        # signal: it is a few hundred characters against a body of up to 20,000,
        # and nearly every article a reader wants is answered here. Measured on
        # 20 KB bodies, going to the body first doubled the cost of matching an
        # article; this way the common case is microseconds and only the
        # articles about to be dropped pay for a second pass.
        # Above the default, a headline mention is no longer a free pass and
        # neither is a phrase: a reader who asked for five is saying "one
        # mention is what I am trying to exclude", so the count is taken over
        # the whole article and nothing shortcuts it. Below that, the shortcuts
        # stay — they are why the default is cheap.
        if self.min_mentions > _PROMINENT_REPEATS:
            whole = article_text(article)
            return any(_count_upto(p, whole, self.min_mentions) >= self.min_mentions
                       for _t, p in self.terms)

        head = headline_text(article)
        for _term, pattern in self.terms:
            if pattern.search(head):
                return True                      # named where it is announced

        body = article.content or ""
        for i, (term, pattern) in enumerate(self.terms):
            if not pattern.search(body):
                continue
            if " " in term.strip():
                return True                      # a phrase is specific already
            # Cheapest question next: is it said again? The second occurrence of
            # a word a story is about is usually a line or two away, so this
            # normally stops early, where looking for a *different* word means
            # reading the whole body once per remaining term.
            if _count_upto(pattern, body, self.min_mentions) >= self.min_mentions:
                return True
            return any(p.search(body) for _t, p in self.terms[i + 1:])
        # Nothing literal matched: the article is here on a NEAR pair, or on a
        # NOT branch. Neither is a passing mention, and neither is this test's
        # business.
        return True

    def _in_area(self, article: Article, area: dict) -> bool:
        """Is this article about that place — by coordinates or by name?

        Coordinates alone were not enough, and the reason is the gazetteer:
        an article is only given a position when its text names one of the 577
        cities Delphi ships. Anything else gets no position, so it can never
        fall inside a circle, so a watched place that is not one of those 577
        matched nothing at all — permanently, not rarely. The picker meanwhile
        offers any address OpenStreetMap knows, which is most of the ways to
        choose a place that can never work.

        So the name the place was picked under is checked too. A headline
        saying "Reading" is about Reading whether or not Delphi holds a
        coordinate for it, and that is the whole of what a reader means by
        watching somewhere.

        The country, when the pick knew it, keeps Reading in England apart from
        Reading in Pennsylvania — but only when the article states a country
        at all, since most do not and rejecting those would trade one kind of
        empty column for another.
        """
        # Name first, geography second, and the order is a measurement rather
        # than a preference: the name test is a regex over a short string,
        # while the geographic test walks the article's tagged places. Most
        # rows in a deep scan fail both, so the cheaper question goes first.
        pattern = _place_re(area.get("name") or "")
        if pattern is not None and pattern.search(headline_text(article)):
            wanted = (area.get("country") or "").upper()
            if not (wanted and article.country and article.country.upper() != wanted):
                return True
        return places_match_geo(article.places or [], article.country, area)


def _fts_expr(matcher: CriteriaMatcher) -> str | None:
    """The whole criteria as one FTS5 MATCH expression matching a superset.

    Keywords are OR-required, so they alone cover a match even alongside a
    boolean query. Otherwise every query has to compile: they are OR-combined,
    and one unbounded branch makes the disjunction unbounded."""
    if matcher.keywords:
        terms = set(matcher.keywords)
        if any(_FTS_UNSAFE.search(t) for t in terms):
            return None
        return _fts_match_expr(terms) or None
    if not matcher.query_strings:
        return None
    parts = [fts_expression(q) for q in matcher.query_strings]
    if not parts or any(p is None for p in parts):
        return None
    return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"


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

    Two routes lead to the same page. The index route asks FTS5 which articles
    contain the required text and reads those — few rows, and it reaches the
    whole table rather than only the newest `scan_cap`. The recency route reads
    the newest articles in order and lets the matcher sift them, which is
    cheaper whenever the text is common enough to turn up there. Either way the
    SQL only has to return a superset: the Python matcher decides membership."""
    matcher = CriteriaMatcher(criteria)

    # Bodies stay deferred for a watched place (names are matched on the
    # headline), which is what makes reading this far affordable.
    if matcher.geos and not matcher.needs_text:
        scan_cap = max(scan_cap, _GEO_SCAN_CAP)

    base = select(Article).options(joinedload(Article.source))
    # Article bodies run to ~20 KB each and this scans up to `scan_cap` rows —
    # only pay to load them when some predicate actually reads the text.
    if not matcher.needs_text:
        base = base.options(defer(Article.content))
    if matcher.search_translations:
        # One extra query per batch instead of one per article. Left lazy, a
        # feed scanning a few thousand candidates would issue a few thousand
        # round trips, which is the difference between a fast feed and a feed
        # nobody waits for.
        base = base.options(selectinload(Article.translations))

    # Which way round to work — ask the index, or read the newest articles and
    # let the matcher sift them — comes down to one number: how many articles
    # the index would offer. Counting them is bounded, so it costs about a
    # millisecond either way, and it is the difference between 2ms and 20ms for
    # an unusual phrase and between 180ms and 8ms for an everyday word.
    # The index holds the article's own words and not its translations, so a
    # feed searching translations must not be narrowed by it: the index would
    # return fewer rows than match, which is the superset guarantee broken —
    # the same failure the Hangul gap caused, arriving by a different door.
    fts_expr = (_fts_expr(matcher)
                if matcher.needs_text and not matcher.search_translations else None)
    index_narrows = False
    if fts_expr:
        offered = db.execute(
            text("SELECT count(*) FROM (SELECT rowid FROM articles_fts "
                 "WHERE articles_fts MATCH :ftsq LIMIT :cap)"),
            {"ftsq": fts_expr, "cap": _FTS_NARROW_MAX + 1},
        ).scalar() or 0
        index_narrows = offered <= _FTS_NARROW_MAX

    def _fts_where():
        """The condition "this article is one the index matched".

        Deliberately unbounded. Capping this subquery (ORDER BY rowid DESC
        LIMIT n) roughly halves the cost of searching a very common term, but
        rowid is insertion order, not publication order: a newly added source
        backfilling old stories, or repair re-ingesting an archive, gives old
        articles high rowids. The cap then discards the newest matches — a
        silent recall failure, and missing a recent story is the worst thing
        this system can do."""
        sub = (text("SELECT rowid FROM articles_fts WHERE articles_fts MATCH :ftsq")
               .bindparams(ftsq=fts_expr).columns(rowid=Article.id.type))
        return Article.id.in_(select(sub.subquery().c.rowid))

    conds = []
    if matcher.since:
        conds.append(Article.published_at >= matcher.since)
    if matcher.date_from:
        conds.append(Article.published_at >= matcher.date_from)
    if matcher.date_to:
        conds.append(Article.published_at < matcher.date_to)
    if matcher.min_importance:
        conds.append(Article.importance >= matcher.min_importance)
    if matcher.source_ids:
        conds.append(Article.source_id.in_(matcher.source_ids))

    # Id breaks ties last. Outlets publish on the minute, so thousands of
    # articles share a timestamp exactly, and without a tiebreak which of them
    # lands in the fortieth slot is up to whichever route the search took —
    # the same search could return a slightly different page each time. Ids
    # descend with publication order in the index, so this costs nothing.
    order = ((Article.importance.desc(), Article.published_at.desc(), Article.id.desc())
             if sort == "importance"
             else (Article.published_at.desc(), Article.id.desc()))

    # Stream the candidates instead of materializing all of them: the loop stops
    # as soon as `limit` matches are found, so a query that fills its page early
    # pays for only the rows it read.
    def scan(cap: int) -> list[Article]:
        found: list[Article] = []
        rows = db.scalars(base.where(*conds).order_by(*order).limit(cap)
                          .execution_options(yield_per=_STREAM_BATCH))
        for article in rows:
            if matcher.matches(article):
                found.append(article)
                if len(found) >= limit:
                    rows.close()      # stop the server-side cursor, don't drain it
                    break
        return found

    def scan_indexed(cap: int) -> list[Article]:
        """The same scan, but ordering identifiers rather than articles.

        Restricting to what the index matched costs SQLite its index on
        publication date, so it sorts the candidates itself — and in one query
        that sort carries whole articles, bodies and all, through a temp B-tree
        only to discard all but a page of them. Ordering bare ids and then
        fetching the few that a page actually needs measured 96ms → 24ms on a
        5,174-candidate search of 244,000 articles."""
        ids = db.scalars(select(Article.id).where(*conds).where(_fts_where())
                         .order_by(*order).limit(cap)
                         .execution_options(yield_per=_ID_BATCH))
        found: list[Article] = []
        batch: list[int] = []

        def take(batch: list[int]) -> None:
            by_id = {a.id: a for a in db.scalars(base.where(Article.id.in_(batch)))}
            for article_id in batch:       # the batch is already in scan order
                article = by_id.get(article_id)
                if article is not None and matcher.matches(article):
                    found.append(article)

        for article_id in ids:
            batch.append(article_id)
            if len(batch) >= _HYDRATE_BATCH:
                take(batch)
                batch = []
                if len(found) >= limit:
                    ids.close()
                    return found[:limit]
        if batch:
            take(batch)
        return found[:limit]

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
    # ladder — its last rung reaches as far as the search ever did, so nothing
    # that used to be findable stops being findable; the fast rungs just spare
    # the common case a sort it never needed.
    if index_narrows:
        # The index offers few enough articles to be worth reading whole: there
        # is nothing for a ladder to climb, and a rung would be pure overhead —
        # a search this selective has almost nothing among the newest rows.
        return scan_indexed(max(scan_cap, _FTS_SCAN_CAP))

    # Sorted by date and reading rows in date order, a rung has nothing to
    # offer: the stream already stops at a full page, so a cap of 400 and a cap
    # of 2,000 cost exactly the same, and a rung that comes up short has only
    # re-read its rows (measured: 75ms → 117ms on a category feed). Rungs are
    # for the two cases where a cap really is the cost — an ordering SQLite has
    # to build in a temp B-tree, and a short page meaning "switch to the index".
    ladder = _SCAN_LADDER if (fts_expr or sort == "importance") else ()
    for rung in ladder:
        cap = max(rung, _ROWS_PER_MATCH * limit)
        if cap >= scan_cap:
            break
        results = scan(cap)
        if len(results) >= limit:
            return results
    if fts_expr:
        # A page the recency rungs could not fill, on text the index was too
        # blunt to narrow: the remaining matches are older than the rows read so
        # far. Ask the index anyway, and skip the plain deep scan on the way —
        # the index returns a superset of every match in the whole table, so it
        # reaches strictly further than the newest `scan_cap` rows would, and
        # paying for both was a wasted 20,000-row sort (measured: 170ms).
        return scan_indexed(max(scan_cap, _FTS_SCAN_CAP))
    return scan(scan_cap)
