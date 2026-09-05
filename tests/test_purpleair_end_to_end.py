"""PurpleAir from the wire to a watched place.

The other thirty-three PurpleAir tests hand `normalize_purpleair` a dict that
was written by hand. That covers the arithmetic and covers none of the wiring:
whether the request is actually shaped the way the API expects, whether the key
is actually sent, whether what comes back over a socket survives the trip into
the hazards table and out again onto somebody's location.

So this one runs a server that speaks PurpleAir's real response format — a
`fields` list and a `data` array of rows, with the column order chosen by the
server rather than by us — and follows one reading the whole way:

    watched place -> box -> HTTP request -> normalize -> upsert -> air reading

`safefetch` is deliberately not in the path: it refuses loopback addresses,
which is correct SSRF protection and is not what these are about. The client is
passed in, exactly as `poll()` passes one in.
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from backend.app import hazards, main
from backend.app.models import FavoriteLocation, Hazard, User, utcnow

KEY = "test-read-key"

# Four sensors, of which two should survive: one indoors, one the network
# itself doubts. The column order is deliberately not the order the code asks
# for, because PurpleAir answers in its own.
FIELDS = ["sensor_index", "name", "latitude", "longitude",
          "pm2.5_cf_1", "humidity", "confidence", "location_type", "last_seen"]
SENSORS = [
    [101, "Wenatchee Ave", 47.4235, -120.3103, 34.2, 41, 98, 0, None],
    [102, "Orchard St", 47.4290, -120.3050, 31.7, 44, 92, 0, None],
    [103, "Indoor kitchen", 47.4240, -120.3110, 12.0, 30, 99, 1, None],
    [104, "Flaky unit", 47.4260, -120.3080, 88.0, 40, 12, 0, None],
]


class _Stub:
    """A server that answers like api.purpleair.com and records what it was asked."""

    def __init__(self):
        self.requests = []
        probe = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                probe.requests.append({
                    "query": parse_qs(urlparse(self.path).query),
                    "headers": dict(self.headers),
                    "path": urlparse(self.path).path,
                })
                rows = [list(r) for r in SENSORS]
                for row in rows:
                    row[-1] = int(utcnow().timestamp())    # last_seen: fresh
                body = json.dumps({"fields": FIELDS, "data": rows}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/sensors"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def purpleair(monkeypatch):
    stub = _Stub()
    monkeypatch.setattr(hazards, "PURPLEAIR_URL", stub.url)
    monkeypatch.setenv("PURPLEAIR_API_KEY", KEY)
    yield stub
    stub.close()


@pytest.fixture
def watched(db):
    user = User(username="probe", email="p@x.test", password_hash="x")
    db.add(user)
    db.flush()
    loc = FavoriteLocation(user_id=f"acct:{user.id}", name="Wenatchee",
                           lat=47.4235, lon=-120.3103, radius_km=25)
    db.add(loc)
    db.commit()
    return loc


def _fetch(boxes):
    async def go():
        async with httpx.AsyncClient(timeout=10) as client:
            return await hazards.fetch_purpleair(client, boxes)
    return asyncio.run(go())


# --- the request that goes out ---------------------------------------------

def test_a_watched_place_becomes_exactly_one_request(db, watched, purpleair):
    boxes = hazards.air_watch_boxes(db)
    assert boxes == [(watched.lat, watched.lon)]
    _fetch(boxes)
    assert len(purpleair.requests) == 1


def test_the_key_travels_as_a_header_and_not_in_the_url(db, watched, purpleair):
    _fetch(hazards.air_watch_boxes(db))
    sent = purpleair.requests[0]
    assert sent["headers"].get("X-API-Key") == KEY
    assert KEY not in json.dumps(sent["query"]), "the key would end up in logs"


def test_the_request_asks_only_for_outdoor_and_recent(db, watched, purpleair):
    _fetch(hazards.air_watch_boxes(db))
    q = purpleair.requests[0]["query"]
    assert q["location_type"] == ["0"]
    assert int(q["max_age"][0]) == hazards.PURPLEAIR_MAX_AGE_S


def test_the_box_is_centred_on_the_place(db, watched, purpleair):
    _fetch(hazards.air_watch_boxes(db))
    q = purpleair.requests[0]["query"]
    west, east = float(q["nwlng"][0]), float(q["selng"][0])
    south, north = float(q["selat"][0]), float(q["nwlat"][0])
    assert west < watched.lon < east
    assert south < watched.lat < north


def test_an_instance_watching_nowhere_sends_nothing(db, purpleair):
    assert _fetch(hazards.air_watch_boxes(db)) is None
    assert purpleair.requests == []


# --- what comes back --------------------------------------------------------

def test_the_sensors_worth_keeping_are_kept(db, watched, purpleair):
    rows = _fetch(hazards.air_watch_boxes(db))
    assert [r["name"] for r in rows] == ["Wenatchee Ave", "Orchard St"]


def test_the_humidity_correction_is_applied_to_real_wire_data(db, watched, purpleair):
    """Not the raw number. A consumer sensor over-reads in humid air, and
    publishing what it says would print a confident wrong figure."""
    rows = {r["name"]: r for r in _fetch(hazards.air_watch_boxes(db))}
    first = rows["Wenatchee Ave"]["raw"]
    assert first["raw_value"] == 34.2          # what the sensor said
    assert first["value"] < first["raw_value"]  # what Delphi publishes
    assert first["value"] == pytest.approx(22.0, abs=0.5)


def test_the_reading_says_what_it_is(db, watched, purpleair):
    row = _fetch(hazards.air_watch_boxes(db))[0]["raw"]
    assert row["estimated"] is True and row["low_cost"] is True
    assert "PurpleAir" in row["agency"]        # attribution travels with it
    assert row["humidity"] == 41               # the input to the correction


# --- into the table and back out onto the place -----------------------------

def test_a_reading_reaches_the_place_somebody_watches(db, watched, purpleair):
    rows = _fetch(hazards.air_watch_boxes(db))
    hazards._upsert(db, rows)
    db.commit()

    assert db.query(Hazard).filter(Hazard.provider == "purpleair").count() == 2

    got = main.air_readings(db, [watched])[watched.id]
    assert got["provider"] == "purpleair"
    assert got["basis"] == "average"
    assert got["stations"] == 2
    assert got["category"] == "Moderate"
    # Averaged over the corrected values, not the raw ones.
    assert got["value"] == pytest.approx(20.6, abs=1.5)


def test_the_place_is_told_this_is_an_estimate(db, watched, purpleair):
    hazards._upsert(db, _fetch(hazards.air_watch_boxes(db)))
    db.commit()
    got = main.air_readings(db, [watched])[watched.id]
    assert got["estimated"] is True, "a community sensor is not an official AQI"
    assert got["low_cost"] is True


def test_a_second_poll_updates_rather_than_duplicates(db, watched, purpleair):
    for _ in range(2):
        hazards._upsert(db, _fetch(hazards.air_watch_boxes(db)))
        db.commit()
    assert db.query(Hazard).filter(Hazard.provider == "purpleair").count() == 2
