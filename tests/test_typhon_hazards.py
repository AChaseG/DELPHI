"""Typhon: live hazards on the map, and the ways a hazard feed goes wrong.

The happy path is the easy half. A hazard feed is re-read *wholesale* every
poll — the same three hundred fires, every fifteen minutes, forever — and that
shape has two failure modes that a first implementation will ship with unless
they are written down as tests:

  1. **A failed fetch that looks like an empty world.** Providers rate-limit,
     go down, and rename fields. If a zero-row answer were taken at face value
     the prune would empty the table, and the next good poll would re-insert
     everything as new. Once proximity alerting lands on top of this, that is
     every watcher being told about every fire they were already told about.
     So: nothing said means nothing pruned.

  2. **"Changed" meaning "appeared in this response".** That describes all of
     them, every time. What a reader needs is the hazard that is new or the one
     that got worse — which is why escalation steps on buckets, never on the
     raw score. A fire creeping from 62 to 63 is not news.

The fixtures here are captured payload shapes, not live calls: the suite has no
network, and a test that needs one is a test that fails on a Sunday for reasons
nobody can see.
"""
import pytest
from sqlalchemy import select

from backend.app import hazards
from backend.app.models import Hazard


def _feature(irwin, name, lat, lon, acres=1000.0, contained=0.0, discovered=None):
    return {
        "attributes": {
            "IrwinID": irwin, "IncidentName": name,
            "IncidentSize": acres, "PercentContained": contained,
            "FireDiscoveryDateTime": discovered, "POOState": "US-CA",
            "POOCounty": "Shasta", "FireCause": "Natural",
            "IncidentShortDescription": "",
        },
        "geometry": {"x": lon, "y": lat},
    }


def _payload(*features):
    return {"features": list(features)}


# ---------- normalization ----------

def test_an_incident_becomes_a_hazard():
    rows = hazards.normalize_wfigs(_payload(
        _feature("abc-123", "Sawtooth Fire", 40.5, -122.3, acres=5000, contained=20)))

    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "wildfire" and row["provider"] == "wfigs"
    assert row["external_id"] == "abc-123"
    assert row["name"] == "Sawtooth Fire"
    assert (row["lat"], row["lon"]) == (40.5, -122.3)
    assert row["country"] == "US"
    assert row["raw"]["acres"] == 5000 and row["raw"]["containment"] == 20


def test_an_incident_with_no_point_is_dropped():
    """ArcGIS returns incidents whose location is still being established.
    There is nothing to draw and nothing to measure a distance from."""
    payload = _payload(_feature("no-geom", "Somewhere", 0, 0))
    payload["features"][0]["geometry"] = {}
    assert hazards.normalize_wfigs(payload) == []


def test_an_incident_with_no_id_is_dropped():
    """The id is what makes the next poll an update rather than a duplicate."""
    payload = _payload(_feature("", "Nameless", 40.0, -120.0))
    assert hazards.normalize_wfigs(payload) == []


def test_a_point_off_the_earth_is_dropped():
    assert hazards.normalize_wfigs(_payload(
        _feature("bad-coords", "Impossible", 91.0, -400.0))) == []


def test_an_unnamed_incident_still_gets_a_label():
    rows = hazards.normalize_wfigs(_payload(_feature("x", "", 40.0, -120.0)))
    assert rows[0]["name"] == "Unnamed incident"


def test_an_arcgis_error_body_is_not_an_empty_world():
    """ArcGIS answers HTTP 200 with an `error` key. Reading that as "no fires"
    is the whole of failure mode 1."""
    assert hazards.normalize_wfigs({"error": {"code": 400}}) is None
    assert hazards.normalize_wfigs({"nothing": "useful"}) is None
    assert hazards.normalize_wfigs("not a dict") is None


# ---------- severity ----------

def test_a_bigger_fire_scores_higher():
    scores = [hazards._wildfire_severity(a, 0) for a in (1, 50, 500, 5_000, 50_000)]
    assert scores == sorted(scores), scores


def test_containment_brings_it_down_but_never_to_nothing():
    """A fire still being reported is still burning."""
    wild = hazards._wildfire_severity(50_000, 0)
    held = hazards._wildfire_severity(50_000, 95)
    assert held < wild
    assert held >= 5


def test_a_huge_contained_fire_ranks_below_a_smaller_loose_one():
    """The judgement the scale exists to make."""
    assert (hazards._wildfire_severity(150_000, 95)
            < hazards._wildfire_severity(25_000, 0))


# ---------- the poll ----------

def _run(monkeypatch, db, answers):
    """Drive poll() with canned provider answers. `answers` maps a provider
    name to what its fetch returns — a list, or None for "could not say"."""
    async def fake(name):
        return answers[name]

    monkeypatch.setattr(hazards, "PROVIDERS",
                        {name: (lambda _c, n=name: fake(n)) for name in answers})
    import asyncio
    return asyncio.run(hazards.poll(db))


def test_a_poll_stores_what_it_found(client, db, monkeypatch):
    rows = hazards.normalize_wfigs(_payload(
        _feature("a", "Sawtooth", 40.0, -122.0),
        _feature("b", "Ridge", 41.0, -121.0)))

    result = _run(monkeypatch, db, {"wfigs": rows})

    assert result["ok"] is True and result["added"] == 2
    assert db.scalar(select(Hazard).where(Hazard.external_id == "a")).name == "Sawtooth"


def test_the_same_poll_twice_adds_nothing(client, db, monkeypatch):
    """Every poll re-reports every fire. If that read as new each time, the
    table would grow without bound and every hazard would be 'new' forever."""
    rows = hazards.normalize_wfigs(_payload(_feature("a", "Sawtooth", 40.0, -122.0)))

    _run(monkeypatch, db, {"wfigs": rows})
    second = _run(monkeypatch, db, {"wfigs": rows})

    assert second["added"] == 0
    assert second["updated"] == 0, "an unchanged fire is not a change"
    assert second["escalated"] == 0
    assert db.query(Hazard).count() == 1


def test_a_growing_fire_is_an_escalation(client, db, monkeypatch):
    """The event this whole feature exists to deliver, and the one an Article
    could never carry: the row is rewritten, not inserted."""
    small = hazards.normalize_wfigs(_payload(
        _feature("a", "Sawtooth", 40.0, -122.0, acres=200)))
    huge = hazards.normalize_wfigs(_payload(
        _feature("a", "Sawtooth", 40.0, -122.0, acres=40_000)))

    _run(monkeypatch, db, {"wfigs": small})
    before = db.scalar(select(Hazard).where(Hazard.external_id == "a")).severity
    result = _run(monkeypatch, db, {"wfigs": huge})

    assert result["escalated"] == 1
    after = db.scalar(select(Hazard).where(Hazard.external_id == "a"))
    assert after.severity > before
    assert db.query(Hazard).count() == 1, "it grew; it is not a second fire"


def test_a_fire_being_brought_under_control_is_not_an_escalation(client, db, monkeypatch):
    big = hazards.normalize_wfigs(_payload(
        _feature("a", "Sawtooth", 40.0, -122.0, acres=40_000, contained=0)))
    held = hazards.normalize_wfigs(_payload(
        _feature("a", "Sawtooth", 40.0, -122.0, acres=40_000, contained=90)))

    _run(monkeypatch, db, {"wfigs": big})
    result = _run(monkeypatch, db, {"wfigs": held})

    assert result["escalated"] == 0


def test_a_nudge_within_the_same_band_is_not_a_change(client, db, monkeypatch):
    """Alerting steps on buckets, never on the raw score, or a watcher hears
    from us every fifteen minutes about a containment figure ticking along.

    These two inputs are chosen to score *differently* (50 and 46) inside the
    *same* band — which is the only way this test can tell the two rules apart.
    An earlier version used a 40%-to-41% nudge that happened to round to the
    same score either way, so it passed whichever rule was in force and was
    checking nothing at all.
    """
    loose = hazards.normalize_wfigs(_payload(
        _feature("a", "Sawtooth", 40.0, -122.0, acres=5_000, contained=0)))
    nudged = hazards.normalize_wfigs(_payload(
        _feature("a", "Sawtooth", 40.0, -122.0, acres=5_000, contained=15)))
    assert loose[0]["severity"] != nudged[0]["severity"], "the scores must differ"
    assert (hazards.bucket_of_score(loose[0]["severity"], "wildfire")
            == hazards.bucket_of_score(nudged[0]["severity"], "wildfire"))

    _run(monkeypatch, db, {"wfigs": loose})
    result = _run(monkeypatch, db, {"wfigs": nudged})

    assert result["updated"] == 0 and result["escalated"] == 0


# ---------- failure mode 1: nothing said is not nothing there ----------

def test_a_provider_that_could_not_answer_prunes_nothing(client, db, monkeypatch):
    rows = hazards.normalize_wfigs(_payload(_feature("a", "Sawtooth", 40.0, -122.0)))
    _run(monkeypatch, db, {"wfigs": rows})

    result = _run(monkeypatch, db, {"wfigs": None})

    assert result["ok"] is False
    assert db.query(Hazard).count() == 1, (
        "an unreachable provider emptied the table — the next good poll would "
        "then re-alert every watcher about every fire")


def test_an_empty_answer_is_treated_as_a_failure(client, db, monkeypatch):
    """A clean HTTP 200 carrying zero features is, in practice, far more often
    a renamed field or a silent rate-limit than a country with no fires."""
    rows = hazards.normalize_wfigs(_payload(_feature("a", "Sawtooth", 40.0, -122.0)))
    _run(monkeypatch, db, {"wfigs": rows})

    result = _run(monkeypatch, db, {"wfigs": []})

    assert result["ok"] is False
    assert db.query(Hazard).count() == 1


def test_a_provider_that_raises_does_not_break_the_poll(client, db, monkeypatch):
    async def boom(_client):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(hazards, "PROVIDERS", {"wfigs": boom})
    import asyncio
    result = asyncio.run(hazards.poll(db))

    assert result["ok"] is False


# ---------- retention ----------

def test_a_fire_the_feed_stopped_listing_ages_out(client, db, monkeypatch):
    from datetime import timedelta

    from backend.app.models import utcnow

    rows = hazards.normalize_wfigs(_payload(
        _feature("gone", "Old", 40.0, -122.0),
        _feature("here", "Current", 41.0, -121.0)))
    _run(monkeypatch, db, {"wfigs": rows})

    stale = db.scalar(select(Hazard).where(Hazard.external_id == "gone"))
    stale.last_seen_at = utcnow() - timedelta(days=hazards.RETENTION_DAYS + 1)
    db.commit()

    still_listed = hazards.normalize_wfigs(_payload(
        _feature("here", "Current", 41.0, -121.0)))
    result = _run(monkeypatch, db, {"wfigs": still_listed})

    assert result["pruned"] == 1
    assert [h.external_id for h in db.scalars(select(Hazard))] == ["here"]


def test_the_table_has_a_ceiling(client, db, monkeypatch):
    """Only reachable through a provider bug — but the failure it prevents is
    not disk space. prune_to_fit only ever deletes *articles*, so a hazard
    table left to grow would quietly push news out of the archive."""
    monkeypatch.setattr(hazards, "MAX_HAZARDS", 3)
    rows = hazards.normalize_wfigs(_payload(*[
        _feature(f"f{i}", f"Fire {i}", 40.0 + i / 100, -122.0, acres=100 * (i + 1))
        for i in range(10)]))

    _run(monkeypatch, db, {"wfigs": rows})

    kept = db.scalars(select(Hazard).order_by(Hazard.severity.desc())).all()
    assert len(kept) == 3
    assert kept[0].external_id == "f9", "the worst ones are what a map is for"


# ---------- the endpoint ----------

def _seed(db, *specs):
    from backend.app.models import utcnow
    for external_id, lat, lon, severity in specs:
        db.add(Hazard(kind="wildfire", provider="wfigs", external_id=external_id,
                      name=external_id, lat=lat, lon=lon, country="US",
                      severity=severity, raw={}, first_seen_at=utcnow(),
                      updated_at=utcnow(), last_seen_at=utcnow()))
    db.commit()


def test_the_map_asks_for_a_rectangle(client, register, db):
    headers = register("hazreader")
    _seed(db, ("inside", 40.0, -122.0, 50), ("far", 10.0, 20.0, 90))

    got = client.get("/api/hazards?bbox=-130,30,-100,52", headers=headers).json()

    assert [h["name"] for h in got] == ["inside"]


def test_the_worst_come_first(client, register, db):
    headers = register("hazreader")
    _seed(db, ("small", 40.0, -122.0, 10), ("big", 40.1, -122.1, 90),
          ("middling", 40.2, -122.2, 50))

    got = client.get("/api/hazards", headers=headers).json()

    assert [h["name"] for h in got] == ["big", "middling", "small"]


def test_a_bbox_past_the_antimeridian_is_clamped(client, register, db):
    """The map sets worldCopyJump, so panning west of the date line hands back
    longitudes beyond -180. Trusting them matches nothing and the layer looks
    broken."""
    headers = register("hazreader")
    _seed(db, ("inside", 40.0, -122.0, 50))

    got = client.get("/api/hazards?bbox=-540,-90,540,90", headers=headers).json()

    assert [h["name"] for h in got] == ["inside"]


def test_a_malformed_bbox_says_so(client, register, db):
    headers = register("hazreader")
    r = client.get("/api/hazards?bbox=nonsense", headers=headers)
    assert r.status_code == 422


def test_hazards_need_an_account_like_everything_else(client, db):
    assert client.get("/api/hazards").status_code == 401


# ---------- the switch ----------

def test_it_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("NEWS_HAZARDS", raising=False)
    assert hazards.enabled() is False


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("on", True),
    ("0", False), ("off", False), ("", False),
])
def test_the_switch_reads_the_usual_words(monkeypatch, value, expected):
    monkeypatch.setenv("NEWS_HAZARDS", value)
    assert hazards.enabled() is expected
