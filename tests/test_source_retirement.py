"""A source that stops answering stops being a source.

The catalog grows on its own — auto-discovery adds outlets, a watched place
adds a search — and nothing used to take one away, so a feed that died stayed
in the polling rotation for the life of the deployment. Five failed polls in a
row now ends it.

What "ends it" means depends on what would be lost. Deleting a source deletes
its articles with it, and five failures is about twenty-five minutes: an outlet
having a bad morning must not cost Delphi everything it ever carried from them.
So a source that has published is retired, and only one that never produced
anything is deleted.
"""
import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from backend.app import ingest, repair
from backend.app.models import Article, FavoriteLocation, Source, utcnow


def _source(db, name="Gone Daily", *, added_by="auto-discovered", failures=None,
            status="error: 404 Not Found", repaired=True):
    s = Source(name=name, rss_url=f"http://{name.replace(' ', '')}.test/rss",
               scope="national", tier=3, added_by=added_by,
               last_fetched_at=utcnow(),
               last_status=status,
               consecutive_failures=ingest.REMOVE_AFTER if failures is None else failures,
               # Repair is offered before removal; say it has already had its go
               # unless a test is specifically about that.
               last_repair_at=utcnow() - timedelta(days=1) if repaired else None)
    db.add(s)
    db.flush()
    return s


def _article(db, source, title="Something happened"):
    a = Article(source_id=source.id, title=title, url=f"http://x.test/{title}",
                summary="", content="", published_at=utcnow(), fetched_at=utcnow(),
                importance=40)
    db.add(a)
    db.flush()
    return a


def test_a_source_that_never_produced_is_deleted(db):
    dead = _source(db)
    dead_id = dead.id

    assert ingest.retire_or_remove(db, dead) == "removed"
    db.commit()

    assert db.get(Source, dead_id) is None, "the dead feed is still in the rotation"


def test_a_source_that_published_is_retired_not_deleted(db):
    outlet = _source(db, "Real Outlet")
    _article(db, outlet, "The bypass was approved")

    assert ingest.retire_or_remove(db, outlet) == "retired"
    db.commit()

    assert db.get(Source, outlet.id) is not None
    assert outlet.enabled is False, "a retired source must not be polled again"
    assert db.query(Article).count() == 1, "its reporting was deleted with it"


def test_a_retired_source_says_why(db):
    outlet = _source(db, "Real Outlet", status="error: 500 Internal Server Error")
    _article(db, outlet)

    ingest.retire_or_remove(db, outlet)

    assert "retired" in outlet.last_status
    assert "500" in outlet.last_status, "the reason it stopped is gone"


def test_four_failures_is_not_enough(db):
    s = _source(db, failures=ingest.REMOVE_AFTER - 1)
    assert ingest.retire_or_remove(db, s) is None
    assert db.get(Source, s.id) is not None


def test_a_source_somebody_added_by_hand_is_never_deleted(db):
    """Deleting the operator's own entry unasked is worse than a dead row they
    can see and remove."""
    mine = _source(db, "My Feed", added_by="user")

    assert ingest.retire_or_remove(db, mine) == "retired"
    assert db.get(Source, mine.id) is not None
    assert mine.enabled is False


def test_repair_gets_its_turn_first(db, monkeypatch):
    """Only five sources are offered a repair per cycle, so one can hit the
    failure limit without ever having been tried. Removing it then would throw
    away a feed whose URL was about to be rediscovered."""
    monkeypatch.setattr(repair, "AUTO_REPAIR", True)
    untried = _source(db, "Moved Feed", status="error: 404 Not Found", repaired=False)

    assert ingest.retire_or_remove(db, untried) is None, \
        "removed before automatic repair had attempted it"

    untried.last_repair_at = utcnow()          # repair tried and failed
    assert ingest.retire_or_remove(db, untried) == "removed"


def test_a_timeout_does_not_wait_for_repair(db):
    """Repair only applies to permanent-looking errors. A source failing for
    any other reason must not become unremovable by never qualifying."""
    s = _source(db, "Slow Feed", status="error: ReadTimeout: timed out")
    assert ingest.retire_or_remove(db, s) == "removed"


def test_removing_a_place_search_leaves_the_place(db):
    """A watched place points at its search feed by id. The row going away must
    not leave the location pointing at something that no longer exists."""
    feed = _source(db, "Local Reading")
    loc = FavoriteLocation(user_id="acct:1", name="Home", lat=51.45, lon=-0.97,
                           radius_km=25, place_name="Reading", country="GB",
                           source_id=feed.id)
    db.add(loc)
    db.commit()

    ingest.retire_or_remove(db, feed)
    db.commit()

    assert loc.source_id is None, "the location still points at a deleted source"


def test_a_working_source_is_left_alone(db):
    fine = _source(db, "Working", failures=0, status="ok")
    assert ingest.retire_or_remove(db, fine) is None
    assert fine.enabled is True


@pytest.mark.parametrize("kind", ["auto-discovered", "city-catalog", "location",
                                  "topic-tracker", "catalog"])
def test_every_way_a_source_arrives_can_also_leave(db, kind):
    """The rule is about what a source produced, not who added it — except for
    the operator's own entries, which have their own test."""
    s = _source(db, f"Feed {kind}", added_by=kind)
    assert ingest.retire_or_remove(db, s) == "removed"


def test_the_poller_removes_a_source_that_keeps_failing(db, monkeypatch):
    """End to end through a real batch: five failed polls and it is gone.

    The unit tests above check the decision; this checks the wiring — that the
    poller counts failures, reaches the limit, and acts on it, rather than the
    rule sitting in a function nothing calls.
    """
    monkeypatch.setattr(ingest, "CONTENT_FETCH", False)
    monkeypatch.setattr(repair, "AUTO_REPAIR", False)
    dead = Source(name="Vanished", rss_url="http://vanished.test/rss",
                  scope="national", tier=3, added_by="auto-discovered")
    db.add(dead)
    db.commit()

    async def always_404(sources):
        return [(s, [], "error: 404 Not Found") for s in sources]

    monkeypatch.setattr(ingest, "_fetch_batch", always_404)

    async def poll_once():
        return await ingest._ingest_batch(db, [dead])

    for attempt in range(1, ingest.REMOVE_AFTER):
        asyncio.run(poll_once())
        assert db.get(Source, dead.id) is not None, \
            f"removed after only {attempt} failure(s)"

    stats = asyncio.run(poll_once())

    assert stats["removed"] == 1
    assert db.scalar(select(func.count(Source.id))) == 0, "the dead feed survived"


def test_a_source_that_comes_back_keeps_its_place(db, monkeypatch):
    """Four failures then an answer must not leave it one poll from removal."""
    monkeypatch.setattr(ingest, "CONTENT_FETCH", False)
    monkeypatch.setattr(repair, "AUTO_REPAIR", False)
    flaky = Source(name="Flaky", rss_url="http://flaky.test/rss",
                   scope="national", tier=3, added_by="auto-discovered")
    db.add(flaky)
    db.commit()

    outcome = {"status": "error: 500 Internal Server Error"}

    async def as_told(sources):
        return [(s, [], outcome["status"]) for s in sources]

    monkeypatch.setattr(ingest, "_fetch_batch", as_told)
    for _ in range(ingest.REMOVE_AFTER - 1):
        asyncio.run(ingest._ingest_batch(db, [flaky]))
    assert flaky.consecutive_failures == ingest.REMOVE_AFTER - 1

    outcome["status"] = ingest.UNCHANGED       # the publisher answers again
    asyncio.run(ingest._ingest_batch(db, [flaky]))

    assert flaky.consecutive_failures == 0, "a recovery did not clear the streak"
    outcome["status"] = "error: 500 Internal Server Error"
    asyncio.run(ingest._ingest_batch(db, [flaky]))
    assert db.get(Source, flaky.id) is not None, "one failure after recovery removed it"
