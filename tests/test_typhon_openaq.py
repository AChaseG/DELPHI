"""Air quality for the rest of the world, and the honesty it costs.

AirNow is the EPA and the EPA stops at the border, so Typhon's air layer was
blank for most of Delphi's readers. OpenAQ fixes that: government and research
monitors worldwide, a free key, and — the thing that decided it over WAQI —
terms that permit storing the data and serving it on, which is exactly what
`hazards.poll()` does by construction.

It costs one thing, and this file is mostly about paying it honestly.

**AirNow hands back an index. OpenAQ hands back a measurement.** 18.4 µg/m³,
with a unit attached, and units are never converted at the source. The EPA's
breakpoint table turns that into a category — but that table is defined over a
24-hour average for particulates and an 8-hour average for ozone, and one
hourly reading is neither. So the category is computed and shown, and it is
labelled as an estimate from one reading rather than as an AQI, everywhere the
number appears.

The two rules that hold it together:

  · **a derived category must never be presented as a published AQI**, and
  · **the two scales must never be averaged together** — AirNow owns the
    United States, OpenAQ owns everywhere else.
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


# ---------- the breakpoint table ----------
#
# Reproducing EPA's own worked examples exactly. If these drift, every reading
# outside the United States is quietly in the wrong band.

@pytest.mark.parametrize("parameter,value,unit,expected", [
    ("pm25", 35.9, "ug/m3", 102),      # EPA's published worked example
    ("pm25", 9.0, "ug/m3", 50),        # top of Good, 2024 revision
    ("pm25", 9.1, "ug/m3", 51),        # bottom of Moderate
    ("pm25", 0.0, "ug/m3", 0),
    ("pm10", 54, "ug/m3", 50),
    ("pm10", 55, "ug/m3", 51),
    ("o3", 0.078, "ppm", 126),
    ("o3", 0.054, "ppm", 50),
    ("co", 8.4, "ppm", 90),
    ("no2", 100, "ppb", 100),
    ("so2", 35, "ppb", 50),
])
def test_the_table_reproduces_the_published_numbers(parameter, value, unit, expected):
    assert hazards.concentration_to_aqi(parameter, value, unit) == expected


def test_the_concentration_is_truncated_the_way_the_standard_says():
    """Truncation is part of the method, not a rounding preference — 9.09
    µg/m³ is Good and 9.1 is Moderate, and rounding first would move the
    boundary for everybody sitting on it."""
    assert hazards.concentration_to_aqi("pm25", 9.09, "ug/m3") == 50
    assert hazards.concentration_to_aqi("pm25", 9.1, "ug/m3") == 51


def test_the_same_reading_in_two_units_lands_in_the_same_place():
    """The assertion that proves units are honoured rather than assumed. Get
    this wrong and a value is out by a factor of a thousand while looking
    entirely plausible."""
    assert (hazards.concentration_to_aqi("o3", 0.078, "ppm")
            == hazards.concentration_to_aqi("o3", 78, "ppb"))
    assert (hazards.concentration_to_aqi("co", 8.4, "ppm")
            == hazards.concentration_to_aqi("co", 8400, "ppb"))


def test_a_gas_reported_as_a_mass_is_converted_through_its_molar_mass():
    """µg/m³ to ppb is not a decimal shift — it needs the molecule. Ozone at
    100 µg/m³ is about 51 ppb at EPA's reference conditions."""
    ppb = hazards.convert_unit("o3", 100, "ug/m3") * 1000
    assert ppb == pytest.approx(50.9, abs=0.5)


def test_a_unit_that_cannot_be_converted_is_refused_rather_than_guessed():
    """Turning a volume fraction into a particulate mass needs a density this
    does not have, and inventing one produces a confident wrong number."""
    assert hazards.concentration_to_aqi("pm25", 5, "ppb") is None
    assert hazards.concentration_to_aqi("pm25", 5, "") is None
    assert hazards.concentration_to_aqi("nonsense", 5, "ug/m3") is None
    assert hazards.concentration_to_aqi("pm25", None, "ug/m3") is None
    assert hazards.concentration_to_aqi("pm25", -3, "ug/m3") is None


def test_a_value_above_the_table_is_pinned_rather_than_extrapolated():
    """The table stops. Beyond it there is no published index, so inventing one
    by continuing the line would be printing a figure nobody defines."""
    assert hazards.concentration_to_aqi("pm25", 9999, "ug/m3") == 500


def test_micro_sign_variants_are_all_the_same_unit():
    for unit in ("µg/m³", "ug/m3", "μg/m3", "UG/M3"):
        assert hazards.concentration_to_aqi("pm25", 35.9, unit) == 102


# ---------- reading the two payloads ----------

def _location(loc_id=1, country="FR", lat=48.85, lon=2.35, sensors=None):
    return {"id": loc_id, "name": f"Station {loc_id}",
            "country": {"code": country}, "provider": {"name": "EEA"},
            "coordinates": {"latitude": lat, "longitude": lon},
            "sensors": sensors if sensors is not None else [
                {"id": loc_id * 10, "parameter": {"name": "pm25", "units": "µg/m³"}}]}


def _latest(sensor_id, value):
    return {"sensorsId": sensor_id, "value": value,
            "datetime": {"utc": "2026-08-17T12:00:00Z"}}


def test_a_station_and_its_reading_are_joined_on_the_sensor(monkeypatch):
    monkeypatch.delenv("AIRNOW_API_KEY", raising=False)
    rows = hazards.normalize_openaq([_location()], {"pm25": [_latest(10, 35.9)]})
    assert len(rows) == 1
    assert rows[0]["kind"] == "air_quality"
    assert rows[0]["provider"] == "openaq"
    assert rows[0]["raw"]["aqi"] == 102
    assert rows[0]["raw"]["category"] == "Unhealthy for Sensitive Groups"


def test_every_row_says_it_is_an_estimate(monkeypatch):
    """The flag the whole provider turns on. Without it the client cannot tell
    a published index from our reading of one hourly measurement."""
    monkeypatch.delenv("AIRNOW_API_KEY", raising=False)
    rows = hazards.normalize_openaq([_location()], {"pm25": [_latest(10, 12)]})
    assert rows[0]["raw"]["estimated"] is True
    assert rows[0]["raw"]["unit"]
    assert rows[0]["raw"]["value"] == 12


def test_a_reading_with_no_matching_sensor_is_dropped(monkeypatch):
    monkeypatch.delenv("AIRNOW_API_KEY", raising=False)
    assert hazards.normalize_openaq([_location()], {"pm25": [_latest(999, 40)]}) == []


def test_a_station_reports_its_worst_pollutant_not_all_of_them(monkeypatch):
    """One station is one dot. Drawing every pollutant would stack them and put
    the worst underneath — and the EPA index is a maximum across pollutants
    anyway, not an average."""
    monkeypatch.delenv("AIRNOW_API_KEY", raising=False)
    loc = _location(sensors=[
        {"id": 10, "parameter": {"name": "pm25", "units": "µg/m³"}},
        {"id": 11, "parameter": {"name": "o3", "units": "ppm"}},
    ])
    rows = hazards.normalize_openaq(
        [loc], {"pm25": [_latest(10, 5.0)], "o3": [_latest(11, 0.078)]})
    assert len(rows) == 1
    assert rows[0]["raw"]["parameter"] == "O3"
    assert rows[0]["raw"]["aqi"] == 126


def test_a_pollutant_with_no_published_scale_is_left_alone(monkeypatch):
    """Real data we have no honest way to colour. Inventing a scale for it
    would be worse than not drawing it."""
    monkeypatch.delenv("AIRNOW_API_KEY", raising=False)
    loc = _location(sensors=[{"id": 10, "parameter": {"name": "bc", "units": "µg/m³"}}])
    assert hazards.normalize_openaq([loc], {"bc": [_latest(10, 4)]}) == []


@pytest.mark.parametrize("bad", [
    {"id": 1, "coordinates": {"latitude": None, "longitude": 2.0}},
    {"id": 2, "coordinates": {"latitude": 200.0, "longitude": 2.0}},
    {"coordinates": {"latitude": 1.0, "longitude": 2.0}},
    "not a dict",
])
def test_a_station_without_a_usable_point_is_dropped(bad, monkeypatch):
    monkeypatch.delenv("AIRNOW_API_KEY", raising=False)
    assert hazards.normalize_openaq([bad], {"pm25": [_latest(10, 5)]}) == []


def test_a_malformed_payload_is_a_failure_not_an_empty_world():
    assert hazards.normalize_openaq("nope", {}) is None
    assert hazards.normalize_openaq([], "nope") is None


# ---------- AirNow owns the United States ----------

def test_us_stations_are_dropped_while_airnow_has_a_key(monkeypatch):
    monkeypatch.setenv("AIRNOW_API_KEY", "sk-yes")
    rows = hazards.normalize_openaq([_location(country="US", lat=47.6, lon=-122.3)],
                                    {"pm25": [_latest(10, 20)]})
    assert rows == [], "two scales must never both cover the same place"


def test_us_stations_are_kept_when_airnow_is_unconfigured(monkeypatch):
    """Strictly better than a blank map: an estimate is more use than nothing,
    and it says it is an estimate."""
    monkeypatch.delenv("AIRNOW_API_KEY", raising=False)
    rows = hazards.normalize_openaq([_location(country="US", lat=47.6, lon=-122.3)],
                                    {"pm25": [_latest(10, 20)]})
    assert len(rows) == 1


def test_the_rest_of_the_world_is_never_dropped(monkeypatch):
    monkeypatch.setenv("AIRNOW_API_KEY", "sk-yes")
    rows = hazards.normalize_openaq([_location(country="FR")],
                                    {"pm25": [_latest(10, 20)]})
    assert len(rows) == 1


# ---------- the reading at a watched place ----------

def _station(db, provider, lat, lon, aqi, estimated=False):
    db.add(Hazard(kind="air_quality", provider=provider,
                  external_id=f"{provider}:{lat},{lon}", name=f"{provider} site",
                  lat=lat, lon=lon, severity=40,
                  raw={"aqi": aqi, "estimated": estimated,
                       "parameter": "PM2.5", "value": 12.0, "unit": "µg/m³"}))
    db.commit()


def _place(db, lat, lon):
    loc = FavoriteLocation(user_id="u", name="Home", lat=lat, lon=lon)
    db.add(loc)
    db.commit()
    return loc


def test_two_scales_are_never_averaged_together(db):
    """The belt to the border's braces. `air_readings` averages every station
    within five kilometres, and an AirNow AQI averaged with an OpenAQ estimate
    is a number that is neither of them."""
    loc = _place(db, 48.85, 2.35)
    _station(db, "airnow", 48.851, 2.351, 20)
    _station(db, "openaq", 48.852, 2.352, 180, estimated=True)
    air = main.air_readings(db, [loc])[loc.id]
    assert air["stations"] == 1
    assert air["provider"] in ("airnow", "openaq")
    assert air["aqi"] in (20, 180), "an average of the two would be neither"


def test_the_nearest_station_decides_whose_scale_is_used(db):
    """A place on a border reports the network it actually sits in."""
    loc = _place(db, 48.85, 2.35)
    _station(db, "openaq", 48.8505, 2.3505, 180, estimated=True)
    _station(db, "airnow", 48.88, 2.39, 20)
    air = main.air_readings(db, [loc])[loc.id]
    assert air["provider"] == "openaq"
    assert air["estimated"] is True


def test_several_stations_from_one_provider_still_average(db):
    """The provider rule must not have broken the feature it guards."""
    loc = _place(db, 48.85, 2.35)
    _station(db, "openaq", 48.851, 2.351, 100, estimated=True)
    _station(db, "openaq", 48.852, 2.352, 140, estimated=True)
    air = main.air_readings(db, [loc])[loc.id]
    assert air["stations"] == 2
    assert air["aqi"] == 120


def test_the_reading_carries_whether_it_is_an_estimate(db):
    loc = _place(db, 48.85, 2.35)
    _station(db, "airnow", 48.851, 2.351, 42)
    air = main.air_readings(db, [loc])[loc.id]
    assert air["estimated"] is False
    assert air["provider"] == "airnow"


# ---------- fetching ----------

def test_no_key_means_no_provider_not_a_failure(monkeypatch):
    """Same graceful absence AirNow already has: nothing to explain to somebody
    who never asked for air quality."""
    import asyncio
    monkeypatch.delenv("OPENAQ_API_KEY", raising=False)
    assert asyncio.run(hazards.fetch_openaq(None)) is None


def test_a_missing_key_is_named_in_the_status(monkeypatch):
    monkeypatch.delenv("OPENAQ_API_KEY", raising=False)
    assert "openaq" in hazards.idle_status("x")["no_key"]
    monkeypatch.setenv("OPENAQ_API_KEY", "sk-yes")
    assert "openaq" not in hazards.idle_status("x")["no_key"]


class _Reply:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class _FakeClient:
    """Answers by path prefix. Records what was asked for, so a test can assert
    on the shape of the conversation rather than on the source text."""

    def __init__(self, routes):
        self.routes, self.asked = routes, []

    async def get(self, url, params=None, headers=None):
        self.asked.append((url, (params or {}).get("page")))
        for prefix, reply in self.routes.items():
            if prefix in url:
                return reply(len(self.asked)) if callable(reply) else reply
        return _Reply(404, {})


def _ok(results):
    return _Reply(200, {"results": results})


def test_a_rate_limit_is_a_failure_and_never_prunes(monkeypatch):
    """429 is the one that would otherwise look like an empty world — and an
    emptied table re-inserts everything on the next good poll, which once
    proximity alerting exists means notifying everybody about everything."""
    import asyncio
    monkeypatch.setenv("OPENAQ_API_KEY", "sk-yes")
    client = _FakeClient({"/parameters": _Reply(429, {})})
    assert asyncio.run(hazards._openaq_pages(client, "/parameters", {})) is None


@pytest.mark.parametrize("status", [401, 403, 500])
def test_any_bad_status_is_a_failure(status, monkeypatch):
    import asyncio
    monkeypatch.setenv("OPENAQ_API_KEY", "sk-yes")
    client = _FakeClient({"/locations": _Reply(status, {})})
    assert asyncio.run(hazards._openaq_pages(client, "/locations", {})) is None


def test_a_body_without_a_result_list_is_a_failure(monkeypatch):
    import asyncio
    monkeypatch.setenv("OPENAQ_API_KEY", "sk-yes")
    client = _FakeClient({"/locations": _Reply(200, {"detail": "nope"})})
    assert asyncio.run(hazards._openaq_pages(client, "/locations", {})) is None


def test_a_partial_fetch_never_becomes_a_whole_answer(monkeypatch):
    """If one pollutant's page fails, returning the rest would let the prune
    treat every station it did not reach as retired — and the next good poll
    would re-insert them all as new."""
    import asyncio
    monkeypatch.setenv("OPENAQ_API_KEY", "sk-yes")
    monkeypatch.delenv("AIRNOW_API_KEY", raising=False)
    client = _FakeClient({
        "/parameters/2/latest": _ok([_latest(10, 20)]),
        "/parameters/3/latest": _Reply(500, {}),          # one pollutant down
        "/parameters": _ok([{"id": 2, "name": "pm25"}, {"id": 3, "name": "o3"}]),
        "/locations": _ok([_location()]),
    })
    assert asyncio.run(hazards.fetch_openaq(client)) is None


def test_a_whole_fetch_produces_rows(monkeypatch):
    import asyncio
    monkeypatch.setenv("OPENAQ_API_KEY", "sk-yes")
    monkeypatch.delenv("AIRNOW_API_KEY", raising=False)
    client = _FakeClient({
        "/parameters/2/latest": _ok([_latest(10, 35.9)]),
        "/parameters": _ok([{"id": 2, "name": "pm25"}]),
        "/locations": _ok([_location()]),
    })
    rows = asyncio.run(hazards.fetch_openaq(client))
    assert len(rows) == 1 and rows[0]["raw"]["aqi"] == 102


def test_parameter_ids_are_looked_up_not_hardcoded(monkeypatch):
    """They are OpenAQ's own numbering and not part of any contract. A table of
    magic numbers here would fail silently if one ever moved — so the catalog
    is asked, and an id it does not list is simply not fetched."""
    import asyncio
    monkeypatch.setenv("OPENAQ_API_KEY", "sk-yes")
    monkeypatch.delenv("AIRNOW_API_KEY", raising=False)
    client = _FakeClient({
        "/parameters/77/latest": _ok([]),
        "/parameters": _ok([{"id": 77, "name": "pm25"}]),
        "/locations": _ok([_location()]),
    })
    asyncio.run(hazards.fetch_openaq(client))
    assert any("/parameters/77/latest" in url for url, _ in client.asked)


def test_pagination_stops_at_the_cap(monkeypatch):
    """A loop-until-done would let a provider bug hold the cycle lock for as
    long as it liked, and that lock is the one the news poll needs."""
    import asyncio
    monkeypatch.setenv("OPENAQ_API_KEY", "sk-yes")
    monkeypatch.setattr(hazards, "OPENAQ_MAX_PAGES", 3)
    monkeypatch.setattr(hazards, "OPENAQ_PAGE_SIZE", 2)
    client = _FakeClient({"/locations": lambda n: _ok([_location(1), _location(2)])})
    rows = asyncio.run(hazards._openaq_pages(client, "/locations", {}))
    assert len(client.asked) == 3
    assert [page for _, page in client.asked] == [1, 2, 3]
    assert len(rows) == 6


def test_a_short_page_ends_the_walk(monkeypatch):
    import asyncio
    monkeypatch.setenv("OPENAQ_API_KEY", "sk-yes")
    monkeypatch.setattr(hazards, "OPENAQ_PAGE_SIZE", 5)
    client = _FakeClient({"/locations": _ok([_location(1)])})
    asyncio.run(hazards._openaq_pages(client, "/locations", {}))
    assert len(client.asked) == 1


def test_openaq_is_a_provider():
    assert "openaq" in hazards.PROVIDERS


# ---------- and it is said where somebody is looking ----------

def test_the_pane_row_calls_an_estimate_an_estimate():
    src = _fn("function airLine")
    assert "not an" in src and "official AQI" in src
    assert "air.estimated" in src


def test_the_popup_says_so_too():
    src = _fn("function hazardPopup")
    assert "raw.estimated" in src
    assert "24-hour average" in src


def test_settings_names_both_keys_separately():
    """They cover different halves of the world; an operator who sets one still
    has a blank layer over the other."""
    src = _fn("function renderHazardStatus")
    assert "AIRNOW_API_KEY" in src and "OPENAQ_API_KEY" in src


def test_the_air_layer_no_longer_claims_to_be_united_states_only():
    assert 'TYPHON_AIR_LAYER = "🌫 Typhon · Air quality"' in APP
