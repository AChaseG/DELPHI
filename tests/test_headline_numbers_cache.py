"""The dashboard's headline numbers must never be counted on a page load.

`/api/meta` carries five aggregates. They are identical for every reader and
nothing in the app makes a decision from them — they are a running total quoted
to a human. Caching them was the first fix, with a sixty-second window, and it
was not enough: whoever's page load arrived after the window expired paid the
whole cost, and one of the five was measured at **78 seconds** on the live
database. Past the 30s a browser waits, so the dashboard reported that the
server could not be reached while the server was answering health checks
throughout.

    total_articles   0.32s
    articles_24h     0.18s
    countries_24h   78.04s   <-- count(DISTINCT country) over a day
    sources_ok       0.06s
    sources_total    0.01s

Two changes, and this file covers both. The count is fast now: a composite
(fetched_at, country) index makes it covering, measured 25x at 1.36M rows.
And the counting happens on a timer rather than on a request, so if any of them
becomes slow again the cost is a stale number instead of an unreachable site.
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


# ---------- a request never counts ----------

def test_reading_the_numbers_runs_no_queries():
    """The whole point. A reader must not be able to trigger the aggregates,
    however stale the last answer is."""
    db = _CountingDB()
    main.refresh_stats(db)
    after_refresh = db.queries
    assert after_refresh > 0, "the refresh did not query anything"

    for _ in range(50):
        main._stats()
    assert db.queries == after_refresh, (
        f"{db.queries - after_refresh} queries ran for readers")


def test_reading_needs_no_database_at_all():
    """Called with nothing, because it has nothing to ask."""
    main.refresh_stats(_CountingDB())
    assert main._stats()["total_articles"] == 7


def test_before_the_first_count_it_answers_zeros_not_a_wait():
    """A cold start shows zeros for a few seconds. That is a worse dashboard;
    a thirty-second wait is a broken one."""
    stats = main._stats()
    assert stats["ready"] is False
    assert stats["total_articles"] == 0


def test_a_counted_answer_says_it_is_ready():
    main.refresh_stats(_CountingDB())
    assert main._stats()["ready"] is True


def test_every_aggregate_is_counted_by_the_refresh():
    """One left on the request path would make the endpoint slow again."""
    db = _CountingDB()
    stats = main.refresh_stats(db)
    numbers = {k: v for k, v in stats.items() if k != "ready"}
    assert set(numbers) == {"total_articles", "articles_24h", "countries_24h",
                            "sources_ok", "sources_total"}
    assert db.queries == len(numbers), (
        f"{db.queries} queries for {len(numbers)} numbers")


# ---------- it keeps being counted ----------

def test_there_is_a_loop_that_recounts():
    """A number nobody recomputes is a number that silently ages."""
    import inspect
    src = inspect.getsource(main.stats_loop)
    assert "refresh_stats" in src
    assert "asyncio.to_thread" in src, "counting must stay off the event loop"
    assert "while True" in src


def test_the_loop_survives_a_failed_count():
    """It runs for the life of the process; one bad pass must not end it."""
    import inspect
    src = inspect.getsource(main.stats_loop)
    assert "except Exception" in src


def test_the_loop_runs_even_when_ingestion_is_off():
    """Switching off the poller must not freeze the numbers forever."""
    import inspect
    src = inspect.getsource(main.lifespan)
    at_loop = src.index("stats_loop()")
    at_guard = src.index("if not DISABLE_INGEST")
    assert at_loop > at_guard
    line = [ln for ln in src.splitlines() if "stats_loop()" in ln][0]
    assert len(line) - len(line.lstrip()) == 4, (
        "the numbers only refresh when ingestion is on")


def test_the_interval_is_sensible():
    """Long enough not to be constant work, short enough that the counter
    visibly moves while someone watches it."""
    assert 10 <= main.STATS_CACHE_TTL <= 300


# ---------- the index that makes counting cheap ----------

def test_the_covering_index_is_declared():
    assert "ix_articles_fetched_country" in main._WANTED_INDEXES
    assert main._WANTED_INDEXES["ix_articles_fetched_country"] == \
        "articles(fetched_at, country)", (
        "country has to be in the index or the query reads 170,000 full rows")


def test_indexes_are_built_after_the_port_is_bound():
    """Building one scans the whole table. In the migrations, before uvicorn
    binds, that is tens of seconds against a sixty-second grace period — a
    restart loop instead of a slow boot."""
    import inspect
    src = inspect.getsource(main.lifespan)
    assert "_ensure_indexes()" in src
    assert "_ensure_indexes" not in inspect.getsource(main._ensure_schema)
    assert "asyncio.to_thread" in inspect.getsource(main._ensure_indexes)


def test_a_failed_index_build_does_not_take_the_app_down():
    """A missing index is slow, not broken."""
    import inspect
    assert "except Exception" in inspect.getsource(main._ensure_indexes)


def test_the_index_is_created_if_not_exists():
    """It runs on every boot, so it must be idempotent and cheap once built."""
    import inspect
    assert "CREATE INDEX IF NOT EXISTS" in inspect.getsource(main._ensure_indexes)


# ---------- the endpoint's contract ----------

def test_meta_still_reports_the_numbers(client, register):
    """The dashboard reads these keys by name."""
    body = client.get("/api/meta", headers=register("statsreader")).json()
    numbers = {k: v for k, v in body["stats"].items() if k != "ready"}
    assert set(numbers) == {"total_articles", "articles_24h",
                            "countries_24h", "sources_ok", "sources_total"}
    assert all(isinstance(v, int) for v in numbers.values())


def test_meta_is_not_slowed_by_the_numbers(client, register):
    """A request that reads a dict cannot be the slowest thing on the page."""
    headers = register("timedreader")
    t0 = time.perf_counter()
    res = client.get("/api/meta", headers=headers)
    assert res.status_code == 200
    assert time.perf_counter() - t0 < 5, "meta is doing work it should not"
