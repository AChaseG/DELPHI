"""The dashboard's headline numbers must not be recomputed per reader.

`/api/meta` carries four aggregates, one of them a count of every article ever
stored. Every page load asks for them, and they are identical for everybody.
On the live machine, with the poller busy, this was logged at **31.8s** — past
the 30s the browser waits — so the dashboard told its reader the server could
not be reached while the server was answering health checks on schedule
throughout. That is the specific request behind the "Can't reach the server"
screen.

Caching is sound here because of what the numbers are: a running total of the
catalog, quoted to a human. A minute-old total is the same total. Nothing in
the app makes a decision from them.
"""
import time

import pytest

from backend.app import main


@pytest.fixture(autouse=True)
def _clear_cache():
    main._invalidate_stats_cache()
    yield
    main._invalidate_stats_cache()


class _CountingDB:
    """Stands in for the session, counting how often the aggregates are run."""

    def __init__(self):
        self.queries = 0

    def scalar(self, _stmt):
        self.queries += 1
        return 7


def test_the_numbers_are_computed_once_and_reused():
    db = _CountingDB()
    first = main._stats(db)
    after_first = db.queries
    assert after_first > 0, "nothing was queried at all"

    for _ in range(20):
        main._stats(db)
    assert db.queries == after_first, (
        f"the aggregates ran {db.queries - after_first} extra times for readers "
        f"who could have had the answer already computed")
    assert main._stats(db) == first


def test_every_aggregate_is_inside_the_cache():
    """One left outside would keep a full-table count on the per-request path
    and the endpoint would still be the slowest thing on the page."""
    db = _CountingDB()
    stats = main._stats(db)
    assert set(stats) == {"total_articles", "articles_24h", "countries_24h",
                          "sources_ok", "sources_total"}
    assert db.queries == len(stats), (
        f"{db.queries} queries for {len(stats)} numbers")


def test_the_answer_does_not_go_stale_for_long():
    """Long enough to absorb a burst of readers, short enough that the counter
    visibly moves while someone watches it."""
    assert 10 <= main.STATS_CACHE_TTL <= 300


def test_it_recomputes_once_the_window_passes(monkeypatch):
    db = _CountingDB()
    main._stats(db)
    first = db.queries

    later = time.monotonic() + main.STATS_CACHE_TTL + 1
    monkeypatch.setattr(main.time, "monotonic", lambda: later)
    main._stats(db)
    assert db.queries == first * 2, "the numbers never refresh"


def test_meta_still_reports_the_numbers(client, register):
    """The endpoint's contract is unchanged — this is a caching change, and the
    dashboard reads these keys by name."""
    body = client.get("/api/meta", headers=register("statsreader")).json()
    assert set(body["stats"]) == {"total_articles", "articles_24h",
                                  "countries_24h", "sources_ok", "sources_total"}
    assert all(isinstance(v, int) for v in body["stats"].values())
