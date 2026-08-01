"""FTS5 keyword/text search: correctness (superset + matcher), recall, sync (Gap C)."""
from datetime import timedelta

import pytest

from backend.app import matching
from backend.app.matching import CriteriaMatcher, _fts_expr, query_articles
from backend.app.models import Article, Source, utcnow


@pytest.fixture
def seeded(db):
    s = Source(name="S", rss_url="http://x/f", scope="national", tier=2, added_by="user")
    db.add(s)
    db.flush()
    rows = [
        ("Major earthquake strikes Tokyo", "tsunami warning issued", "quake body text"),
        ("Stock market rallies on tech earnings", "shares climb", "markets body"),
        ("Ceasefire talks resume in the region", "diplomats meet", "supply chain disruption"),
    ]
    for i, (t, su, co) in enumerate(rows):
        db.add(Article(source_id=s.id, url=f"http://x/{i}", title=t, summary=su, content=co,
                       published_at=utcnow(), importance=50, language="en"))
    db.commit()
    return db, s


def _ids(db, crit):
    return sorted(a.id for a in query_articles(db, crit, limit=500, scan_cap=100000))


def _brute(db, crit):
    m = CriteriaMatcher(crit)
    return sorted(a.id for a in db.query(Article).all() if m.matches(a))


@pytest.mark.parametrize("crit", [
    {"keywords": ["earthquake"]},
    {"keywords": ["tsunami", "ceasefire"]},
    {"query": '"supply chain"'},                       # phrase, body-only
    {"query": "earthquake AND tokyo"},
    {"query": "earthquake OR ceasefire"},
    {"query": "NOT earthquake"},                        # unbounded → scan fallback
    {"keywords": ["earthquake"], "exclude_keywords": ["tokyo"]},
    {"keywords": ["strik*"]},                           # wildcard → scan fallback
])
def test_fts_matches_bruteforce(seeded, crit):
    db, _ = seeded
    assert _ids(db, crit) == _brute(db, crit)


def test_routing(seeded):
    """Which criteria the index can be asked about at all. None means "scan" —
    for anything the index might read differently from the Python matcher, and
    for anything with no positive requirement to hand it."""
    db, _ = seeded
    assert _fts_expr(CriteriaMatcher({"keywords": ["earthquake"]})) == '"earthquake"'
    assert _fts_expr(CriteriaMatcher({"query": "a AND b"})) == '("a" AND "b")'
    # Keywords are OR-required, so they cover a match on their own.
    assert _fts_expr(CriteriaMatcher(
        {"keywords": ["earthquake"], "query": "strik*"})) == '"earthquake"'
    # Independent queries OR together, so one unbounded branch loses the lot.
    assert _fts_expr(CriteriaMatcher(
        {"queries": ["a AND b", "c"]})) == '(("a" AND "b") OR "c")'
    assert _fts_expr(CriteriaMatcher({"queries": ["a", "NOT b"]})) is None
    assert _fts_expr(CriteriaMatcher({"query": "NOT rumor"})) is None        # unbounded
    assert _fts_expr(CriteriaMatcher({"query": "strik*"})) is None           # wildcard
    assert _fts_expr(CriteriaMatcher({"keywords": ["東京"]})) is None         # CJK unsafe
    assert _fts_expr(CriteriaMatcher({"exclude_keywords": ["x"]})) is None   # NOT-only


def test_recall_beyond_old_scan_cap(db):
    # A rare keyword in the OLDEST article among many: the pre-FTS code capped
    # the scan at the newest 2000 rows and would miss it; FTS reaches it.
    s = Source(name="S", rss_url="http://x/f", scope="national", tier=2, added_by="user")
    db.add(s)
    db.flush()
    from datetime import timedelta
    now = utcnow()
    db.add(Article(source_id=s.id, url="http://x/rare", title="Unique aardvark dispatch",
                   summary="", content="", published_at=now - timedelta(days=20),
                   importance=50, language="en"))
    for i in range(2500):  # bury it under newer noise
        db.add(Article(source_id=s.id, url=f"http://x/n{i}", title="routine market update",
                       summary="", content="", published_at=now - timedelta(minutes=i),
                       importance=30, language="en"))
    db.commit()
    hits = query_articles(db, {"keywords": ["aardvark"]}, limit=10, scan_cap=2000)
    assert len(hits) == 1 and hits[0].title == "Unique aardvark dispatch"


def test_index_syncs_on_content_update_and_delete(seeded):
    db, s = seeded
    # A keyword present only in a body added AFTER insert is found (UPDATE trigger).
    art = Article(source_id=s.id, url="http://x/late", title="Placeholder", summary="",
                  content="", published_at=utcnow(), importance=50, language="en")
    db.add(art)
    db.commit()
    assert query_articles(db, {"keywords": ["zebra"]}, limit=10) == []
    art.content = "a wild zebra appeared downtown"
    db.commit()
    hits = query_articles(db, {"keywords": ["zebra"]}, limit=10)
    assert len(hits) == 1 and hits[0].id == art.id
    # Deleting the row drops it from the index (DELETE trigger).
    db.delete(art)
    db.commit()
    assert query_articles(db, {"keywords": ["zebra"]}, limit=10) == []


def test_common_term_search_returns_the_newest_matches(db):
    """A term present in most of the corpus must still return the newest
    articles first. Guards the FTS prefilter against being bounded by rowid
    (insertion order) as a speed optimization: articles arrive out of
    publication order, so such a cap silently drops the most recent hits."""
    from datetime import timedelta

    from backend.app import matching
    from backend.app.models import Article, Source, utcnow

    src = Source(name="S", rss_url="http://s.local/f")
    db.add(src)
    db.flush()

    # Inserted oldest-id-first but newest-published-first, the arrangement a
    # rowid-ordered prefilter gets wrong.
    now = utcnow()
    for i in range(40):
        db.add(Article(
            source_id=src.id, url=f"http://s.local/a/{i}", guid=str(i),
            title=f"Report {i} about flooding", summary="", content="",
            published_at=now - timedelta(hours=i), fetched_at=now,
            language="en", country="US", categories=[], places=[], importance=50))
    db.commit()

    got = matching.query_articles(db, {"keywords": ["flooding"]}, sort="newest", limit=5)
    assert [a.title for a in got] == [f"Report {i} about flooding" for i in range(5)]


@pytest.fixture
def deep(db):
    """A corpus where the index offers far more than the page needs, and where
    Python-side predicates reject most of it — the shape that decides whether
    the scan ladder can truncate a result."""
    from backend.app.models import Source as S
    src = S(name="S", rss_url="http://d/f", scope="national", tier=2)
    db.add(src)
    db.flush()
    for i in range(1500):
        # Every article carries "common"; only every 50th also carries "needle".
        words = "common routine filler"
        if i % 50 == 0:
            words += " needle"
        db.add(Article(source_id=src.id, url=f"http://d/{i}", guid=str(i),
                       title=f"Report {i}", summary=words, content=words,
                       published_at=utcnow() - timedelta(minutes=i),
                       importance=50, language="en", country="US",
                       categories=["world"], places=[]))
    db.commit()
    return db


def _laddered_and_full(db, crit, sort="newest", limit=40):
    """The same query the normal way, and with the ladder disabled so it goes
    straight to the full cap — i.e. the behaviour before the ladder existed."""
    laddered = [a.id for a in query_articles(db, crit, sort=sort, limit=limit)]
    db.expire_all()
    saved = matching._SCAN_LADDER
    matching._SCAN_LADDER = ()
    try:
        full = [a.id for a in query_articles(db, crit, sort=sort, limit=limit)]
    finally:
        matching._SCAN_LADDER = saved
    db.expire_all()
    return laddered, full


@pytest.mark.parametrize("label,crit,sort", [
    ("common term", {"keywords": ["common"]}, "newest"),
    ("common term by importance", {"keywords": ["common"]}, "importance"),
    # Needs to scan ~50 rows per hit, so the first rung cannot fill the page.
    ("sparse term needs a deep scan", {"keywords": ["needle"]}, "newest"),
    # Python-side exclusion rejects everything the index offers.
    ("exclusion rejects every candidate",
     {"keywords": ["common"], "exclude_keywords": ["filler"]}, "newest"),
    ("boolean requiring both", {"queries": ["common AND needle"]}, "newest"),
])
def test_scan_ladder_does_not_change_results(deep, label, crit, sort):
    """Widening the scan only when a page is short must be invisible: the fast
    path has to return exactly what an unconditional full scan returns, or the
    optimization is a silent recall bug."""
    laddered, full = _laddered_and_full(deep, crit, sort=sort)
    assert laddered == full, label


def _by_route(db, crit, sort="newest", limit=40):
    """The same query down each route: candidates chosen by the index, and
    candidates read newest-first and sifted. The search picks between them on
    how much the index would narrow things; both have to agree."""
    saved = matching._FTS_NARROW_MAX
    pages = []
    try:
        for threshold in (10 ** 9, 0):        # always index, then never index
            matching._FTS_NARROW_MAX = threshold
            pages.append([a.id for a in
                          query_articles(db, crit, sort=sort, limit=limit)])
            db.expire_all()
    finally:
        matching._FTS_NARROW_MAX = saved
    return pages


@pytest.mark.parametrize("label,crit,sort", [
    # "common" is in every article: the probe should decline the index here.
    ("blunt term", {"keywords": ["common"]}, "newest"),
    ("blunt term by importance", {"keywords": ["common"]}, "importance"),
    # "needle" is in 30 of 1500: selective enough for the index to earn its keep.
    ("selective term", {"keywords": ["needle"]}, "newest"),
    ("selective boolean", {"queries": ["common AND needle"]}, "newest"),
    ("blunt term with an exclusion",
     {"keywords": ["common"], "exclude_keywords": ["filler"]}, "newest"),
])
def test_skipping_a_blunt_index_does_not_change_results(deep, label, crit, sort):
    """Deciding whether to consult the index has to be invisible in the output.
    Both routes return a superset the matcher then judges, so the page they
    produce must be identical — otherwise the faster route is a recall bug."""
    indexed, recency = _by_route(deep, crit, sort=sort)
    assert indexed == recency, label


@pytest.mark.parametrize("sort", ["newest", "importance"])
def test_articles_sharing_a_timestamp_come_back_in_a_fixed_order(db, sort):
    """Outlets publish on the minute, so a page boundary usually falls in the
    middle of a group of articles with identical timestamps. Which of them makes
    the page has to be settled, or the same search returns different pages
    depending on which route it took."""
    src = Source(name="S", rss_url="http://t/f", scope="national", tier=2)
    db.add(src)
    db.flush()
    stamp = utcnow()
    for i in range(30):                      # every one published at the same instant
        db.add(Article(source_id=src.id, url=f"http://t/{i}", guid=str(i),
                       title=f"Report {i} on flooding", summary="", content="",
                       published_at=stamp, importance=50, language="en",
                       country="US", categories=[], places=[]))
    db.commit()

    page = [a.id for a in query_articles(db, {"keywords": ["flooding"]},
                                         sort=sort, limit=10)]
    assert page == sorted(page, reverse=True)      # newest arrival first
    indexed, recency = _by_route(db, {"keywords": ["flooding"]}, sort=sort, limit=10)
    assert indexed == recency == page


def test_blunt_term_still_reaches_past_the_scan_cap(deep):
    """The tail of the ladder. When a term is too common for the index to
    narrow, the recency rungs run first — but a match older than everything
    they read must still be found, so the search falls back to the index."""
    src_id = deep.query(Article).first().source_id
    buried = Article(source_id=src_id, url="http://d/buried", guid="buried",
                     title="Report from far back", summary="common routine",
                     content="common routine", country="FR", language="en",
                     categories=["world"], places=[],
                     published_at=utcnow() - timedelta(days=30), importance=50)
    deep.add(buried)
    deep.commit()
    got = query_articles(deep, {"keywords": ["common"], "countries": ["FR"]},
                         limit=40, scan_cap=500)
    assert [a.id for a in got] == [buried.id]


def _fts_queries(db, fn):
    """Run `fn` and count how many statements went to the search index."""
    from sqlalchemy import event
    seen = []

    def spy(conn, cursor, statement, *rest):
        if "articles_fts" in statement:
            seen.append(statement)

    bind = db.get_bind()
    event.listen(bind, "before_cursor_execute", spy)
    try:
        return fn(), seen
    finally:
        event.remove(bind, "before_cursor_execute", spy)


def test_a_rung_can_hold_the_page_it_is_asked_for(deep):
    """The grouped views ask for 200 articles to cluster. A rung fixed at 400
    rows holds that many matches only if half the news matches, so those queries
    fell through to the index every time. Rungs grow with the page instead: all
    1,500 articles here carry "common", so the newest rows answer this outright
    and the index is only asked the one question that routes the search."""
    saved = matching._FTS_NARROW_MAX
    matching._FTS_NARROW_MAX = 0     # a corpus too big for the index to narrow
    try:
        got, fts = _fts_queries(
            deep, lambda: query_articles(deep, {"keywords": ["common"]}, limit=200))
    finally:
        matching._FTS_NARROW_MAX = saved
    assert len(got) == 200
    assert len(fts) == 1, fts        # the selectivity probe, and nothing after it


def test_sparse_term_still_fills_its_page(deep):
    """The guard for the ladder's premise: a term appearing once every 50
    articles must still return a full page, which means rung one (400 rows)
    is not where the search stops."""
    got = query_articles(deep, {"keywords": ["needle"]}, limit=25)
    assert len(got) == 25
