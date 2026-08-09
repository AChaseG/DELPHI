"""Shortening the retention window must not be an outage.

Age-based pruning normally removes one day's tail per pass, so an unbounded
delete never hurt: there was never much to delete. Lowering NEWS_RETENTION_DAYS
changes that in one step — every article between the old cutoff and the new one
becomes eligible at once. On the live database that is several hundred thousand
rows, each cascading through the full-text index, and `_next_prune_at` starts at
zero so it runs on the first tick after the deploy that changed the setting.

That is the same hours-long stall the poller was restructured to avoid, caused
by the setting meant to shrink the database that causes it. These tests hold the
bound in place, and hold the other half of the bargain too: bounded work is only
acceptable if it keeps going until the job is done.
"""
from datetime import timedelta

import pytest

from backend.app import ingest
from backend.app.models import Article, Event, Source, utcnow


@pytest.fixture
def source(db):
    s = Source(name="probe", rss_url="http://probe.example/feed")
    db.add(s)
    db.commit()
    return s


def _articles(db, source, count, *, days_old):
    for i in range(count):
        db.add(Article(source_id=source.id, title=f"headline {days_old}-{i}",
                       url=f"http://probe.example/{days_old}/{i}",
                       summary="summary",
                       published_at=utcnow() - timedelta(days=days_old),
                       fetched_at=utcnow(), importance=10))
    db.commit()


# ---------- the bound ----------

def test_one_pass_removes_no_more_than_its_batch(db, source, monkeypatch):
    """The whole point: a big eligible backlog is taken in slices."""
    monkeypatch.setattr(ingest, "RETENTION_BATCH", 10)
    monkeypatch.setattr(ingest, "RETENTION_DAYS", 5)
    _articles(db, source, 25, days_old=40)

    result = ingest.prune_old_articles(db)

    assert result["articles"] == 10, "an unbounded pass would have taken all 25"
    assert db.query(Article).count() == 15


def test_a_full_batch_reports_there_is_more(db, source, monkeypatch):
    monkeypatch.setattr(ingest, "RETENTION_BATCH", 10)
    monkeypatch.setattr(ingest, "RETENTION_DAYS", 5)
    _articles(db, source, 25, days_old=40)

    assert ingest.prune_old_articles(db)["more"] is True


def test_a_partial_batch_reports_it_is_caught_up(db, source, monkeypatch):
    """Otherwise the poller would keep coming back every minute forever."""
    monkeypatch.setattr(ingest, "RETENTION_BATCH", 10)
    monkeypatch.setattr(ingest, "RETENTION_DAYS", 5)
    _articles(db, source, 4, days_old=40)

    result = ingest.prune_old_articles(db)
    assert result["articles"] == 4
    assert result["more"] is False


def test_nothing_eligible_is_not_reported_as_a_backlog(db, source, monkeypatch):
    monkeypatch.setattr(ingest, "RETENTION_BATCH", 10)
    monkeypatch.setattr(ingest, "RETENTION_DAYS", 5)
    _articles(db, source, 8, days_old=1)

    result = ingest.prune_old_articles(db)
    assert result["articles"] == 0
    assert result["more"] is False


def test_repeated_passes_do_finish_the_job(db, source, monkeypatch):
    """Bounded is only acceptable because it converges. Exercised, not argued."""
    monkeypatch.setattr(ingest, "RETENTION_BATCH", 10)
    monkeypatch.setattr(ingest, "RETENTION_DAYS", 5)
    _articles(db, source, 25, days_old=40)
    _articles(db, source, 5, days_old=1)

    passes = 0
    while ingest.prune_old_articles(db)["more"]:
        passes += 1
        assert passes < 20, "not converging"

    assert db.query(Article).count() == 5, "the recent ones survived"


def test_it_takes_the_oldest_first(db, source, monkeypatch):
    """A slice off an arbitrary part of the window would leave the archive
    ragged — old stories kept while newer ones went."""
    monkeypatch.setattr(ingest, "RETENTION_BATCH", 2)
    monkeypatch.setattr(ingest, "RETENTION_DAYS", 5)
    _articles(db, source, 1, days_old=40)
    _articles(db, source, 1, days_old=30)
    _articles(db, source, 1, days_old=20)

    ingest.prune_old_articles(db)

    ages = sorted(
        round((utcnow() - a.published_at).days) for a in db.query(Article).all())
    assert ages == [20], "the two oldest should have gone, not any other two"


def test_the_retention_boundary_is_still_respected(db, source, monkeypatch):
    """Bounding must not have widened what is eligible."""
    monkeypatch.setattr(ingest, "RETENTION_BATCH", 1000)
    monkeypatch.setattr(ingest, "RETENTION_DAYS", 10)
    _articles(db, source, 3, days_old=11)
    _articles(db, source, 3, days_old=9)

    ingest.prune_old_articles(db)
    assert db.query(Article).count() == 3


def test_disabled_retention_still_removes_nothing(db, source, monkeypatch):
    monkeypatch.setattr(ingest, "RETENTION_DAYS", 0)
    _articles(db, source, 5, days_old=400)

    result = ingest.prune_old_articles(db)
    assert result == {"articles": 0, "events": 0, "more": False}
    assert db.query(Article).count() == 5


# ---------- the event sweep ----------

def test_the_empty_event_sweep_is_bounded_too(db, source, monkeypatch):
    """It scans for events no article points at, which is a pass over a table
    with as many rows as there are stories."""
    monkeypatch.setattr(ingest, "EMPTY_EVENT_BATCH", 3)
    old = utcnow() - timedelta(days=40)
    for i in range(9):
        db.add(Event(title=f"event {i}", updated_at=old, first_seen=old))
    db.commit()

    dropped = ingest._drop_empty_events(db, utcnow() - timedelta(days=5))

    assert dropped == 3
    assert db.query(Event).count() == 6


def test_an_event_with_articles_is_never_dropped(db, source, monkeypatch):
    monkeypatch.setattr(ingest, "EMPTY_EVENT_BATCH", 100)
    old = utcnow() - timedelta(days=40)
    kept = Event(title="still referenced", updated_at=old, first_seen=old)
    db.add(kept)
    db.commit()
    db.add(Article(source_id=source.id, title="headline",
                   url="http://probe.example/kept", summary="s",
                   published_at=utcnow(), fetched_at=utcnow(),
                   importance=10, event_id=kept.id))
    db.commit()

    ingest._drop_empty_events(db, utcnow() - timedelta(days=5))
    assert db.query(Event).count() == 1


# ---------- the poller comes back for the rest ----------

def test_the_poller_shortens_its_interval_when_behind():
    """A batch every six hours would take days to apply a changed window —
    indistinguishable, from the outside, from the setting being ignored."""
    import inspect
    body = inspect.getsource(ingest.ingest_loop)
    assert 'pruned.get("more")' in body
    assert "PRUNE_CATCHUP_SECONDS" in body
    assert ingest.PRUNE_CATCHUP_SECONDS < ingest.PRUNE_EVERY_SECONDS


def test_the_catchup_interval_is_not_a_busy_loop():
    assert ingest.PRUNE_CATCHUP_SECONDS >= 15
