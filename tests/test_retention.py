"""What retention removes, and what it used to leave behind.

Age-based retention was already here and already running. It still let the
volume fill, for two reasons this file covers:

*Rows that outlive what they point at.* Every table keyed on article_id has to
be cleared with the article. Missing one fails silently — nothing errors, the
rows simply accumulate for as long as the system runs. ViewedArticle, added
with article dimming, was exactly that.

*Age cannot bound size.* How much thirty days weighs depends entirely on how
much news happened; a busy month and a quiet one differ by more than a small
volume has spare. So there is a second rule, oldest-first, driven by the disk.
"""
import itertools
from datetime import timedelta

import pytest

from backend.app import ingest, storage
from backend.app.models import (AlertEvent, Article, Event, Source, Translation,
                                ViewedArticle, ViewedEvent, utcnow)

SEQ = itertools.count(1)


def _article(db, source, *, days_old=0, event=None):
    a = Article(source_id=source.id, title=f"headline {next(SEQ)}",
                url=f"http://probe.example/{next(SEQ)}",
                summary="summary", content="body text " * 50,
                published_at=utcnow() - timedelta(days=days_old),
                fetched_at=utcnow(), importance=10,
                event_id=event.id if event else None)
    db.add(a)
    db.commit()
    return a


@pytest.fixture
def source(db):
    s = Source(name="probe", rss_url="http://probe.example/feed")
    db.add(s)
    db.commit()
    return s


# ---------- nothing may outlive the article it points at ----------

def test_viewed_markers_go_with_the_article(db, source):
    """The leak. Written on every read, never cleaned up before this.

    One row per person per article read, kept forever — small individually,
    unbounded in aggregate, and invisible until someone goes looking.
    """
    old = _article(db, source, days_old=99)
    db.add(ViewedArticle(user_id="acct:1", article_id=old.id))
    db.commit()

    ingest.prune_old_articles(db)

    assert db.query(Article).filter_by(id=old.id).count() == 0
    assert db.query(ViewedArticle).filter_by(article_id=old.id).count() == 0, (
        "viewed markers survived the article they refer to")


def test_translations_and_alert_history_go_too(db, source):
    old = _article(db, source, days_old=99)
    db.add(Translation(article_id=old.id, lang="fr", title="titre", summary="résumé"))
    db.add(AlertEvent(alert_id=1, article_id=old.id))
    db.commit()

    ingest.prune_old_articles(db)

    assert db.query(Translation).filter_by(article_id=old.id).count() == 0
    assert db.query(AlertEvent).filter_by(article_id=old.id).count() == 0


def test_nothing_is_left_pointing_at_a_missing_article(db, source):
    """A guard against the next table someone adds and forgets.

    This is the check that would have caught ViewedArticle when it was
    introduced, rather than a volume filling up months later.
    """
    old = _article(db, source, days_old=99)
    db.add_all([
        ViewedArticle(user_id="acct:1", article_id=old.id),
        Translation(article_id=old.id, lang="de", title="t", summary="s"),
        AlertEvent(alert_id=1, article_id=old.id),
    ])
    db.commit()

    ingest.prune_old_articles(db)

    live = {a.id for a in db.query(Article).all()}
    for model in (ViewedArticle, Translation, AlertEvent):
        orphans = [r for r in db.query(model).all() if r.article_id not in live]
        assert not orphans, f"{model.__name__} left {len(orphans)} orphaned row(s)"


def test_recent_articles_are_kept(db, source):
    fresh = _article(db, source, days_old=1)
    db.add(ViewedArticle(user_id="acct:1", article_id=fresh.id))
    db.commit()

    ingest.prune_old_articles(db)

    assert db.query(Article).filter_by(id=fresh.id).count() == 1
    assert db.query(ViewedArticle).filter_by(article_id=fresh.id).count() == 1


def test_emptied_events_and_their_markers_go(db, source):
    event = Event(title="something happened", updated_at=utcnow() - timedelta(days=99))
    db.add(event)
    db.commit()
    _article(db, source, days_old=99, event=event)
    db.add(ViewedEvent(user_id="acct:1", event_id=event.id))
    db.commit()

    result = ingest.prune_old_articles(db)

    assert result["events"] == 1
    assert db.query(Event).filter_by(id=event.id).count() == 0
    assert db.query(ViewedEvent).filter_by(event_id=event.id).count() == 0


# ---------- size is the backstop age cannot be ----------

def test_nothing_happens_while_within_the_ceiling(db, source, monkeypatch):
    monkeypatch.setattr(storage, "over_ceiling", lambda: 0)
    for _ in range(5):
        _article(db, source, days_old=10)

    assert ingest.prune_to_fit(db)["articles"] == 0
    assert db.query(Article).count() == 5


def test_the_oldest_go_first_when_over(db, source, monkeypatch):
    monkeypatch.setattr(storage, "over_ceiling", lambda: 50 * 1024 * 1024)
    monkeypatch.setattr(ingest, "OVERSIZE_BATCH", 3)
    for age in (30, 25, 20, 10, 5):
        _article(db, source, days_old=age)

    result = ingest.prune_to_fit(db)

    assert result["articles"] == 3
    remaining = sorted(a.published_at for a in db.query(Article).all())
    assert len(remaining) == 2
    ages = [(utcnow() - p).days for p in remaining]
    assert max(ages) <= 10, "the newest should be what survives"


def test_recent_news_is_never_dropped_for_space(db, source, monkeypatch):
    """The safety floor, and it is not decorative.

    Before conversion to incremental vacuum, deleting does not shrink the file
    at all — so a size rule with no floor would delete everything in pursuit of
    a number it cannot move, and leave a working system with no news in it.
    """
    monkeypatch.setattr(storage, "over_ceiling", lambda: 500 * 1024 * 1024)
    monkeypatch.setattr(ingest, "MIN_KEEP_DAYS", 2)
    for _ in range(5):
        _article(db, source, days_old=0)

    result = ingest.prune_to_fit(db)

    assert result["articles"] == 0
    assert db.query(Article).count() == 5


def test_being_stuck_over_the_ceiling_is_reported(db, source, monkeypatch, caplog):
    """Nothing left to delete and still too big means the disk is too small.

    That is an operator's problem to solve, so it has to reach an operator
    rather than being retried silently every tick.
    """
    monkeypatch.setattr(storage, "over_ceiling", lambda: 500 * 1024 * 1024)
    _article(db, source, days_old=0)

    with caplog.at_level("WARNING"):
        ingest.prune_to_fit(db)

    assert any("too small" in r.message or "ceiling" in r.message
               for r in caplog.records)


def test_size_pruning_clears_dependants_too(db, source, monkeypatch):
    """It deletes through the same path, so it must not skip the extra tables."""
    monkeypatch.setattr(storage, "over_ceiling", lambda: 50 * 1024 * 1024)
    old = _article(db, source, days_old=30)
    db.add(ViewedArticle(user_id="acct:1", article_id=old.id))
    db.commit()

    ingest.prune_to_fit(db)

    assert db.query(Article).filter_by(id=old.id).count() == 0
    assert db.query(ViewedArticle).filter_by(article_id=old.id).count() == 0


# ---------- what happens when the disk is nearly gone ----------

@pytest.mark.anyio
async def test_ingestion_pauses_instead_of_filling_the_last_of_the_disk(
        db, source, monkeypatch, anyio_backend):
    """The behaviour that turns an outage into a degraded system.

    The app died because the disk was full and it kept trying. Pausing keeps it
    running, keeps it serving what it already has, and leaves room for the
    database to be opened at all on the next restart — while pruning works the
    problem down.
    """
    import asyncio

    fetched = []
    monkeypatch.setattr(ingest, "_ingest_batch",
                        lambda *a, **k: fetched.append(1) or {"new_articles": 0})
    monkeypatch.setattr(ingest, "POLL_TICK", 0.05)
    monkeypatch.setattr(storage, "disk", lambda: {
        "ok": True, "low": True, "free_bytes": 5 * 1024 * 1024,
        "total_bytes": 1024 ** 3, "free_pct": 0.5})
    monkeypatch.setattr(storage, "ensure_incremental_vacuum",
                        lambda: {"converted": False, "reason": "test"})
    monkeypatch.setattr(storage, "checkpoint", lambda: None)
    monkeypatch.setattr(storage, "reclaim", lambda *a, **k: 0)
    monkeypatch.setattr(storage, "over_ceiling", lambda: 0)
    ingest.status["last_error"] = ""

    task = asyncio.create_task(ingest.ingest_loop())
    await asyncio.sleep(0.4)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert not fetched, "kept fetching with the disk nearly full"
    assert "paused" in ingest.status["last_error"].lower()
    assert "MB free" in ingest.status["last_error"]


@pytest.fixture
def anyio_backend():
    return "asyncio"
