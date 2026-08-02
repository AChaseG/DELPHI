"""A restart must not take the site down.

Every source is overdue the moment the process starts, so the first ticks after
a deploy poll a full batch each — hundreds of articles to score, index and
cluster, and as many article bodies to fetch. On two shared vCPUs that
saturates the machine, and the machine is the only one there is.

That is not hypothetical. On the live deployment the health check could not be
answered inside its 10s timeout, Fly stopped routing to the machine — "could
not find a good candidate within 40 attempts at load balancing" — and the site
was unreachable for about two minutes with nothing actually wrong with it. The
check meant to catch a dead app caused the outage.

Two defences, one for each half: the poller starts gently, and the health check
tells being busy apart from being broken.
"""
import asyncio

import pytest

from backend.app import ingest, main


# ---------- the poller starts gently ----------

def test_the_first_tick_after_a_restart_is_a_fraction_of_a_batch():
    assert ingest.warmup_batch(ingest.POLL_BATCH, 0.0) == ingest.WARMUP_FIRST_BATCH


def test_it_is_back_to_full_speed_after_the_warmup():
    assert ingest.warmup_batch(ingest.POLL_BATCH, ingest.WARMUP_SECONDS) == ingest.POLL_BATCH
    assert ingest.warmup_batch(ingest.POLL_BATCH,
                               ingest.WARMUP_SECONDS * 10) == ingest.POLL_BATCH


def test_it_ramps_rather_than_stepping():
    """A step at the end of the window would just move the stampede later."""
    sizes = [ingest.warmup_batch(ingest.POLL_BATCH, ingest.WARMUP_SECONDS * f / 10)
             for f in range(11)]
    assert sizes == sorted(sizes), sizes
    assert sizes[0] < sizes[5] < sizes[-1], sizes


def test_it_never_returns_nothing():
    """A batch of zero would stall ingestion completely, not slow it down."""
    for full in (1, 2, 5, ingest.CITY_PER_TICK, ingest.POLL_BATCH):
        for t in (0.0, 1.0, ingest.WARMUP_SECONDS / 2, ingest.WARMUP_SECONDS):
            assert ingest.warmup_batch(full, t) >= 1, (full, t)


def test_a_batch_smaller_than_the_warmup_size_is_left_alone():
    """City feeds are already capped at 20; there is nothing to ramp down to."""
    small = ingest.WARMUP_FIRST_BATCH - 1
    assert ingest.warmup_batch(small, 0.0) == small


def test_it_never_exceeds_the_configured_batch():
    for t in (-5.0, 0.0, ingest.WARMUP_SECONDS / 3, ingest.WARMUP_SECONDS * 2):
        assert ingest.warmup_batch(ingest.POLL_BATCH, t) <= ingest.POLL_BATCH


# ---------- busy is not broken ----------

@pytest.fixture(autouse=True)
def _fresh_health_state():
    main._DB_STATE = (0.0, True, "")
    yield
    main._DB_STATE = (0.0, True, "")


def test_a_healthy_database_passes(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_an_unreadable_database_fails_the_check(client, monkeypatch):
    """The one condition where restarting is the right response."""
    main._DB_STATE = (0.0, True, "")
    monkeypatch.setattr(main, "_db_readable",
                        lambda: (False, "database unreadable: OperationalError"))

    r = client.get("/healthz")

    assert r.status_code == 503
    assert "unreadable" in r.json()["detail"]


def test_a_busy_machine_still_answers_and_answers_quickly(client, monkeypatch):
    """The outage this exists to prevent.

    Fly gives the check 10 seconds and pulls the machine out of rotation if it
    does not hear back. So it is not enough to eventually return 200 — a probe
    that cannot get a turn has to be abandoned, and the last answer returned in
    its place, inside the budget.
    """
    import time as clock
    client.get("/healthz")                       # establish a good answer
    monkeypatch.setattr(main, "HEALTH_RECHECK_S", 0.0)
    monkeypatch.setattr(main, "HEALTH_PROBE_TIMEOUT_S", 0.1)

    def too_busy():
        clock.sleep(10)                          # never gets a turn in time
        return True, ""

    monkeypatch.setattr(main, "_db_readable", too_busy)

    t = clock.perf_counter()
    r = client.get("/healthz")
    took = clock.perf_counter() - t

    assert r.status_code == 200, "a busy machine was reported as a dead one"
    assert took < 2.0, f"the check took {took:.1f}s; Fly waits 10s and then de-routes"


def test_a_broken_database_is_not_hidden_by_the_cache(client, monkeypatch):
    """The other way to get this wrong: remembering "ok" forever."""
    client.get("/healthz")
    monkeypatch.setattr(main, "HEALTH_RECHECK_S", 0.0)
    monkeypatch.setattr(main, "_db_readable", lambda: (False, "database unreadable: X"))

    assert client.get("/healthz").status_code == 503


def test_the_answer_is_reused_between_checks(client, monkeypatch):
    """Fly asks every 30s; the database is not asked every time somebody
    else's script hits the endpoint."""
    client.get("/healthz")
    calls = []
    monkeypatch.setattr(main, "_db_readable",
                        lambda: (calls.append(1), (True, ""))[1])

    for _ in range(5):
        assert client.get("/healthz").status_code == 200

    assert not calls, f"{len(calls)} database probes for 5 checks inside the window"


def test_it_needs_no_session(client):
    """Fly's checker has none, and a 401 would read as an unhealthy app."""
    assert client.get("/healthz").status_code == 200
