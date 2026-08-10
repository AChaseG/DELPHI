"""Auto-discovery needs an answer to "how many sources is too many".

It had none. In a single day the catalog went from 4,655 enabled sources to
9,421, and nothing was wrong with any individual adoption — each one was a real
publisher that had appeared twice in coverage. There was simply no ceiling, so
the answer to how many was "more", and the article volume that follows from it
is what every other limit in the system then has to absorb.

The ceiling comes from how long a full sweep takes: the poller reaches about
12,500 sources an hour, so 12,000 are all polled within the hour. Twice that and
a feed waits ninety minutes to be read, which is a worse product than a smaller
catalog polled promptly.

Only adoption is bounded. Nothing is ever disabled or deleted to satisfy the
ceiling, and a source somebody added by hand is never counted against them.
"""
import pytest

from backend.app import discovery
from backend.app.models import Source


def _sources(db, count, *, enabled=True, added_by="auto-discovered"):
    for i in range(count):
        db.add(Source(name=f"outlet {added_by} {i}",
                      rss_url=f"http://outlet{added_by}{i}.example/feed",
                      enabled=enabled, added_by=added_by))
    db.commit()


# ---------- the measurement ----------

def test_room_below_the_ceiling(db, monkeypatch):
    monkeypatch.setattr(discovery, "MAX_SOURCES", 10)
    _sources(db, 4)
    assert discovery.at_capacity(db) == 0


def test_over_the_ceiling_reports_by_how_much(db, monkeypatch):
    monkeypatch.setattr(discovery, "MAX_SOURCES", 10)
    _sources(db, 13)
    assert discovery.at_capacity(db) == 3


def test_exactly_at_the_ceiling_is_not_over(db, monkeypatch):
    monkeypatch.setattr(discovery, "MAX_SOURCES", 5)
    _sources(db, 5)
    assert discovery.at_capacity(db) == 0


def test_disabled_sources_do_not_count(db, monkeypatch):
    """The ceiling is about polling cost, and a disabled source is not polled.
    Counting them would mean an audit that switched things off still blocked
    discovery."""
    monkeypatch.setattr(discovery, "MAX_SOURCES", 5)
    _sources(db, 5, enabled=False)
    _sources(db, 2, enabled=True, added_by="catalog")
    assert discovery.at_capacity(db) == 0


def test_zero_disables_the_ceiling(db, monkeypatch):
    monkeypatch.setattr(discovery, "MAX_SOURCES", 0)
    _sources(db, 50)
    assert discovery.at_capacity(db) == 0


# ---------- what it does to adoption ----------

@pytest.mark.anyio
async def test_a_full_catalog_adopts_nothing(db, monkeypatch):
    monkeypatch.setattr(discovery, "MAX_SOURCES", 3)
    monkeypatch.setattr(discovery, "AUTO_DISCOVER", True)
    _sources(db, 5)

    added = await discovery.discover_new_sources(
        db, {"newpaper.example": ("New Paper", "http://newpaper.example")})

    assert added == []


@pytest.mark.anyio
async def test_a_full_catalog_does_not_burn_the_sighting(db, monkeypatch):
    """A domain seen while the catalog is full should still be considered fresh
    if room appears later, rather than having quietly used up its sightings."""
    monkeypatch.setattr(discovery, "MAX_SOURCES", 1)
    monkeypatch.setattr(discovery, "AUTO_DISCOVER", True)
    _sources(db, 4)

    await discovery.discover_new_sources(
        db, {"newpaper.example": ("New Paper", "http://newpaper.example")})

    assert discovery._sightings(db, {"newpaper.example"}) in ({}, {"newpaper.example": 0})


def test_hand_added_sources_are_not_blocked_by_the_ceiling():
    """The ceiling governs what Delphi adopts on its own. A source a person
    added is a decision, and no automatic limit should overrule it."""
    import inspect
    # The guard lives in discover_new_sources, which is only the automatic path.
    assert "at_capacity" in inspect.getsource(discovery.discover_new_sources)
    from backend.app import main
    add_source = getattr(main, "add_source", None)
    if add_source is not None:
        assert "at_capacity" not in inspect.getsource(add_source)


def test_the_ceiling_never_removes_anything():
    """Disabling sources to fit would make a tuning knob destructive."""
    import inspect
    src = inspect.getsource(discovery)
    guard = src[src.index("def at_capacity"):src.index("def at_capacity") + 900]
    for destructive in ("delete", "enabled = False", "enabled=False"):
        assert destructive not in guard


def test_the_default_ceiling_matches_the_sweep_arithmetic():
    """Stated so it can be checked rather than trusted: the poller reaches
    POLL_BATCH + CITY_PER_TICK sources per tick."""
    from backend.app import ingest
    per_tick = ingest.POLL_BATCH + ingest.CITY_PER_TICK
    per_hour = per_tick * (3600 / ingest.POLL_TICK)
    assert discovery.MAX_SOURCES <= per_hour * 1.1, (
        f"{discovery.MAX_SOURCES} sources cannot be swept within an hour at "
        f"{per_hour:.0f}/hour")


@pytest.fixture
def anyio_backend():
    return "asyncio"
