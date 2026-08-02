"""Giving the disk back, and noticing before it runs out.

Delphi filled a 1 GB volume and then would not start: SQLite could not enable
WAL, so the process never opened a port and every request became a 503.
Retention had been deleting old articles the entire time, which changed nothing,
because deleting rows does not shrink a SQLite file — freed pages stay inside it.

Everything here is about the gap between "we deleted it" and "the disk got it
back". Both halves of that have a failure mode that raises nothing and reports
success, so both are measured rather than asserted:

  - `PRAGMA incremental_vacuum` through the ordinary execute() path frees
    exactly one page and returns cleanly.
  - Pruning that leaves rows pointing at deleted articles grows forever and
    looks fine.
"""
import itertools

import pytest
from sqlalchemy import text

from backend.app import storage
from backend.app.database import engine
from backend.app.models import Article, Source, ViewedArticle, utcnow

SEQ = itertools.count(1)


def _pages(kind="freelist_count"):
    with engine.connect() as conn:
        return int(conn.exec_driver_sql(f"PRAGMA {kind}").scalar() or 0)


@pytest.fixture
def bulky(db):
    """A database with enough real rows that page counts mean something."""
    db.add(Source(id=1, name="probe", rss_url="http://probe.example/feed"))
    db.commit()

    def add(n):
        for i in [next(SEQ) for _ in range(n)]:
            db.add(Article(source_id=1, title=f"headline {i}",
                           url=f"http://probe.example/{i}",
                           summary="summary " * 40, content="body text " * 400,
                           published_at=utcnow(), fetched_at=utcnow(), importance=10))
        db.commit()
    return add


# ---------- the trap that returns success and does nothing ----------

def test_incremental_vacuum_actually_drains(bulky):
    """The bug that nearly shipped.

    Python's sqlite3 steps a row-less statement once, and incremental_vacuum
    frees one page per step — so the obvious `execute("PRAGMA
    incremental_vacuum(100000)")` frees a single page and raises nothing.
    """
    storage.ensure_incremental_vacuum()
    bulky(400)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM articles WHERE id % 10 != 0"))
    storage.checkpoint()

    waiting = _pages()
    assert waiting > 50, "not enough free pages to make this test meaningful"

    storage.reclaim(pages=10_000_000)

    assert _pages() < waiting * 0.1, (
        f"{_pages()} of {waiting} free pages still held — incremental_vacuum "
        f"is not being drained")


def test_reclaim_shrinks_the_file_on_disk(bulky):
    """The property that matters: the file gets smaller, not just tidier."""
    storage.ensure_incremental_vacuum()
    bulky(500)
    storage.checkpoint()
    full = storage.db_bytes()

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM articles WHERE id % 10 != 0"))
    storage.checkpoint()
    before = storage.db_bytes()
    assert before > full * 0.7, (
        "deleting alone appears to have shrunk the file — if SQLite ever starts "
        "doing that, this whole module is unnecessary")

    # What is actually recoverable, in bytes, rather than a guess at a fraction
    # of the file. This asserted `< full * 0.5` and passed here while failing on
    # CI at 58% — how much of a file is free pages depends on the page size and
    # on what else (the FTS index, the freelist itself) is in it, none of which
    # this is trying to test. Tie it to the mechanism instead.
    recoverable = _pages() * _pages("page_size")

    freed = storage.reclaim(pages=10_000_000)
    shrunk = before - storage.db_bytes()

    assert freed > 0
    assert shrunk >= recoverable * 0.9, (
        f"{shrunk} bytes came back from {recoverable} of free pages")
    assert abs(shrunk - freed) <= 64 * 1024, (
        f"reported {freed} freed but the file shrank by {shrunk}")


def test_the_freed_figure_is_not_understated(bulky):
    """Freed pages sit in the WAL until it is folded in.

    Measuring before that reported 1.5 MB for a pass that took a file from
    21 MB to 4 — true, useless, and the sort of number that makes an operator
    think the cleanup is not working.
    """
    storage.ensure_incremental_vacuum()
    bulky(500)
    storage.checkpoint()
    before = storage.db_bytes()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM articles WHERE id % 10 != 0"))

    freed = storage.reclaim(pages=10_000_000)

    actual = before - storage.db_bytes()
    assert freed >= actual * 0.9, f"reported {freed} freed, really freed {actual}"


def test_reclaim_is_harmless_before_conversion(db):
    """On a database still in NONE mode it should do nothing, not fail."""
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA auto_vacuum=NONE")
    assert storage.reclaim() >= 0          # no exception, no damage


# ---------- the conversion ----------

def test_conversion_is_refused_when_the_disk_is_too_full(monkeypatch):
    """The situation this exists for: VACUUM needs a second copy's worth.

    Trying anyway is how a full disk becomes a corrupt one.
    """
    monkeypatch.setattr(storage, "db_bytes", lambda: 900 * 1024 * 1024)
    monkeypatch.setattr(storage, "disk", lambda: {
        "ok": True, "free_bytes": 40 * 1024 * 1024, "total_bytes": 1024 ** 3})
    monkeypatch.setattr(storage, "auto_vacuum_mode", lambda: 0)

    result = storage.ensure_incremental_vacuum()

    assert not result["converted"]
    assert "not enough free space" in result["reason"]


def test_conversion_is_not_repeated(bulky):
    storage.ensure_incremental_vacuum()
    assert storage.auto_vacuum_mode() == 2
    again = storage.ensure_incremental_vacuum()
    assert not again["converted"] and again["reason"] == "already incremental"


# ---------- measuring ----------

def test_disk_reports_the_volume(db):
    info = storage.disk()
    assert info["ok"]
    assert info["total_bytes"] > 0
    assert 0 <= info["free_pct"] <= 100
    assert info["db_bytes"] > 0


def test_the_ceiling_follows_the_volume(monkeypatch):
    """A fixed megabyte figure would be wrong the moment the volume changed."""
    monkeypatch.setattr(storage, "DB_MAX_MB", 0)
    monkeypatch.setattr(storage, "DB_MAX_FRACTION", 0.5)
    monkeypatch.setattr(storage, "disk", lambda: {"ok": True, "total_bytes": 1000})
    assert storage.db_ceiling() == 500


def test_an_explicit_ceiling_wins(monkeypatch):
    monkeypatch.setattr(storage, "DB_MAX_MB", 10)
    assert storage.db_ceiling() == 10 * 1024 * 1024


def test_over_ceiling_reports_the_excess(monkeypatch):
    monkeypatch.setattr(storage, "db_ceiling", lambda: 100)
    monkeypatch.setattr(storage, "db_bytes", lambda: 250)
    assert storage.over_ceiling() == 150
    monkeypatch.setattr(storage, "db_bytes", lambda: 80)
    assert storage.over_ceiling() == 0


def test_low_space_is_flagged(monkeypatch):
    monkeypatch.setattr(storage, "LOW_SPACE_MB", 1024 * 1024)   # absurdly high
    assert storage.disk()["low"] is True


# ---------- the check that would have caught the outage ----------

def test_healthz_is_public_and_says_ok(client):
    """Fly's checker has no session, so this must sit outside /api."""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_healthz_fails_when_the_database_cannot_be_read(client, monkeypatch):
    """The actual outage: SQLite could not open the file, so nothing served.

    A deploy reported success through all of it because nothing was asked.
    """
    class Broken:
        def connect(self):
            raise OSError("disk I/O error")

    monkeypatch.setattr("backend.app.main.engine", Broken())
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert "database unreadable" in resp.json()["detail"]


def test_fly_actually_asks_for_it():
    """A health endpoint nothing calls is decoration."""
    from pathlib import Path
    toml = (Path(__file__).resolve().parents[1] / "fly.toml").read_text()
    assert "[[http_service.checks]]" in toml, "fly.toml has no health check"
    assert "/healthz" in toml
