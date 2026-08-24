"""Only stories somebody else is also running.

Dataminr detects events by corroborating across sources and applies the
reader's boolean afterwards, as a filter on things already judged real. Delphi's
boolean is the detector, so one outlet's passing mention has always been enough
to fill a feed.

Delphi already clusters coverage and already knows which outlets are in a
cluster. That number simply played no part in matching. This is it playing one.
"""
import pytest

from backend.app.clustering import MAX_EVENT_SOURCES, _merge
from backend.app.matching import CriteriaMatcher
from backend.app.models import Article, Event, Source, utcnow


@pytest.fixture
def wires(db):
    made = []
    for name in ("Alpha Wire", "Beta Times", "Gamma Post"):
        s = Source(name=name, rss_url=f"http://{name.split()[0].lower()}.test/feed",
                   scope="international")
        db.add(s)
        made.append(s)
    db.commit()
    return made


def _story(db, wires, *, outlets=1, filings_each=1):
    """One event carried by `outlets` outlets, each filing `filings_each` times."""
    event = Event(title="A quake struck", first_seen=utcnow(), updated_at=utcnow(),
                  article_count=0, importance=50, source_ids=[],
                  cluster_tokens="quake coast", countries=[], categories=[])
    db.add(event)
    db.flush()
    first = None
    for i in range(outlets):
        for _ in range(filings_each):
            a = Article(source_id=wires[i].id, event_id=event.id,
                        title="A quake struck the coast",
                        summary="Reports of damage.", content="", importance=50,
                        cluster_tokens="quake coast", categories=[],
                        url=f"http://x.test/{event.id}-{i}-{db.query(Article).count()}",
                        published_at=utcnow(), fetched_at=utcnow())
            db.add(a)
            db.flush()
            _merge(event, a)
            first = first or a
    db.commit()
    db.refresh(first)
    return first


def test_by_default_a_single_source_story_still_arrives(db, wires):
    art = _story(db, wires, outlets=1)
    assert CriteriaMatcher({"queries": ["quake"]}).matches(art) is True


def test_asking_for_two_outlets_drops_a_single_source_story(db, wires):
    art = _story(db, wires, outlets=1)
    m = CriteriaMatcher({"queries": ["quake"], "min_sources": 2})
    assert m.matches(art) is False


def test_asking_for_two_outlets_keeps_a_corroborated_story(db, wires):
    art = _story(db, wires, outlets=2)
    m = CriteriaMatcher({"queries": ["quake"], "min_sources": 2})
    assert m.matches(art) is True


def test_one_outlet_filing_three_times_is_still_one_outlet(db, wires):
    """The reason this counts sources and not articles. A newsroom running
    updates all afternoon has not been corroborated by anybody."""
    art = _story(db, wires, outlets=1, filings_each=3)
    assert art.event.article_count == 3
    assert len(art.event.source_ids) == 1
    m = CriteriaMatcher({"queries": ["quake"], "min_sources": 2})
    assert m.matches(art) is False


def test_an_article_in_no_cluster_has_one_source(db, wires):
    """The honest answer rather than a generous one: nothing has corroborated
    it yet."""
    a = Article(source_id=wires[0].id, title="A quake struck", summary="",
                content="", importance=50, url="http://x.test/lonely",
                published_at=utcnow(), fetched_at=utcnow())
    db.add(a)
    db.commit()
    assert CriteriaMatcher({"queries": ["quake"]}).matches(a) is True
    assert CriteriaMatcher({"queries": ["quake"], "min_sources": 2}).matches(a) is False


def test_a_cluster_from_before_the_column_existed_is_not_silently_emptied(db, wires):
    """An old row has no list. Reading article_count as an upper bound admits
    a few stories a stricter answer would drop — the right way round, because
    a filter that emptied every feed on old data would be far worse."""
    art = _story(db, wires, outlets=3)
    art.event.source_ids = None
    art.event.article_count = 3
    db.commit()
    m = CriteriaMatcher({"queries": ["quake"], "min_sources": 3})
    assert m.matches(art) is True


def test_the_source_list_is_capped(db, wires):
    """A wire story can be carried by hundreds, and the list is read on every
    match. The cap only bites well above any threshold a reader would set."""
    event = Event(title="Big", first_seen=utcnow(), updated_at=utcnow(),
                  article_count=0, importance=1, cluster_tokens="",
                  source_ids=list(range(MAX_EVENT_SOURCES)))
    a = Article(source_id=99999, title="x", summary="", content="",
                url="http://x.test/capped", published_at=utcnow(),
                fetched_at=utcnow(), importance=1, cluster_tokens="",
                categories=[])
    _merge(event, a)
    assert len(event.source_ids) == MAX_EVENT_SOURCES


def test_it_is_off_unless_asked_for():
    assert CriteriaMatcher({}).min_sources == 0
    assert CriteriaMatcher({"min_sources": 1}).min_sources == 1
    assert CriteriaMatcher({"min_sources": -3}).min_sources == 0


def test_one_means_the_same_as_off(db, wires):
    art = _story(db, wires, outlets=1)
    assert CriteriaMatcher({"queries": ["quake"], "min_sources": 1}).matches(art) is True


def test_a_feed_remembers_it(client, register, db):
    from backend.app.models import Feed
    headers = register("reader")
    feed_id = client.post("/api/feeds", headers=headers, json={
        "name": "Corroborated", "criteria": {"queries": ["quake"], "min_sources": 3},
    }).json()["id"]
    assert db.get(Feed, feed_id).criteria["min_sources"] == 3
