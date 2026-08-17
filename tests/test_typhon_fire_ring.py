"""The fire ring: told once, told again only if it got worse.

A watched place can carry a distance, and a wildfire that turns up inside it
says so. The whole difficulty is that the federal feed is **re-read wholesale
every fifteen minutes** — the same three hundred incidents, over and over — so
"is this fire inside the ring" is true of the same fires forever and is not on
its own a reason to say anything. Left naive, this notifies every watcher about
every nearby fire four times an hour until the fire goes out.

Two guards stop that, and they are what most of this file is about:

  · `UniqueConstraint(location_id, hazard_id)` on HazardHit — the first
    sighting is the only sighting.
  · `severity_at_alert` on that row — a fire that climbs a band earns a second
    word; one merely still burning does not.

Plus a floor: the smallest band never notifies. A 50 km ring in California
during fire season would otherwise carry several a day, nearly all quarter-acre
roadside starts that are out by evening, and a channel that cries wolf daily is
one nobody reads on the day it matters.

Air quality has no ring and is never notified — see test_typhon_air_reading.py
for what happens to it instead.
"""
import asyncio

import pytest
from sqlalchemy import select

from backend.app import hazards
from backend.app.models import FavoriteLocation, Hazard, HazardHit

BASE_LAT, BASE_LON = 40.5865, -122.3917


def _km_north(km):
    return BASE_LAT + km / 111.0


def _place(db, user_id="acct:1", name="Home", fire_km=75.0, fire_email=False,
           lat=BASE_LAT, lon=BASE_LON):
    loc = FavoriteLocation(user_id=user_id, name=name, lat=lat, lon=lon,
                           radius_km=25.0, fire_km=fire_km, fire_email=fire_email)
    db.add(loc)
    db.commit()
    return loc


def _fire(external_id="f1", km_away=10.0, acres=5_000.0, contained=0.0):
    return {
        "kind": "wildfire", "provider": "wfigs", "external_id": external_id,
        "name": f"{external_id} Fire", "lat": _km_north(km_away), "lon": BASE_LON,
        "country": "US",
        "severity": hazards._wildfire_severity(acres, contained),
        "started_at": None,
        "raw": {"acres": acres, "containment": contained},
    }


def _poll(monkeypatch, db, rows):
    async def fake(_client):
        return rows

    monkeypatch.setattr(hazards, "PROVIDERS", {"wfigs": fake})
    return asyncio.run(hazards.poll(db))


# ---------- it fires ----------

def test_a_fire_inside_the_ring_is_reported(client, db, monkeypatch):
    loc = _place(db)
    result = _poll(monkeypatch, db, [_fire(km_away=20)])

    assert result["hits"] == 1
    hit = db.scalar(select(HazardHit))
    assert hit.location_id == loc.id
    assert 19 <= hit.distance_km <= 21


def test_a_fire_outside_the_ring_is_not(client, db, monkeypatch):
    _place(db, fire_km=50)
    result = _poll(monkeypatch, db, [_fire(km_away=120)])

    assert result["hits"] == 0
    assert db.query(HazardHit).count() == 0


def test_a_place_with_no_ring_is_never_considered(client, db, monkeypatch):
    """Zero is off, and off is the default — nobody is opted in."""
    _place(db, fire_km=0)
    result = _poll(monkeypatch, db, [_fire(km_away=2)])

    assert result["hits"] == 0


def test_the_smallest_fires_do_not_notify(client, db, monkeypatch):
    """A quarter-acre roadside start is not worth interrupting anybody for."""
    _place(db)
    result = _poll(monkeypatch, db, [_fire(acres=2, km_away=5)])

    assert result["hits"] == 0
    assert db.query(Hazard).count() == 1, "it is still drawn on the map"


# ---------- and then it stops ----------

def test_the_same_fire_next_poll_says_nothing(client, db, monkeypatch):
    """The guard the whole feature turns on. Without it this is four
    notifications an hour for as long as the fire burns."""
    _place(db)
    rows = [_fire(km_away=20)]

    first = _poll(monkeypatch, db, rows)
    second = _poll(monkeypatch, db, rows)
    third = _poll(monkeypatch, db, rows)

    assert first["hits"] == 1
    assert second["hits"] == 0 and third["hits"] == 0
    assert db.query(HazardHit).count() == 1


def test_a_fire_that_grows_a_band_speaks_again(client, db, monkeypatch):
    _place(db)
    _poll(monkeypatch, db, [_fire(acres=1_200, km_away=20)])
    result = _poll(monkeypatch, db, [_fire(acres=60_000, km_away=20)])

    assert result["hits"] == 1
    assert db.query(HazardHit).count() == 1, "the same fire, not a second one"
    hit = db.scalar(select(HazardHit))
    assert hit.severity_at_alert == hazards._wildfire_severity(60_000, 0)


def test_a_fire_being_brought_under_control_does_not(client, db, monkeypatch):
    _place(db)
    _poll(monkeypatch, db, [_fire(acres=60_000, contained=0, km_away=20)])
    result = _poll(monkeypatch, db, [_fire(acres=60_000, contained=90, km_away=20)])

    assert result["hits"] == 0


def test_a_fire_climbing_back_below_its_own_peak_stays_quiet(client, db, monkeypatch):
    """The case `severity_at_alert` exists for, and the only one that reaches
    it — found by mutation-testing, because removing that check broke nothing
    until this test was written.

    `_upsert` already narrows to new-or-escalated, which covers the ordinary
    "same fire, same size" repeat. What it cannot see is history: a fire that
    peaks at band 4, is beaten down to band 2, and then climbs to band 3 *is*
    an escalation from the last poll's point of view. But the reader was
    already told about it at band 4, and being told again about something
    smaller than what they were warned of is a false alarm.
    """
    _place(db)
    peak = _fire(acres=150_000, km_away=20)      # band 4
    ebb = _fire(acres=5_000, km_away=20)         # band 2, a de-escalation
    partial = _fire(acres=25_000, km_away=20)    # band 3 — up, but below peak

    first = _poll(monkeypatch, db, [peak])
    assert first["hits"] == 1

    _poll(monkeypatch, db, [ebb])                # quiet: going down is not news
    result = _poll(monkeypatch, db, [partial])   # up, but not past what we said

    assert result["hits"] == 0
    hit = db.scalar(select(HazardHit))
    assert hit.severity_at_alert == peak["severity"], (
        "the record should still hold the worst we reported")


def test_climbing_past_its_own_peak_does_speak(client, db, monkeypatch):
    """The other half: the guard must not silence a fire that genuinely gets
    worse than anything the reader has been told."""
    _place(db)
    _poll(monkeypatch, db, [_fire(acres=5_000, km_away=20)])     # band 2
    _poll(monkeypatch, db, [_fire(acres=500, km_away=20)])       # down to band 1
    result = _poll(monkeypatch, db, [_fire(acres=150_000, km_away=20)])  # band 4

    assert result["hits"] == 1


def test_growing_within_one_band_does_not(client, db, monkeypatch):
    small = _fire(acres=1_200, km_away=20)
    bigger = _fire(acres=1_800, km_away=20)
    assert (hazards.bucket_of_score(small["severity"], "wildfire")
            == hazards.bucket_of_score(bigger["severity"], "wildfire"))
    _place(db)

    _poll(monkeypatch, db, [small])
    result = _poll(monkeypatch, db, [bigger])

    assert result["hits"] == 0


# ---------- who hears about it ----------

def test_two_places_near_one_fire_each_hear_once(client, db, monkeypatch):
    _place(db, name="Home")
    _place(db, name="Cabin", lat=_km_north(10))

    first = _poll(monkeypatch, db, [_fire(km_away=5)])
    second = _poll(monkeypatch, db, [_fire(km_away=5)])

    assert first["hits"] == 2
    assert second["hits"] == 0
    assert db.query(HazardHit).count() == 2


def test_the_hit_carries_what_the_message_needs(client, db, monkeypatch):
    """The dict returned here is what becomes the toast and the email, so it
    has to be complete — including the ownership fields, which are the only
    thing stopping one reader's stream from showing another's places."""
    loc = _place(db, name="Home", user_id="acct:7")
    rows = [_fire(acres=40_000, contained=10, km_away=20)]

    # Through poll, so the hazard is persisted and has an id — a HazardHit
    # pointing at nothing is the failure this test was written badly enough to
    # cause the first time.
    async def fake(_client):
        return rows

    monkeypatch.setattr(hazards, "PROVIDERS", {"wfigs": fake})
    captured = []
    real = hazards.evaluate_fire_ring
    monkeypatch.setattr(hazards, "evaluate_fire_ring",
                        lambda d, c: captured.extend(real(d, c)) or captured)
    asyncio.run(hazards.poll(db))

    assert len(captured) == 1
    hit = captured[0]
    assert hit["location_id"] == loc.id and hit["location_name"] == "Home"
    assert hit["user_id"] == "acct:7"
    assert hit["name"] == "f1 Fire"
    assert 19 <= hit["distance_km"] <= 21
    assert hit["acres"] == 40_000 and hit["containment"] == 10
    assert hit["again"] is False
    assert db.scalar(select(HazardHit)).hazard_id is not None


def test_email_only_goes_to_places_that_asked(client, db, monkeypatch):
    _place(db, name="Quiet", fire_email=False)
    sent = []
    monkeypatch.setattr(hazards.mailer, "enabled", lambda: True)
    monkeypatch.setattr(hazards.mailer, "send_hazard_digest",
                        lambda *a, **k: sent.append(a) or True)

    result = _poll(monkeypatch, db, [_fire(km_away=20)])

    assert result["hits"] == 1
    assert result["emailed"] == 0 and sent == []


def test_email_goes_to_places_that_did(client, db, monkeypatch, register, client_user=None):
    from backend.app.models import User
    user = User(username="fireowner", email="fire@example.com",
                password_hash="x", email_verified=True)
    db.add(user)
    db.commit()
    _place(db, user_id=f"acct:{user.id}", name="Home", fire_email=True)
    sent = []
    monkeypatch.setattr(hazards.mailer, "enabled", lambda: True)
    monkeypatch.setattr(hazards.mailer, "send_hazard_digest",
                        lambda *a, **k: sent.append(a) or True)

    result = _poll(monkeypatch, db, [_fire(km_away=20)])

    assert result["emailed"] == 1
    assert sent[0][0] == "fire@example.com"
    assert sent[0][1] == "Home"


def test_a_mail_failure_does_not_break_the_poll(client, db, monkeypatch):
    from backend.app.models import User
    user = User(username="fireowner", email="fire@example.com",
                password_hash="x", email_verified=True)
    db.add(user)
    db.commit()
    _place(db, user_id=f"acct:{user.id}", fire_email=True)

    def boom(*a, **k):
        raise RuntimeError("smtp is having an afternoon")

    monkeypatch.setattr(hazards.mailer, "enabled", lambda: True)
    monkeypatch.setattr(hazards.mailer, "send_hazard_digest", boom)

    result = _poll(monkeypatch, db, [_fire(km_away=20)])

    assert result["ok"] is True
    assert db.query(HazardHit).count() == 1, (
        "the hit is recorded even when the email is not sent")


# ---------- it reaches the reader in-app ----------

def test_every_hit_is_published_to_the_stream(client, db, monkeypatch):
    """In-app delivery is the half that always happens — email is opt-in and
    needs a mail server, but the toast is how anybody finds out at all."""
    published = []
    monkeypatch.setattr(hazards.broadcaster, "publish", published.append)
    _place(db, name="Home", user_id="acct:7")

    _poll(monkeypatch, db, [_fire(km_away=20)])

    hazard_msgs = [m for m in published if m.get("type") == "hazard"]
    assert len(hazard_msgs) == 1
    msg = hazard_msgs[0]
    # The two fields the client's ownership guard reads. Without them the
    # message either reaches nobody or reaches everybody.
    assert msg["user_id"] == "acct:7"
    assert "pantheon_id" in msg
    assert msg["location_name"] == "Home" and msg["name"] == "f1 Fire"
    assert msg["distance_km"] > 0


def test_a_quiet_poll_publishes_nothing(client, db, monkeypatch):
    published = []
    monkeypatch.setattr(hazards.broadcaster, "publish", published.append)
    _place(db)
    rows = [_fire(km_away=20)]

    _poll(monkeypatch, db, rows)
    published.clear()
    _poll(monkeypatch, db, rows)

    assert [m for m in published if m.get("type") == "hazard"] == []


# ---------- air is not part of this ----------

def test_air_quality_never_produces_a_hit(client, db, monkeypatch):
    """It has no ring by design. A continuous field with a value everywhere
    cannot be alerted on by proximity without saying 'the air is fine' a dozen
    times over."""
    _place(db)
    station = {
        "kind": "air_quality", "provider": "airnow", "external_id": "aq",
        "name": "Redding", "lat": _km_north(2), "lon": BASE_LON,
        "country": "US", "severity": 100, "started_at": None,
        "raw": {"aqi": 340, "category": "Hazardous"},
    }

    async def fake(_client):
        return [station]

    monkeypatch.setattr(hazards, "PROVIDERS", {"airnow": fake})
    result = asyncio.run(hazards.poll(db))

    assert result["hits"] == 0
    assert db.query(HazardHit).count() == 0
    assert db.query(Hazard).count() == 1, "it is still on the map"


# ---------- the distance is honest ----------

def test_the_distance_matches_the_geo_helper(client, db, monkeypatch):
    """Checked against the shared haversine rather than a reimplementation —
    Wildfire Command had five copies of this function and they did not all
    agree."""
    from backend.app.geo import haversine_km

    loc = _place(db)
    _poll(monkeypatch, db, [_fire(km_away=33)])

    hit = db.scalar(select(HazardHit))
    fire = db.scalar(select(Hazard))
    assert abs(hit.distance_km
               - haversine_km(loc.lat, loc.lon, fire.lat, fire.lon)) < 0.01
