"""FTS5 keyword/text search: correctness (superset + matcher), recall, sync (Gap C)."""
import pytest

from backend.app.matching import CriteriaMatcher, _fts_terms, query_articles
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
    db, _ = seeded
    assert _fts_terms(CriteriaMatcher({"keywords": ["earthquake"]}))          # uses FTS
    assert _fts_terms(CriteriaMatcher({"query": "a AND b"}))                  # covering set
    assert _fts_terms(CriteriaMatcher({"query": "NOT rumor"})) is None        # unbounded
    assert _fts_terms(CriteriaMatcher({"query": "strik*"})) is None           # wildcard
    assert _fts_terms(CriteriaMatcher({"keywords": ["東京"]})) is None         # CJK unsafe
    assert _fts_terms(CriteriaMatcher({"exclude_keywords": ["x"]})) is None   # NOT-only


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
