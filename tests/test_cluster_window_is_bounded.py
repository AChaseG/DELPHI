"""The corroboration index has to be bounded, and reused.

`RecentClusters` answers "how many other outlets ran this story", and its own
docstring records the measurement it was designed against: a window of 2,016
headlines. Nothing bounded that window. It was every article published in the
last 48 hours, and how many that is depends entirely on how many sources are
enabled — the same mistake the article ceiling exists to correct.

At 9,400 sources it became about 350,000 headlines. Building the index over that
takes 6.6 seconds on a fast laptop, and it was built from scratch at the top of
every batch, on the event loop, where nothing else could be answered while it
ran. The stall recorder reported it faithfully for hours: twenty to sixty
seconds, every one to three minutes, always during "fetch".

Three things fix it and this file holds all three: a cap that does not depend on
how much news happened, reuse between batches (the batch already adds its own
new articles, so a rebuild only drops what aged out), and doing the work off the
loop so a slow tick is not an outage.
"""
from datetime import timedelta

import pytest

from backend.app import ingest
from backend.app.ingest import RecentClusters
from backend.app.models import Article, Source, utcnow


@pytest.fixture(autouse=True)
def _fresh_index():
    ingest.reset_cluster_index()
    yield
    ingest.reset_cluster_index()


@pytest.fixture
def source(db):
    s = Source(name="probe", rss_url="http://probe.example/feed")
    db.add(s)
    db.commit()
    return s


def _headlines(db, source, count, *, hours_old=1, token="alpha"):
    for i in range(count):
        db.add(Article(
            source_id=source.id, title=f"headline {i}",
            url=f"http://probe.example/{hours_old}/{i}", summary="s",
            cluster_tokens=f"{token} story{i}",
            published_at=utcnow() - timedelta(hours=hours_old),
            fetched_at=utcnow(), importance=10))
    db.commit()


# ---------- the bound ----------

def test_the_window_is_capped_however_much_news_happened(db, source, monkeypatch):
    """The important half: hours alone is not a bound."""
    monkeypatch.setattr(ingest, "CLUSTER_WINDOW_MAX", 25)
    _headlines(db, source, 80, hours_old=1)

    assert len(ingest._recent_clusters(db)) == 25


def test_the_cap_keeps_the_newest(db, source, monkeypatch):
    """A cap that dropped the recent end would answer the corroboration
    question with the least relevant headlines available."""
    monkeypatch.setattr(ingest, "CLUSTER_WINDOW_MAX", 5)
    monkeypatch.setattr(ingest, "CLUSTER_WINDOW_HOURS", 48)
    _headlines(db, source, 20, hours_old=40, token="old")
    _headlines(db, source, 5, hours_old=1, token="new")

    index = ingest._recent_clusters(db)
    assert len(index) == 5
    # Every retained entry should be from the recent group.
    kept = {w for _, words in index._entries for w in words}
    assert "new" in kept
    assert "old" not in kept


def test_the_window_respects_its_hours(db, source, monkeypatch):
    monkeypatch.setattr(ingest, "CLUSTER_WINDOW_HOURS", 12)
    monkeypatch.setattr(ingest, "CLUSTER_WINDOW_MAX", 1000)
    _headlines(db, source, 4, hours_old=2)
    _headlines(db, source, 6, hours_old=30)

    assert len(ingest._recent_clusters(db)) == 4


def test_articles_with_no_tokens_are_not_indexed(db, source, monkeypatch):
    monkeypatch.setattr(ingest, "CLUSTER_WINDOW_MAX", 1000)
    _headlines(db, source, 3, hours_old=1)
    db.add(Article(source_id=source.id, title="no tokens",
                   url="http://probe.example/none", summary="s",
                   cluster_tokens="", published_at=utcnow(),
                   fetched_at=utcnow(), importance=10))
    db.commit()

    assert len(ingest._recent_clusters(db)) == 3


def test_the_default_window_is_smaller_than_the_disk_allows():
    """Whatever the numbers become, they have to be numbers — an unbounded
    window is how this went wrong."""
    assert ingest.CLUSTER_WINDOW_MAX > 0
    assert ingest.CLUSTER_WINDOW_HOURS > 0
    # Comfortably above the 2,016 it was measured on, comfortably below the
    # 350,000 that broke it.
    assert 5_000 <= ingest.CLUSTER_WINDOW_MAX <= 100_000


# ---------- it is reused, not rebuilt every batch ----------

def test_the_index_is_reused_between_batches(db, source):
    _headlines(db, source, 3, hours_old=1)

    first = ingest.cluster_index(db)
    second = ingest.cluster_index(db)

    assert first is second, "rebuilding per batch is the whole cost"


def test_a_stale_index_is_rebuilt(db, source, monkeypatch):
    _headlines(db, source, 3, hours_old=1)
    first = ingest.cluster_index(db)

    monkeypatch.setattr(ingest, "CLUSTER_REBUILD_EVERY_S", 0.0)
    second = ingest.cluster_index(db)

    assert second is not first


def test_a_rebuild_drops_what_aged_out(db, source, monkeypatch):
    """The only thing reuse costs, so the rebuild has to actually collect it."""
    monkeypatch.setattr(ingest, "CLUSTER_WINDOW_HOURS", 12)
    monkeypatch.setattr(ingest, "CLUSTER_WINDOW_MAX", 1000)
    _headlines(db, source, 4, hours_old=2)
    assert len(ingest.cluster_index(db)) == 4

    # Those four slide out of the window; two new ones arrive.
    for a in db.query(Article).all():
        a.published_at = utcnow() - timedelta(hours=30)
    db.commit()
    _headlines(db, source, 2, hours_old=1, token="fresh")

    monkeypatch.setattr(ingest, "CLUSTER_REBUILD_EVERY_S", 0.0)
    assert len(ingest.cluster_index(db)) == 2


def test_what_the_batch_adds_is_visible_without_a_rebuild(db, source):
    """Why reuse is safe: the batch keeps the index current itself."""
    _headlines(db, source, 2, hours_old=1)
    index = ingest.cluster_index(db)
    before = len(index)

    index.add(source.id, "breaking something happened")

    assert len(ingest.cluster_index(db)) == before + 1


def test_resetting_forgets_it(db, source):
    _headlines(db, source, 2, hours_old=1)
    first = ingest.cluster_index(db)
    ingest.reset_cluster_index()
    assert ingest.cluster_index(db) is not first


# ---------- it is off the loop ----------

def test_the_index_is_built_in_a_thread():
    """A rebuild reads tens of thousands of rows and builds an index from them.
    On the loop, that is the health check Fly abandons after ten seconds."""
    import inspect
    body = inspect.getsource(ingest._ingest_batch)
    assert "asyncio.to_thread(cluster_index" in body
    assert "recent = _recent_clusters(db)" not in body, (
        "the synchronous call is what caused the stalls")


# ---------- the thing it measures still works ----------

def test_corroboration_still_counts_other_outlets(db):
    """None of the above may change the answer for a window that fits."""
    index = RecentClusters([
        (1, "flood warning river"),
        (2, "flood warning river"),
        (3, "unrelated market news"),
    ])
    assert index.corroboration(4, "flood warning river") == 2
    assert index.corroboration(1, "flood warning river") == 1
    assert index.corroboration(9, "nothing in common here") == 0


def test_len_reports_the_size_that_matters():
    assert len(RecentClusters([(1, "a b"), (2, "c d")])) == 2
    assert len(RecentClusters()) == 0
