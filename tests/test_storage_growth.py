"""How fast the archive is filling, and trimming it to match.

Retention could always answer "delete anything older than thirty days". It
could never answer the question an operator actually has — **how fast is this
filling up and how long have I got** — and without that, the only signal the
disk was in trouble was the disk being in trouble. That is how Delphi filled
its volume once: the app simply stopped starting, because SQLite could not
enable WAL, and there was nothing left running to say why.

Two things here, and they are the same thing at different ends.

**Measuring.** A row an hour recording the archive's size and article count.
Two of them give a rate; a fortnight of them gives a rate that is not just
yesterday's news cycle. Grounded in a real number: a synthetic corpus puts one
article at about 2.8 KB once its row, its full-text index and the indexes over
it are counted — but how many arrive a day is local to the instance, so the
rate is measured rather than assumed.

**Trimming.** `prune_old_articles` answers "how old is too old" and cannot
bound a database, because how much thirty days weighs depends on how much news
happened. `prune_to_fit` answers "are we over the wall" and only acts once the
archive has filled seventy per cent of the volume — and on a database that
predates incremental auto-vacuum it never comes back down, because deleting
rows does not shrink a SQLite file. `trim_to_budget` holds a *level*: a small
batch every housekeeping tick, so the archive settles at its target and then
sheds each day's intake as the next day's arrives.
"""
from datetime import timedelta

import pytest
from sqlalchemy import select

from backend.app import ingest, storage
from backend.app.models import Article, StorageSample, utcnow


def _sample(db, hours_ago, db_bytes, articles, free=10**9):
    row = StorageSample(at=utcnow() - timedelta(hours=hours_ago),
                        db_bytes=db_bytes, free_bytes=free, articles=articles)
    db.add(row)
    db.commit()
    return row


def _article(db, days_ago, url):
    a = Article(title="t", url=url, source_id=1,
                published_at=utcnow() - timedelta(days=days_ago),
                fetched_at=utcnow() - timedelta(days=days_ago))
    db.add(a)
    db.commit()
    return a


# ---------- measuring ----------

def test_a_rate_needs_more_than_one_measurement(db):
    assert storage.growth(db)["ready"] is False
    _sample(db, 0, 1000, 10)
    assert storage.growth(db)["ready"] is False


def test_two_samples_an_hour_apart_are_not_enough(db):
    """A checkpoint landing between two close samples looks like a doubling,
    and a wrong rate would drive the trimming."""
    _sample(db, 1, 1_000_000, 100)
    _sample(db, 0, 2_000_000, 200)
    g = storage.growth(db)
    assert g["ready"] is False
    assert "history" in g["reason"]


def test_enough_history_gives_a_rate_per_day(db):
    _sample(db, 24, 100_000_000, 10_000)
    _sample(db, 0, 128_000_000, 20_000)
    g = storage.growth(db)
    assert g["ready"] is True
    assert g["bytes_per_day"] == pytest.approx(28_000_000, rel=0.02)
    assert g["articles_per_day"] == pytest.approx(10_000, rel=0.02)


def test_it_reports_what_an_article_costs(db):
    """The number that makes the other two concrete — and measured here rather
    than taken from the synthetic corpus, because a real archive's mix of wire
    copy and full article text is its own."""
    _sample(db, 24, 0, 0)
    _sample(db, 0, 28_000_000, 10_000)
    assert storage.growth(db)["bytes_per_article"] == pytest.approx(2800, rel=0.01)


def test_a_shrinking_archive_is_given_no_deadline(db):
    """Dividing by a negative rate would invent one, and an archive that
    trimming is keeping up with has no deadline to report."""
    _sample(db, 24, 200_000_000, 20_000)
    _sample(db, 0, 150_000_000, 15_000)
    g = storage.growth(db)
    assert g["bytes_per_day"] < 0
    assert g["days_to_ceiling"] is None


def test_a_flat_archive_does_not_divide_by_zero(db):
    _sample(db, 24, 100_000_000, 10_000)
    _sample(db, 0, 100_000_000, 10_000)
    g = storage.growth(db)
    assert g["ready"] is True
    assert g["days_to_ceiling"] is None
    assert g["bytes_per_article"] is None


def test_sampling_records_and_forgets(db, monkeypatch):
    """A fortnight is long enough that a quiet weekend does not read as a
    collapse in the rate, and short enough to notice a catalog that doubled on
    Tuesday. Beyond that it is dead weight."""
    _sample(db, 24 * 40, 1, 1)          # older than the window
    _sample(db, 1, 2, 2)
    storage.sample(db)
    kept = db.scalars(select(StorageSample)).all()
    assert len(kept) == 2, "the stale one goes, the recent one and the new one stay"


def test_measuring_never_stops_the_poll():
    """It is a diagnostic. A diagnostic that can take the system down is worse
    than no diagnostic."""
    import inspect
    src = inspect.getsource(ingest.ingest_loop)
    # The sampling call, not the `global` line that happens to name it first.
    block = src[src.index("storage.SAMPLE_EVERY_SECONDS"):]
    assert "except Exception" in block[:600]
    assert "could not record a storage sample" in block[:600]


# ---------- trimming to a level ----------

def test_the_target_sits_below_the_ceiling(db):
    """The ceiling is a wall you hit; the target is a level you sit at. Equal
    would mean the archive only ever settles at the point where the poller
    starts pausing."""
    assert ingest.DB_TARGET_FRACTION < storage.DB_MAX_FRACTION


def test_nothing_is_deleted_while_under_target(db, monkeypatch):
    for i in range(5):
        _article(db, 40, f"https://x.test/{i}")
    monkeypatch.setattr(storage, "db_bytes", lambda: 1)
    r = ingest.trim_to_budget(db)
    assert r["articles"] == 0
    assert db.query(Article).count() == 5


def test_the_oldest_go_first(db, monkeypatch):
    for i in range(6):
        _article(db, 40 - i, f"https://x.test/{i}")
    monkeypatch.setattr(storage, "db_bytes", lambda: 10 ** 12)
    monkeypatch.setattr(ingest, "TRIM_BATCH", 2)
    ingest.trim_to_budget(db)
    left = sorted(a.url for a in db.scalars(select(Article)))
    assert "https://x.test/0" not in left, "the oldest should have gone"
    assert "https://x.test/5" in left, "the newest should not have"


def test_a_pass_is_bounded(db, monkeypatch):
    """It runs every housekeeping tick. The point is to shed roughly what a day
    adds over the course of a day, not to take a bite out of the archive on the
    hour."""
    for i in range(20):
        _article(db, 40, f"https://x.test/{i}")
    monkeypatch.setattr(storage, "db_bytes", lambda: 10 ** 12)
    monkeypatch.setattr(ingest, "TRIM_BATCH", 5)
    r = ingest.trim_to_budget(db)
    assert r["articles"] == 5
    assert r["more"] is True, "so the caller comes back next tick, not in six hours"


def test_recent_news_is_never_trimmed(db, monkeypatch):
    """However full the disk is. Deleting today's news to make room for today's
    news is not a strategy."""
    for i in range(5):
        _article(db, 0, f"https://x.test/{i}")
    monkeypatch.setattr(storage, "db_bytes", lambda: 10 ** 12)
    r = ingest.trim_to_budget(db)
    assert r["articles"] == 0
    assert db.query(Article).count() == 5


def test_a_volume_too_small_says_so_rather_than_failing_quietly(db, monkeypatch,
                                                               caplog):
    """Nothing old enough to delete and still over target means the volume is
    too small for this catalog. That is an operator's decision, not ours, and
    it has to reach them."""
    _article(db, 0, "https://x.test/today")
    monkeypatch.setattr(storage, "db_bytes", lambda: 10 ** 12)
    import logging
    with caplog.at_level(logging.WARNING):
        r = ingest.trim_to_budget(db)
    assert r["articles"] == 0
    assert any("too small" in m for m in caplog.messages)


def test_trimming_hands_the_space_back(db, monkeypatch):
    """Deleting rows does not shrink a SQLite file on its own — that is the
    whole reason this module exists."""
    import inspect
    src = inspect.getsource(ingest.trim_to_budget)
    assert "storage.checkpoint()" in src
    assert "storage.reclaim()" in src


def test_it_runs_on_the_housekeeping_tick():
    import inspect
    src = inspect.getsource(ingest.ingest_loop)
    assert "trim_to_budget(db)" in src
    assert src.index("prune_old_articles(db)") < src.index("trim_to_budget(db)")


# ---------- and the operator can see all of it ----------

def test_the_disk_block_cannot_take_the_status_endpoint_down(db, monkeypatch):
    """`auto_vacuum_mode` opens a database connection, and a volume with no
    room left is exactly when opening one fails. That raised straight through
    the status endpoint — so the operator trying to find out why the disk was
    full got nothing at all, the failure hiding its own diagnosis."""
    from backend.app import main

    def boom():
        raise OSError("disk I/O error")

    monkeypatch.setattr(storage, "auto_vacuum_mode", boom)
    out = main._storage_status(db)
    assert out["ok"] is True
    assert out["reclaimable"] is None, "unknown, not False"
    assert "disk I/O error" in out["reclaimable_detail"]


def test_unknown_is_never_reported_as_cannot(db, monkeypatch):
    """Telling an operator their database cannot return space, when the truth
    is we could not ask, sends them to run a VACUUM they may not need on a disk
    that cannot afford one."""
    src = (__import__("pathlib").Path(__file__).resolve().parent.parent
           / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    assert "st.reclaimable === null" in src
    assert src.index("st.reclaimable === null") < src.index("} else if (!st.reclaimable)")


def test_a_missing_disk_answer_is_stated_rather_than_skipped():
    """The gap it used to fall through: with neither `ok` nor `detail` set,
    both branches were skipped and the disk had no line at all — on the one
    panel an operator opens *because* the disk is in trouble."""
    src = (__import__("pathlib").Path(__file__).resolve().parent.parent
           / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    assert "if (!st.ok && !st.detail)" in src
    assert "could not be measured" in src


def test_the_growth_line_reaches_the_operator(db):
    from backend.app import main
    out = main._storage_status(db)
    assert "growth" in out
    assert "target_fraction" in out
    src = (__import__("pathlib").Path(__file__).resolve().parent.parent
           / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    assert "📈 Growth:" in src
    assert "days_to_ceiling" in src
