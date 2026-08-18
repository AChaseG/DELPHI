"""PurpleAir: the density the Fire and Smoke Map has, and the caveats it costs.

The comment above AirNow in `hazards.py` has said for a while that our air
layer is thinner than the EPA's own Fire and Smoke Map because that map also
draws tens of thousands of PurpleAir consumer sensors, and no argument to
airnowapi.org will ever return one. This is the other half of that sentence.

What it is not: a second opinion beside a regulatory monitor. A PurpleAir unit
is a light-scattering sensor on somebody's fence. It reads high in humidity, it
can be pointed at a barbecue, and its two channels can disagree. So three rules
hold this file up:

  · **the raw number is never published** — the EPA's correction is applied,
    because the raw number is wrong in a known direction;
  · **a regulatory monitor in range always wins** — the crowd's value is
    coverage *between* good instruments, not a rival reading next to one;
  · **every row says what it is**, in the pane and in the popup.
"""
import pathlib

import pytest

from backend.app import hazards, main
from backend.app.models import FavoriteLocation, Hazard

FRONTEND = pathlib.Path(__file__).resolve().parent.parent / "frontend"
APP = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")


def _fn(name: str, end: str = "\n}\n") -> str:
    start = APP.index(name)
    return APP[start:APP.find(end, start)]


# ---------- the correction ----------

def test_the_raw_reading_is_never_what_is_published():
    """A light-scattering sensor over-reads, and more so in damp air. Publishing
    the raw figure would put a clear humid morning into Moderate on nothing but
    the weather — the single most common way consumer air data misleads."""
    assert hazards.purpleair_correct(10, 50) < 10


def test_humidity_pulls_the_number_down():
    dry = hazards.purpleair_correct(20, 10)
    damp = hazards.purpleair_correct(20, 90)
    assert damp < dry


def test_the_correction_is_continuous_across_its_seams():
    """It is piecewise, and a step at a boundary would be a sensor jumping a
    category because a number crossed 30."""
    for edge in (30, 50, 210, 260):
        below = hazards.purpleair_correct(edge - 0.01, 50)
        above = hazards.purpleair_correct(edge + 0.01, 50)
        assert abs(above - below) < 1.0, f"discontinuity at {edge}"


def test_heavy_smoke_is_handled_rather_than_extrapolated():
    """The low-range slope run out to 400 µg/m³ would understate a genuinely
    dangerous day, which is the one it matters most to get right."""
    assert hazards.purpleair_correct(400, 50) > hazards.purpleair_correct(260, 50)
    assert hazards.purpleair_correct(400, 50) > 300


def test_it_never_goes_negative():
    """The humidity term alone can take a very clean reading below zero, and a
    negative concentration is not a thing."""
    assert hazards.purpleair_correct(0, 100) == 0.0


# ---------- reading the table ----------

def _payload(rows, fields=None):
    fields = fields or ["sensor_index", "name", "latitude", "longitude",
                        "pm2.5_cf_1", "humidity", "confidence", "location_type"]
    return {"fields": fields, "data": rows}


def _sensor(idx=1, lat=47.6, lon=-122.3, pm=20.0, rh=50, conf=100, indoors=0):
    return [idx, f"Sensor {idx}", lat, lon, pm, rh, conf, indoors]


def test_a_sensor_becomes_a_station():
    rows = hazards.normalize_purpleair(_payload([_sensor()]))
    assert len(rows) == 1
    assert rows[0]["kind"] == "air_quality"
    assert rows[0]["provider"] == "purpleair"
    assert rows[0]["raw"]["low_cost"] is True
    assert rows[0]["raw"]["estimated"] is True


def test_the_column_order_is_read_not_assumed():
    """The response is a header row and then bare arrays. Assuming the order is
    how a provider reordering its output turns latitude into humidity without
    anything raising."""
    shuffled = ["sensor_index", "confidence", "pm2.5_cf_1", "longitude",
                "latitude", "humidity", "name", "location_type"]
    rows = hazards.normalize_purpleair(_payload(
        [[7, 100, 20.0, -122.3, 47.6, 50, "Backwards", 0]], fields=shuffled))
    assert len(rows) == 1
    assert rows[0]["lat"] == pytest.approx(47.6)
    assert rows[0]["lon"] == pytest.approx(-122.3)
    assert rows[0]["name"] == "Backwards"


def test_a_table_missing_a_column_it_needs_is_a_failure():
    assert hazards.normalize_purpleair(
        _payload([[1, 47.6]], fields=["sensor_index", "latitude"])) is None
    assert hazards.normalize_purpleair("not a dict") is None
    assert hazards.normalize_purpleair({"fields": "no", "data": []}) is None


def test_a_sensor_the_network_itself_doubts_is_dropped():
    """Confidence is mostly whether the unit's two channels agree. EPA excludes
    disagreeing sensors from the Fire and Smoke Map, and so does this."""
    assert hazards.normalize_purpleair(_payload([_sensor(conf=30)])) == []
    assert len(hazards.normalize_purpleair(_payload([_sensor(conf=90)]))) == 1


def test_an_indoor_sensor_is_dropped():
    """It is measuring somebody's kitchen and has nothing to say about the air
    outside their house."""
    assert hazards.normalize_purpleair(_payload([_sensor(indoors=1)])) == []


def test_the_reading_carries_the_raw_number_alongside_the_corrected_one():
    """So the popup can show its working. A correction nobody can see is a
    number the reader has to take entirely on faith."""
    raw = hazards.normalize_purpleair(_payload([_sensor(pm=100.0, rh=60)]))[0]["raw"]
    assert raw["raw_value"] == 100.0
    assert raw["value"] < raw["raw_value"]
    assert raw["humidity"] == 60


def test_the_cap_keeps_the_worst_not_the_first(monkeypatch):
    """Thirty thousand sensors would crowd every other hazard out of the table,
    and the entire reason anybody switches on a smoke layer is to find the
    smoke."""
    monkeypatch.setattr(hazards, "PURPLEAIR_MAX", 2)
    rows = hazards.normalize_purpleair(_payload([
        _sensor(1, pm=5.0), _sensor(2, pm=300.0), _sensor(3, pm=150.0),
        _sensor(4, pm=8.0)]))
    assert len(rows) == 2
    assert [r["raw"]["aqi"] for r in rows] == sorted(
        [r["raw"]["aqi"] for r in rows], reverse=True)
    assert rows[0]["raw"]["raw_value"] == 300.0


def test_no_key_means_no_provider_not_a_failure(monkeypatch):
    import asyncio
    monkeypatch.delenv("PURPLEAIR_API_KEY", raising=False)
    assert asyncio.run(hazards.fetch_purpleair(None)) is None


def test_a_bad_bounding_box_fails_loudly_rather_than_fetching_the_world(monkeypatch):
    import asyncio
    monkeypatch.setenv("PURPLEAIR_API_KEY", "sk-yes")
    monkeypatch.setattr(hazards, "PURPLEAIR_BBOX", "not,a,box")
    assert asyncio.run(hazards.fetch_purpleair(None)) is None


def test_the_query_asks_only_for_outdoor_and_recent(monkeypatch):
    import inspect
    src = inspect.getsource(hazards.fetch_purpleair)
    assert '"location_type": "0"' in src
    assert "max_age" in src
    assert "X-API-Key" in src


def test_purpleair_is_a_provider():
    assert "purpleair" in hazards.PROVIDERS


def test_its_key_is_named_in_the_status(monkeypatch):
    monkeypatch.delenv("PURPLEAIR_API_KEY", raising=False)
    assert "purpleair" in hazards.idle_status("x")["no_key"]
    monkeypatch.setenv("PURPLEAIR_API_KEY", "sk-yes")
    assert "purpleair" not in hazards.idle_status("x")["no_key"]


# ---------- a good instrument always wins ----------

def _station(db, provider, lat, lon, aqi, low_cost=False):
    db.add(Hazard(kind="air_quality", provider=provider,
                  external_id=f"{provider}:{lat},{lon}", name=f"{provider} site",
                  lat=lat, lon=lon, severity=40,
                  raw={"aqi": aqi, "estimated": low_cost, "low_cost": low_cost,
                       "parameter": "PM2.5", "value": 12.0, "unit": "µg/m³"}))
    db.commit()


def _place(db, lat=47.6, lon=-122.3):
    loc = FavoriteLocation(user_id="u", name="Home", lat=lat, lon=lon)
    db.add(loc)
    db.commit()
    return loc


def test_a_regulatory_monitor_beats_a_nearer_community_sensor(db):
    """The load-bearing rule. A consumer sensor at the end of the road is not a
    better answer than a reference monitor two kilometres away, and averaging
    the two would produce a figure neither of them made."""
    loc = _place(db)
    _station(db, "purpleair", 47.6005, -122.3005, 180, low_cost=True)
    _station(db, "airnow", 47.63, -122.34, 40)
    air = main.air_readings(db, [loc])[loc.id]
    assert air["provider"] == "airnow"
    assert air["low_cost"] is False


def test_openaq_also_outranks_a_community_sensor(db):
    loc = _place(db, 48.85, 2.35)
    _station(db, "purpleair", 48.8501, 2.3501, 180, low_cost=True)
    _station(db, "openaq", 48.86, 2.36, 40, low_cost=False)
    assert main.air_readings(db, [loc])[loc.id]["provider"] == "openaq"


def test_a_community_sensor_speaks_when_nothing_better_is_in_range(db):
    """Which is the whole point of adding it: coverage between the good
    instruments, in the places they do not reach."""
    loc = _place(db)
    _station(db, "purpleair", 47.6005, -122.3005, 180, low_cost=True)
    air = main.air_readings(db, [loc])[loc.id]
    assert air["provider"] == "purpleair"
    assert air["low_cost"] is True


def test_community_sensors_still_average_among_themselves(db):
    loc = _place(db)
    _station(db, "purpleair", 47.6005, -122.3005, 100, low_cost=True)
    _station(db, "purpleair", 47.6008, -122.3008, 140, low_cost=True)
    air = main.air_readings(db, [loc])[loc.id]
    assert air["stations"] == 2
    assert air["aqi"] == 120


def test_scales_are_still_never_mixed(db):
    loc = _place(db)
    _station(db, "airnow", 47.6005, -122.3005, 40)
    _station(db, "purpleair", 47.6008, -122.3008, 180, low_cost=True)
    air = main.air_readings(db, [loc])[loc.id]
    assert air["stations"] == 1
    assert air["aqi"] == 40


def test_the_ranking_has_exactly_two_tiers():
    """The only real distinction is reference monitor against consumer sensor.
    Ordering AirNow above OpenAQ would rank the aggregators rather than the
    instruments — and since they never cover the same ground anyway, it would
    only decide border cases, where the honest answer is whichever monitor is
    actually nearer."""
    rank = hazards.AIR_PROVIDER_RANK
    assert rank["airnow"] == rank["openaq"] < rank["purpleair"]


# ---------- and it says what it is ----------

def test_the_pane_row_calls_it_a_community_sensor():
    src = _fn("function airLine")
    assert "community sensor" in src
    assert "air.low_cost" in src


def test_the_popup_shows_its_working():
    """A correction nobody can see is a number taken entirely on faith."""
    src = _fn("function hazardPopup")
    assert "raw.raw_value" in src and "raw.humidity" in src
    assert "not a regulatory monitor" in src


def test_a_missing_purpleair_key_is_not_reported_as_a_hole_in_the_map():
    """It adds density where the regulatory networks are thin; its absence is
    not something a reader can see, and naming every optional key in the map
    note would bury the two that matter."""
    note = _fn("  hazardNote()", "\n  },\n")
    assert "purpleair" not in note
    status = _fn("function renderHazardStatus")
    assert "PURPLEAIR_API_KEY" in status, (
        "an operator should still be told, in the place operators look")
