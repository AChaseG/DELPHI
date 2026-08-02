"""Column scans must not be run on the event loop.

A board is six or more columns, and each one is a SQL read followed by the
matcher sifting rows in Python. Run that directly inside an `async def`
handler and it holds the whole process: while one column is being matched
nothing else is served, so the cheapest read in the app — the panel listing a
reader's watched places — can sit behind a board and time out.

These check the property rather than a stopwatch: that the scan is handed to a
worker thread, and that no more than a couple run at once. A timing assertion
would only measure the machine it happened to run on.
"""
import asyncio
import threading

import pytest

from backend.app import main


def test_the_scan_runs_off_the_event_loop(db, monkeypatch):
    loop_thread = threading.get_ident()
    seen = {}

    def spy(*a, **kw):
        seen["thread"] = threading.get_ident()
        return []

    monkeypatch.setattr(main, "query_articles", spy)
    asyncio.run(main.scan_articles(db, {}, sort="newest", limit=10))

    assert seen["thread"] != loop_thread, \
        "the scan ran on the event loop; every other request waited for it"


def test_the_loop_keeps_serving_while_a_scan_runs(db, monkeypatch):
    """The point of the thread: other work proceeds during a slow column."""
    started = threading.Event()
    release = threading.Event()

    def slow(*a, **kw):
        started.set()
        release.wait(5)
        return []

    monkeypatch.setattr(main, "query_articles", slow)

    async def go():
        scan = asyncio.create_task(main.scan_articles(db, {}, sort="newest", limit=10))
        await asyncio.to_thread(started.wait, 5)
        # The loop is free right now, with the scan still inside the matcher.
        ticks = 0
        for _ in range(5):
            await asyncio.sleep(0)
            ticks += 1
        blocked = scan.done()
        release.set()
        await scan
        return ticks, blocked

    ticks, finished_early = asyncio.run(go())
    assert ticks == 5, "the event loop was blocked by the scan"
    assert not finished_early


def test_a_whole_board_does_not_scan_all_at_once(db, monkeypatch):
    """Threads take turns at the interpreter, so an unbounded board is slower
    than a queued one — six columns measured 2.69s against 0.94s."""
    live = 0
    peak = 0
    lock = threading.Lock()

    def busy(*a, **kw):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        threading.Event().wait(0.05)
        with lock:
            live -= 1
        return []

    monkeypatch.setattr(main, "query_articles", busy)

    async def board():
        await asyncio.gather(*(main.scan_articles(db, {}, sort="newest", limit=10)
                               for _ in range(8)))

    asyncio.run(board())
    assert peak <= main.SCAN_CONCURRENCY, \
        f"{peak} scans ran at once; a board floods the threadpool"


def test_a_failing_scan_gives_its_slot_back(db, monkeypatch):
    """A scan that raises must not leave the queue one place narrower — a few
    of those and the board stops loading at all."""
    def boom(*a, **kw):
        raise RuntimeError("no")

    async def go():
        monkeypatch.setattr(main, "query_articles", boom)
        for _ in range(main.SCAN_CONCURRENCY + 2):
            with pytest.raises(RuntimeError):
                await main.scan_articles(db, {}, sort="newest", limit=10)
        monkeypatch.setattr(main, "query_articles", lambda *a, **kw: [])
        await asyncio.wait_for(main.scan_articles(db, {}, sort="newest", limit=10), 5)

    asyncio.run(go())


def test_the_limiter_survives_a_new_event_loop(db, monkeypatch):
    """Each test here runs its own loop; a limiter bound to the first one would
    fail every case after it, and the same would happen to a reloaded worker."""
    monkeypatch.setattr(main, "query_articles", lambda *a, **kw: [])
    for _ in range(3):
        asyncio.run(main.scan_articles(db, {}, sort="newest", limit=10))
