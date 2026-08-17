"""The air at a watched place — a reading, not an alert.

Wildfires and air quality arrive through the same table and are treated
completely differently, and the reason is worth stating because it is the whole
design: a fire is a **discrete event** that either turns up near you or does
not, so "tell me when one appears within N km" is a well-posed question. Air
quality is a **continuous field** with a value everywhere, so "alert me about
air within N km" has no good answer — a ring around any US city holds a dozen
monitors nearly all reading Good, and every one of them would be a notification
saying nothing is wrong.

So air is never notified. It is a property of the place, and it is shown:

    stations within 5 km   → average their AQI, and say how many
    none that close        → the single nearest, and say how far away it is
    none within 100 km     → no reading at all

The provenance half is not decoration. An average of three monitors down the
road and one reading from forty kilometres away are different claims, and a
reader deciding whether to open a window is entitled to know which they have.
Each branch is tested because the rule *is* the feature.
"""
import pytest
from sqlalchemy import select

from backend.app.main import AIR_MAX_KM, AIR_NEAR_KM, air_readings
from backend.app.models import FavoriteLocation, Hazard, utcnow

# Reading, CA. Every offset below is measured from here.
BASE_LAT, BASE_LON = 40.5865, -122.3917


def _station(db, name, lat, lon, aqi):
    db.add(Hazard(kind="air_quality", provider="airnow", external_id=name,
                  name=name, lat=lat, lon=lon, country="US",
                  severity=10, raw={"aqi": aqi, "category": "x"},
                  first_seen_at=utcnow(), updated_at=utcnow(),
                  last_seen_at=utcnow()))
    db.commit()


def _place(db, user_id="acct:1", name="Home", lat=BASE_LAT, lon=BASE_LON):
    loc = FavoriteLocation(user_id=user_id, name=name, lat=lat, lon=lon,
                           radius_km=25.0)
    db.add(loc)
    db.commit()
    return loc


def _km_north(km):
    """A latitude that many kilometres north of the base point."""
    return BASE_LAT + km / 111.0


# ---------- within 5 km: average, and say how many ----------

def test_several_close_stations_are_averaged(client, db):
    loc = _place(db)
    _station(db, "a", _km_north(1), BASE_LON, 30)
    _station(db, "b", _km_north(2), BASE_LON, 60)
    _station(db, "c", _km_north(3), BASE_LON, 90)

    air = air_readings(db, [loc])[loc.id]

    assert air["aqi"] == 60, "the mean of 30, 60 and 90"
    assert air["basis"] == "average"
    assert air["stations"] == 3
    assert air["within_km"] == AIR_NEAR_KM


def test_the_category_follows_the_average_not_any_one_station(client, db):
    """Two Good stations and one Unhealthy average to Moderate. Reporting the
    worst station's label next to the averaged number would be two different
    claims in one sentence."""
    loc = _place(db)
    _station(db, "a", _km_north(1), BASE_LON, 20)
    _station(db, "b", _km_north(2), BASE_LON, 30)
    _station(db, "c", _km_north(3), BASE_LON, 160)

    air = air_readings(db, [loc])[loc.id]

    assert air["aqi"] == 70
    assert air["category"] == "Moderate"


def test_one_close_station_is_still_an_average_of_one(client, db):
    """It is inside the near ring, so it speaks for the place. Reporting it as
    "nearest station, 2 km away" would imply we had to reach for it."""
    loc = _place(db)
    _station(db, "a", _km_north(2), BASE_LON, 44)

    air = air_readings(db, [loc])[loc.id]

    assert air["basis"] == "average" and air["stations"] == 1
    assert air["aqi"] == 44


def test_a_station_just_outside_the_ring_is_not_averaged_in(client, db):
    loc = _place(db)
    _station(db, "in", _km_north(4), BASE_LON, 40)
    _station(db, "out", _km_north(AIR_NEAR_KM + 3), BASE_LON, 200)

    air = air_readings(db, [loc])[loc.id]

    assert air["stations"] == 1 and air["aqi"] == 40


# ---------- outside 5 km: nearest, and say how far ----------

def test_with_nothing_close_the_nearest_is_used_and_its_distance_stated(client, db):
    loc = _place(db)
    _station(db, "far", _km_north(40), BASE_LON, 128)
    _station(db, "further", _km_north(80), BASE_LON, 20)

    air = air_readings(db, [loc])[loc.id]

    assert air["basis"] == "nearest"
    assert air["aqi"] == 128, "the nearest, not the best-looking"
    assert air["station"] == "far"
    assert 39 <= air["distance_km"] <= 41


def test_a_reading_from_too_far_away_is_no_reading_at_all(client, db):
    """A monitor four hundred kilometres away says nothing about your air, and
    a confident wrong number is worse than a blank."""
    loc = _place(db)
    _station(db, "distant", _km_north(AIR_MAX_KM + 50), BASE_LON, 300)

    assert air_readings(db, [loc]) == {}


def test_no_stations_at_all_is_no_reading(client, db):
    loc = _place(db)
    assert air_readings(db, [loc]) == {}


def test_a_fire_is_never_mistaken_for_a_monitor(client, db):
    """Both kinds live in one table; only air quality speaks for the air."""
    loc = _place(db)
    db.add(Hazard(kind="wildfire", provider="wfigs", external_id="f",
                  name="Sawtooth", lat=_km_north(1), lon=BASE_LON, country="US",
                  severity=90, raw={"acres": 5000}, first_seen_at=utcnow(),
                  updated_at=utcnow(), last_seen_at=utcnow()))
    db.commit()

    assert air_readings(db, [loc]) == {}


# ---------- several places at once ----------

def test_each_place_gets_its_own_answer(client, db):
    here = _place(db, name="Home")
    away = _place(db, name="Cabin", lat=_km_north(300), lon=BASE_LON)
    _station(db, "near-home", _km_north(2), BASE_LON, 40)
    _station(db, "near-cabin", _km_north(301), BASE_LON, 180)

    air = air_readings(db, [here, away])

    assert air[here.id]["aqi"] == 40
    assert air[away.id]["aqi"] == 180


def test_no_places_costs_nothing(client, db):
    assert air_readings(db, []) == {}


# ---------- and it reaches the client ----------

def test_the_api_carries_the_reading(client, register, db):
    headers = register("airreader")
    loc = client.post("/api/locations", headers=headers, json={
        "name": "Home", "place_name": "Reading", "country": "US",
        "lat": BASE_LAT, "lon": BASE_LON, "radius_km": 25,
    }).json()
    _station(db, "close", _km_north(2), BASE_LON, 55)

    got = client.get("/api/locations", headers=headers).json()

    assert got[0]["id"] == loc["id"]
    assert got[0]["air"]["aqi"] == 55
    assert got[0]["air"]["basis"] == "average"


def test_a_place_with_no_monitor_nearby_reports_air_as_absent(client, register):
    """Explicitly null rather than missing, so the client can tell "we looked
    and there is nothing" from "this build does not know about air"."""
    headers = register("airreader")
    client.post("/api/locations", headers=headers, json={
        "name": "Nowhere", "lat": -40.0, "lon": 170.0, "radius_km": 25,
    })

    got = client.get("/api/locations", headers=headers).json()

    assert "air" in got[0] and got[0]["air"] is None


def test_the_stations_are_read_once_however_many_places(client, register, db):
    """The reading is derived per request. Doing it per location would be a
    query per place on a page that lists them all."""
    from sqlalchemy import event

    from backend.app.database import engine

    headers = register("airreader")
    for n in range(4):
        client.post("/api/locations", headers=headers, json={
            "name": f"Place {n}", "lat": BASE_LAT + n, "lon": BASE_LON,
            "radius_km": 25, "create_feed": False,
        })
    _station(db, "s", _km_north(2), BASE_LON, 42)

    seen = []
    rec = lambda c, cur, stmt, p, ctx, many: seen.append(stmt)  # noqa: E731
    event.listen(engine, "before_cursor_execute", rec)
    try:
        client.get("/api/locations", headers=headers)
    finally:
        event.remove(engine, "before_cursor_execute", rec)

    hazard_queries = [s for s in seen if "FROM hazards" in s]
    assert len(hazard_queries) == 1, hazard_queries
