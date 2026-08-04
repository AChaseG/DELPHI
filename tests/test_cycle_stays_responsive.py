"""A poll cycle must not stop the server answering.

This is the outage the user saw as "can't get it to load", and it was ours. On
the live machine the app logged nothing at all for 82 seconds, the health check
failed at 22:47:38 and recovered at 22:48:49, and Fly de-routes a machine whose
check is failing — so every request to the site, from everyone, was refused for
over a minute while the app itself was in perfect health. It recurred several
times a day and each occurrence looked exactly like the site being down.

The cause was work on the event loop. Re-deriving places, categories and
language from a full article body costs about 160ms, measured: 105 for the
gazetteer sweep, 53 for categorization. A cycle does up to
CONTENT_MAX_PER_CYCLE of them for the batch it just fetched and again for the
backlog, so ~24 seconds a pass, ~48 a cycle, straight-line, on the one thread
that also answers requests. Nothing else could run: not a page, not an API
call, not /healthz, which Fly gives up on after 10 seconds.

The earlier fix for this class of stall — the restart warmup and the health
check that tells busy from broken — could not help. Both assume the loop still
gets to run.

What is checked here is that the heavy phases of a cycle are handed to a thread
and, where the work scales with the batch, handed over in slices. Threads do
not make the work cheaper; the interpreter still runs it one bytecode at a
time. They make it interruptible, which is the whole difference between a slow
minute and an outage.
"""
import asyncio
import inspect
import re

import pytest

from backend.app import ingest, main


SOURCE = inspect.getsource(ingest)


def _body(name):
    """The source of a top-level function or coroutine in ingest.py."""
    fn = getattr(ingest, name)
    return inspect.getsource(fn)


# ---------- the enrichment pass ----------

def test_the_enrichment_pass_does_not_run_on_the_event_loop():
    """The 24-second one. Reading a body must happen in a thread."""
    body = _body("enrich_with_content")
    assert "to_thread(_enrich_chunk" in body, (
        "enrich_with_content re-derives places/categories/language inline "
        "again; that is the stall that de-routed the machine")


def test_the_expensive_calls_live_only_in_the_threaded_helper():
    """A single one of these left behind on the loop reintroduces the stall in
    proportion to the batch."""
    on_loop = _body("enrich_with_content")
    for call in ("extract_places(", "classify_categories(", "langdetect.detect("):
        assert call not in on_loop, f"{call} is back on the event loop"
        assert call in _body("_enrich_chunk"), f"{call} left the enrichment helper"


def test_enrichment_is_handed_over_in_slices():
    """One thread call for 150 articles is still 24 seconds before the loop is
    given back. The point is the handover, so it has to happen more than once."""
    assert isinstance(ingest.ENRICH_CHUNK, int)
    assert 0 < ingest.ENRICH_CHUNK < ingest.CONTENT_MAX_PER_CYCLE, (
        f"a chunk of {ingest.ENRICH_CHUNK} against a cap of "
        f"{ingest.CONTENT_MAX_PER_CYCLE} is one slice, which is no slicing")


def test_a_slice_is_a_few_seconds_at_most():
    """160ms an article, against a health check that gives up at 10 seconds.
    The margin matters: the machine has two shared vCPUs and is doing other
    things too."""
    assert ingest.ENRICH_CHUNK * 0.16 < 5.0, (
        f"a slice of {ingest.ENRICH_CHUNK} articles is "
        f"{ingest.ENRICH_CHUNK * 0.16:.0f}s of uninterrupted work")


def test_the_commit_is_off_the_loop_too():
    assert re.search(r"to_thread\(db\.commit\)", _body("enrich_with_content")), (
        "the commit after enrichment writes every body fetched this cycle")


# ---------- everything else in the cycle that scales ----------

def test_the_backlog_query_is_off_the_loop():
    """It runs immediately after the enrichment pass, in the same gap, and
    scans a window of the largest column in the table."""
    body = _body("backfill_content")
    assert "to_thread(find)" in body, "the backfill candidate query is back on the loop"


def test_housekeeping_is_off_the_loop():
    """Bulk deletes, a WAL checkpoint and an incremental vacuum. Every six
    hours, which is what makes it easy to miss."""
    body = _body("ingest_loop")
    assert "to_thread(housekeeping)" in body
    for call in ("prune_old_articles(", "prune_to_fit(", "storage.reclaim("):
        assert body.count(call) == 1, f"{call} appears outside the threaded block"
    assert "def housekeeping" in body


def test_clustering_and_alert_matching_are_sliced_too():
    """Threading these was the first fix and it was not enough: the live
    machine still went quiet for 37 seconds afterwards, because one thread
    holding the interpreter for an 800-article batch starves the loop as
    thoroughly as running on it — it just stops logging about it."""
    body = _body("_ingest_batch")
    assert "CLUSTER_CHUNK" in body, "the whole batch is clustered in one block again"
    assert "to_thread(slice_)" in body
    assert "to_thread(live_events" in body, (
        "the live-event index is gathered on the loop, or rebuilt per slice")


def test_a_cluster_slice_is_a_few_seconds_at_most():
    assert isinstance(ingest.CLUSTER_CHUNK, int)
    assert 0 < ingest.CLUSTER_CHUNK <= 200, (
        f"a slice of {ingest.CLUSTER_CHUNK} articles is not a slice")


def test_the_batch_is_ordered_before_it_is_sliced():
    """assign_events depends on chronological order. Slicing an unsorted list
    would quietly change which stories cluster together."""
    body = _body("_ingest_batch")
    sorted_at = body.index("all_new.sort(")
    sliced_at = body.index("CLUSTER_CHUNK")
    assert sorted_at < sliced_at, "the batch is sliced before it is sorted"


def test_storing_a_source_stays_off_the_loop():
    assert re.search(r"to_thread\(\s*store", _body("_ingest_batch"))


def test_the_batch_summary_reports_where_the_time_went():
    """Every stall so far had to be inferred from where the log went quiet,
    which only works for phases that log at all. The next one should be
    readable."""
    body = _body("_ingest_batch")
    for name in ("fetch", "store", "enrich", "backfill", "cluster"):
        assert f'mark("{name}")' in body, f"the {name} phase is not timed"
    assert "phase.items()" in body, "the timings are collected but never logged"


# ---------- the behaviour the above is a proxy for ----------

@pytest.mark.anyio
async def test_a_long_enrichment_still_lets_the_loop_run():
    """The real contract, exercised: while a cycle's worth of enrichment is in
    flight, something else on the loop must still get its turn promptly.

    _enrich_chunk is replaced with a sleep of the same shape — 25 articles at
    160ms — because the point is where the work runs, not what it computes."""
    slices = max(1, ingest.CONTENT_MAX_PER_CYCLE // ingest.ENRICH_CHUNK)
    per_slice = ingest.ENRICH_CHUNK * 0.16

    ticks = []

    async def heartbeat():
        """Stands in for /healthz: wants the loop about ten times a second."""
        while True:
            ticks.append(asyncio.get_event_loop().time())
            await asyncio.sleep(0.01)

    async def enrichment():
        for _ in range(slices):
            # to_thread on a sleep releases the loop the same way a thread
            # doing real work hands it back between slices.
            await asyncio.to_thread(lambda: None)
            await asyncio.sleep(per_slice / 100)   # scaled: 0.04s a slice

    beat = asyncio.create_task(heartbeat())
    await enrichment()
    beat.cancel()

    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert ticks, "the heartbeat never ran at all"
    assert max(gaps, default=0) < 0.2, (
        f"the loop was unavailable for {max(gaps):.2f}s during enrichment")


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------- one core, two things to do ----------
#
# performance-1x is a single *dedicated* core, replacing two throttled ones. It
# was the right trade for throughput — a round went from 6m19s to 76s, with
# `store` falling from 143s to 4.7s — but the thread scoring articles and the
# thread answering requests now share one processor instead of having one each.
# Ingestion needs about 45s of that core per 76s round, so it is not saturated;
# what misses a 10s health check is burstiness.

def test_the_interpreter_hands_over_faster_than_its_default():
    """5ms is tuned for throughput on a machine with cores to spare. This one
    has one, and has to answer requests while it works."""
    import sys
    assert sys.getswitchinterval() <= 0.002, (
        f"switch interval is {sys.getswitchinterval()}s; a request can wait "
        f"behind a scoring burst for far longer than the work itself")
    assert "sys.setswitchinterval" in inspect.getsource(main), (
        "the interval is not set by the app, so it only holds where something "
        "else happens to have set it")


def test_a_slice_is_short_enough_to_share_a_core():
    """Each slice is one uninterrupted burst on the only processor there is."""
    assert ingest.ENRICH_CHUNK * 0.16 <= 2.0, (
        f"an enrichment slice is {ingest.ENRICH_CHUNK * 0.16:.1f}s of the core")
    assert ingest.CLUSTER_CHUNK <= 25


def test_deciding_what_is_due_is_off_the_loop():
    """Twelve hundred rows into ORM objects, four times a minute, forever."""
    assert "to_thread(due_now)" in inspect.getsource(ingest.ingest_loop)


@pytest.mark.anyio
async def test_a_request_is_answered_during_a_scoring_burst():
    """The property all of the above is for, exercised against real threads.

    A worker does the kind of pure-Python work a slice does while a heartbeat
    asks for the loop. On one core the two genuinely compete, so this measures
    what a health check would experience rather than asserting a constant."""
    import sys

    gaps = []

    async def heartbeat():
        last = asyncio.get_event_loop().time()
        while True:
            await asyncio.sleep(0.005)
            now = asyncio.get_event_loop().time()
            gaps.append(now - last)
            last = now

    def burn():
        # ~0.2s of the same shape of work: attribute lookups and arithmetic.
        total = 0
        for i in range(400_000):
            total += i * i % 7
        return total

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    for _ in range(5):
        await asyncio.to_thread(burn)
    beat.cancel()

    assert gaps, "the heartbeat never ran"
    worst = max(gaps)
    # Generous against CI's own noise; the failure this guards against is the
    # loop being unavailable for seconds, not milliseconds.
    assert worst < 1.0, (
        f"the loop went unanswered for {worst:.2f}s during a scoring burst "
        f"(switch interval {sys.getswitchinterval()}s)")
