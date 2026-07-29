"""Restricting a feed or alert to chosen sources (criteria.source_ids).

The picker in the wizard is what changed, but the criterion it writes is what
has to keep working — including the part that matters for a big catalog: the
restriction must narrow the SQL, not just filter in Python afterwards.
"""
import pytest

from backend.app.matching import CriteriaMatcher, query_articles
from backend.app.models import Article, Source, utcnow


@pytest.fixture
def three_sources(db):
    srcs = []
    for i, name in enumerate(("Wire A", "Wire B", "Wire C")):
        s = Source(name=name, rss_url=f"http://s{i}/feed", scope="international",
                   country="US", language="en")
        db.add(s)
        srcs.append(s)
    db.flush()
    for s in srcs:
        for n in range(3):
            db.add(Article(
                source_id=s.id, url=f"http://s{s.id}/a{n}", guid=f"{s.id}-{n}",
                title=f"Flood report {n} from {s.name}", summary="", content="",
                published_at=utcnow(), fetched_at=utcnow(),
                language="en", country="US", categories=["world"], places=[],
                importance=50))
    db.commit()
    return srcs


def test_no_source_ids_means_every_source(db, three_sources):
    got = query_articles(db, {}, limit=50)
    assert len({a.source_id for a in got}) == 3


def test_restriction_keeps_only_the_chosen_sources(db, three_sources):
    a, b, _c = three_sources
    crit = {"source_ids": [a.id, b.id]}
    got = query_articles(db, crit, limit=50)
    assert got, "the chosen sources do publish matching articles"
    assert {art.source_id for art in got} == {a.id, b.id}


def test_restriction_combines_with_other_criteria(db, three_sources):
    """It narrows alongside the rest rather than replacing them — a feed that
    restricts sources and asks for a keyword must satisfy both."""
    a = three_sources[0]
    db.add(Article(source_id=a.id, url="http://s/none", guid="none",
                   title="Unrelated harvest festival", summary="", content="",
                   published_at=utcnow(), fetched_at=utcnow(), language="en",
                   country="US", categories=["world"], places=[], importance=50))
    db.commit()
    got = query_articles(db, {"source_ids": [a.id], "keywords": ["flood"]}, limit=50)
    assert got
    assert all(art.source_id == a.id for art in got)
    assert all("flood" in art.title.lower() for art in got)


def test_unknown_source_id_matches_nothing(db, three_sources):
    """A feed pointed at a source that has since been deleted comes back empty
    rather than falling back to every source."""
    ghost = max(s.id for s in three_sources) + 999
    assert query_articles(db, {"source_ids": [ghost]}, limit=50) == []


def test_matcher_agrees_with_the_query(db, three_sources):
    """query_articles narrows in SQL; CriteriaMatcher.matches decides per
    article (it is what alerts run during ingest). They must not disagree."""
    a, b, c = three_sources
    matcher = CriteriaMatcher({"source_ids": [a.id, b.id]})
    for art in db.query(Article).all():
        assert matcher.matches(art) is (art.source_id in {a.id, b.id})
    assert not any(matcher.matches(art) for art in
                   db.query(Article).filter(Article.source_id == c.id))


def test_feed_round_trips_source_ids_through_the_api(client, register, three_sources):
    hdr = register("alice")
    a, b, _ = three_sources
    r = client.post("/api/feeds", headers=hdr, json={
        "name": "Only A and B", "criteria": {"source_ids": [a.id, b.id]}})
    assert r.status_code == 201, r.text
    feed_id = r.json()["id"]

    # it survives being read back, which is what the wizard reopens from
    got = next(f for f in client.get("/api/feeds", headers=hdr).json() if f["id"] == feed_id)
    assert got["criteria"]["source_ids"] == [a.id, b.id]

    arts = client.get(f"/api/feeds/{feed_id}/articles", headers=hdr).json()
    assert arts
    assert {x["source"]["id"] for x in arts} == {a.id, b.id}
