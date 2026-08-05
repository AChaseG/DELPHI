"""A record of the moments the process could not answer.

Every "can't reach the server" so far has been diagnosed from whatever was
still in a log buffer that holds about a minute. By the time anybody looks the
machine is healthy again, so each round has been reconstruction rather than
reading — and twice a real cause turned out not to be the whole one, because
the evidence for the rest was already gone.

This measures the condition directly: a coroutine that asks for the loop every
second and notices how late it was. Thirteen seconds late means thirteen
seconds in which nothing else ran either — not a page, not an API call, not the
health check Fly abandons after ten. That is what "unreachable" is.
"""
import asyncio
import time

import pytest

from backend.app import ingest, watchdog


@pytest.fixture(autouse=True)
def _clean():
    watchdog.clear()
    watchdog.doing("idle")
    yield
    watchdog.clear()
    watchdog.doing("idle")


def test_nothing_is_recorded_when_nothing_is_wrong():
    assert watchdog.stalls() == []
    assert watchdog.worst() == 0.0


def test_a_stall_records_when_how_long_and_during_what():
    watchdog.doing("ingest:cluster")
    watchdog._record(13.4, watchdog.activity())

    (entry,) = watchdog.stalls()
    assert entry["late_s"] == 13.4
    assert entry["during"] == "ingest:cluster"
    assert entry["at"].endswith("Z"), "a timestamp you can match against a report"


def test_the_newest_is_first():
    """The question is always "what happened at about four this morning", so
    the most recent is the one being asked about."""
    for i in range(3):
        watchdog._record(float(i + 1), f"phase{i}")
    assert [s["during"] for s in watchdog.stalls()] == ["phase2", "phase1", "phase0"]


def test_the_record_is_bounded():
    """An unbounded list of a recurring fault is its own leak."""
    for i in range(watchdog.MAX_RECORDS * 3):
        watchdog._record(5.0, "x")
    assert len(watchdog.stalls()) == watchdog.MAX_RECORDS


def test_the_worst_is_reported_separately():
    for late in (4.0, 22.5, 6.0):
        watchdog._record(late, "x")
    assert watchdog.worst() == 22.5


# ---------- the threshold ----------

def test_it_notices_a_stall_before_it_becomes_an_outage():
    """Fly gives up at 10s. A threshold at or above that only ever records
    outages that already happened."""
    assert watchdog.STALL_S < 10.0


def test_it_is_not_so_sensitive_that_ordinary_delay_counts():
    """One core shared with the poller means small delays are normal. A record
    full of 0.2s hiccups is a record nobody reads."""
    assert watchdog.STALL_S >= 1.0
    assert watchdog.TICK_S <= watchdog.STALL_S


# ---------- it measures the loop, not itself ----------

@pytest.mark.anyio
async def test_it_records_a_real_stall(monkeypatch):
    """Exercised rather than asserted: block the loop and check it notices.

    Scaled down so the test is quick. Lateness is measured from when the sleep
    was *due*, so a block has to outlast the remaining tick as well as the
    threshold — which is the arithmetic that made the first version of this
    test fail against a watchdog that was working correctly.
    """
    monkeypatch.setattr(watchdog, "TICK_S", 0.05)
    monkeypatch.setattr(watchdog, "STALL_S", 0.3)
    watchdog.clear()
    watchdog.doing("ingest:cluster")

    watch = asyncio.create_task(watchdog.watch())
    await asyncio.sleep(0.02)
    # Blocking sleep on the loop thread — exactly what a long synchronous
    # phase does, which is the thing being detected.
    time.sleep(0.5)
    await asyncio.sleep(0.1)
    watch.cancel()

    assert watchdog.stalls(), "a blocked loop went unrecorded"
    entry = watchdog.stalls()[0]
    assert entry["late_s"] >= 0.3
    assert entry["during"] == "ingest:cluster"


@pytest.mark.anyio
async def test_a_healthy_loop_records_nothing(monkeypatch):
    monkeypatch.setattr(watchdog, "TICK_S", 0.05)
    monkeypatch.setattr(watchdog, "STALL_S", 0.3)
    watchdog.clear()
    watch = asyncio.create_task(watchdog.watch())
    await asyncio.sleep(0.3)
    watch.cancel()
    assert watchdog.stalls() == []


# ---------- it is wired in ----------

def test_the_poller_names_the_phase_it_is_in():
    """A stall is only actionable if it says which phase was running, and the
    phase timings are printed when a cycle ends — no use if it never does."""
    import inspect
    body = inspect.getsource(ingest._ingest_batch)
    for phase in ("fetch", "store", "enrich", "backfill", "cluster"):
        assert f'start("{phase}")' in body, f"{phase} is not named for the watchdog"


def test_it_goes_back_to_idle_when_a_cycle_ends():
    """Otherwise a stall between cycles is blamed on whatever ran last."""
    import inspect
    assert 'watchdog.doing("idle")' in inspect.getsource(ingest._ingest_batch)


def test_it_runs_even_when_the_poller_does_not():
    """The loop can be starved by a slow request or by this machine's single
    core, and a watchdog that only runs alongside the poller cannot say so."""
    import inspect
    from backend.app import main
    src = inspect.getsource(main.lifespan)
    watch_at = src.index("watchdog.watch()")
    guard_at = src.index("if not DISABLE_INGEST")
    assert watch_at > guard_at
    # ...but outside the guarded block: same indentation as `tasks = []`.
    line = [ln for ln in src.splitlines() if "watchdog.watch()" in ln][0]
    assert len(line) - len(line.lstrip()) == 4, (
        "the watchdog only starts when ingestion does")


def test_the_record_is_served_where_an_operator_looks():
    import inspect
    from backend.app import main
    assert "watchdog.stalls()" in inspect.getsource(main.ingest_status)


@pytest.fixture
def anyio_backend():
    return "asyncio"
