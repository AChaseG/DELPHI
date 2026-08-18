"""A fire is a shape, not a dot.

Typhon drew every wildfire as one circle at the point the agency reported.
`mailer.send_hazard_digest` has had to apologise for that since it was
written — "the incident point the agency reported, which for a large fire can
be well inside its own perimeter" — because a hundred-thousand-acre fire is a
circle roughly eleven kilometres in radius, and a twelve-pixel dot is wrong by
about that much, in the direction that decides whether a reader thinks it is
near them.

Two shapes fix it and they are not the same claim, which is what most of this
file is about:

  · a **perimeter** is measured — somebody flew it — and is drawn solid;
  · an **acreage circle** is arithmetic on one reported number, drawn dashed
    and hollow, and says nothing whatever about which way the fire has run.

The rule underneath: **a computed shape must never be dressed as a measured
one.**
"""
import json
import pathlib

import pytest

from backend.app import geo, hazards
from backend.app.models import Hazard, utcnow

FRONTEND = pathlib.Path(__file__).resolve().parent.parent / "frontend"
APP = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
API_JS = (FRONTEND / "js" / "api.js").read_text(encoding="utf-8")


def _fn(name: str, end: str = "\n}\n") -> str:
    start = APP.index(name)
    return APP[start:APP.find(end, start)]


SQUARE = {"type": "Polygon", "coordinates":
          [[[-120.0, 39.0], [-119.9, 39.0], [-119.9, 39.1],
            [-120.0, 39.1], [-120.0, 39.0]]]}


def _collection(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def _feature(irwin, geometry=None, acres=5000, date=1_700_000_000_000):
    return {"type": "Feature",
            "properties": {"attr_IrwinID": irwin, "poly_GISAcres": acres,
                           "poly_DateCurrent": date},
            "geometry": SQUARE if geometry is None else geometry}


# ---------- reading the feed ----------

def test_an_error_body_is_not_an_empty_world():
    """ArcGIS serves its errors with HTTP 200 and an `error` key, so the status
    line never tells you. The same trap the incident parser already guards."""
    assert hazards.normalize_perimeters({"error": {"code": 400}}) is None
    assert hazards.normalize_perimeters("not a dict") is None
    assert hazards.normalize_perimeters({"features": "nope"}) is None


def test_shapes_come_back_keyed_by_the_fire_they_belong_to():
    out = hazards.normalize_perimeters(_collection(_feature("IRWIN-1")))
    assert list(out) == ["IRWIN-1"]
    geometry, drawn_at = out["IRWIN-1"]
    assert geometry == SQUARE
    assert drawn_at is not None


def test_a_feature_with_no_irwin_id_is_dropped():
    """It cannot be attached to anything, and this parser never creates a
    hazard of its own — the incident service stays the authority on which
    fires exist."""
    bad = _feature("")
    assert hazards.normalize_perimeters(_collection(bad)) == {}


@pytest.mark.parametrize("geometry", [
    None, "a string", {"type": "Point", "coordinates": [1, 2]},
    {"type": "Polygon", "coordinates": []},
])
def test_anything_that_is_not_an_area_is_dropped(geometry):
    out = hazards.normalize_perimeters(_collection(
        {"type": "Feature", "properties": {"attr_IrwinID": "X"},
         "geometry": geometry}))
    assert out == {}


def test_an_iso_date_is_read_as_well_as_epoch_milliseconds():
    """ArcGIS is consistent about epoch ms until it isn't."""
    out = hazards.normalize_perimeters(_collection(
        _feature("I1", date="2026-08-01T12:00:00Z")))
    assert out["I1"][1].year == 2026
    assert hazards.normalize_perimeters(
        _collection(_feature("I2", date="rubbish")))["I2"][1] is None


def test_a_shape_too_large_to_draw_is_dropped_and_the_fire_survives(monkeypatch):
    """The cap is on the geometry, never on the hazard. A fire whose outline
    will not fit is still a fire; it falls back to the labelled circle, which
    is a smaller lie than a payload that punishes everybody panning the map."""
    monkeypatch.setattr(hazards, "MAX_GEOMETRY_BYTES", 50)
    assert hazards.normalize_perimeters(_collection(_feature("I1"))) == {}
    monkeypatch.setattr(hazards, "MAX_GEOMETRY_BYTES", 100_000)
    assert "I1" in hazards.normalize_perimeters(_collection(_feature("I1")))


def test_the_query_asks_the_server_to_do_the_generalising():
    """maxAllowableOffset is the whole cost story: without it a single federal
    perimeter can be a megabyte of vertices, none of which survive a screen.
    Asserted on the source because the service cannot be reached from here."""
    import inspect
    src = inspect.getsource(hazards.fetch_perimeters)
    assert "maxAllowableOffset" in src
    assert "geometryPrecision" in src
    assert '"f": "geojson"' in src, (
        "GeoJSON is asked for so that neither geo.point_in_geo nor Leaflet "
        "needs an Esri-rings converter, which is where winding order goes wrong")


# ---------- attaching them ----------

def _fire(db, irwin, **kw):
    row = Hazard(kind="wildfire", provider="wfigs", external_id=irwin,
                 name=irwin, lat=39.05, lon=-119.95, country="US",
                 severity=70, raw={"acres": 5000}, **kw)
    db.add(row)
    db.commit()
    return row


def test_a_shape_attaches_to_its_fire(db):
    fire = _fire(db, "IRWIN-1")
    shapes = hazards.normalize_perimeters(_collection(_feature("IRWIN-1")))
    assert hazards.attach_perimeters(db, shapes) == 1
    db.refresh(fire)
    assert fire.geometry == SQUARE
    assert fire.geometry_at is not None


def test_a_shape_for_a_fire_we_do_not_hold_creates_nothing(db):
    """The perimeter service is not allowed to invent incidents. It carries
    only the fires big enough to be flown, so trusting it as a roster would
    quietly drop every small fire off the map."""
    before = db.query(Hazard).count()
    hazards.attach_perimeters(
        db, hazards.normalize_perimeters(_collection(_feature("GHOST"))))
    assert db.query(Hazard).count() == before


def test_re_attaching_the_same_shape_is_not_a_change(db):
    fire = _fire(db, "IRWIN-1")
    shapes = hazards.normalize_perimeters(_collection(_feature("IRWIN-1")))
    assert hazards.attach_perimeters(db, shapes) == 1
    assert hazards.attach_perimeters(db, shapes) == 0
    db.refresh(fire)
    assert fire.geometry == SQUARE


def test_a_perimeter_feed_that_fails_leaves_the_last_shape_alone(db):
    """Deliberately the opposite of every other fetch here. Elsewhere silence
    must never prune, because a wiped table re-alerts everyone; here silence
    must never *clear*, because the fires are the payload and the outlines are
    decoration on rows that are already correct without them."""
    fire = _fire(db, "IRWIN-1")
    hazards.attach_perimeters(
        db, hazards.normalize_perimeters(_collection(_feature("IRWIN-1"))))
    hazards.attach_perimeters(db, {})          # the feed said nothing
    db.refresh(fire)
    assert fire.geometry == SQUARE


def test_a_stored_perimeter_is_geojson_the_rest_of_delphi_already_reads(db):
    """The assertion that proves the format is genuinely GeoJSON rather than
    Esri rings that happen to look similar: hand it to the geofence code that
    has been in this repo since long before Typhon."""
    fire = _fire(db, "IRWIN-1")
    hazards.attach_perimeters(
        db, hazards.normalize_perimeters(_collection(_feature("IRWIN-1"))))
    db.refresh(fire)
    assert geo.point_in_geo(39.05, -119.95, fire.geometry) is True
    assert geo.point_in_geo(10.0, 10.0, fire.geometry) is False


# ---------- distance to the edge ----------

def test_a_place_inside_a_perimeter_is_zero_from_its_edge():
    assert geo.nearest_edge_km(39.05, -119.95, SQUARE) == 0.0


def test_a_place_outside_measures_to_the_nearest_corner():
    km = geo.nearest_edge_km(39.0, -121.0, SQUARE)
    expected = geo.haversine_km(39.0, -121.0, 39.0, -120.0)
    assert km == pytest.approx(expected, abs=0.01)


def test_a_shape_with_no_ring_has_no_answer_rather_than_a_wrong_one():
    """None and 0.0 are different facts — "no perimeter" and "you are standing
    in it" — and the email says something different for each."""
    assert geo.nearest_edge_km(0, 0, {}) is None
    assert geo.nearest_edge_km(0, 0, {"type": "Point", "coordinates": [0, 0]}) is None


def test_the_email_says_inside_rather_than_a_distance_to_a_dot():
    from backend.app import mailer
    hits = [{"name": "Bear Fire", "distance_km": 8.0, "edge_km": 0.0,
             "acres": 5000, "containment": 10}]
    sent = []
    mailer.send  # present
    import unittest.mock as mock
    with mock.patch.object(mailer, "send", lambda *a: sent.append(a) or True):
        mailer.send_hazard_digest("x@example.com", "Home", hits)
    body = sent[0][2]
    assert "Home is inside the mapped perimeter" in body


def test_the_email_gives_both_distances_when_it_can():
    from backend.app import mailer
    import unittest.mock as mock
    hits = [{"name": "Bear Fire", "distance_km": 40.0, "edge_km": 21.0}]
    sent = []
    with mock.patch.object(mailer, "send", lambda *a: sent.append(a) or True):
        mailer.send_hazard_digest("x@example.com", "Home", hits)
    body = sent[0][2]
    assert "40.0 km" in body and "21.0 km" in body


def test_the_email_falls_back_to_the_old_caveat_with_no_perimeter():
    from backend.app import mailer
    import unittest.mock as mock
    sent = []
    with mock.patch.object(mailer, "send", lambda *a: sent.append(a) or True):
        mailer.send_hazard_digest("x@example.com", "Home",
                                  [{"name": "F", "distance_km": 12.0,
                                    "edge_km": None}])
    body = sent[0][2]
    assert "reported 12.0 km from Home" in body
    assert "mapped edge" not in body


# ---------- the API ----------

def test_shapes_are_opt_in(client, register):
    """A perimeter is orders of magnitude larger than the row carrying it, and
    the default response is the one the flame logic needs — so it stays small
    and the geometry is asked for separately."""
    headers = register("shapes")
    rows = client.get("/api/hazards", headers=headers).json()
    assert isinstance(rows, list)
    for row in rows:
        assert "has_geometry" in row
        assert "geometry" not in row


def test_has_geometry_is_always_there(db, client, register):
    """It is what lets the client decide to draw the approximate circle
    without a second round trip."""
    _fire(db, "IRWIN-1", geometry=SQUARE, geometry_at=utcnow())
    _fire(db, "IRWIN-2")
    headers = register("shapes2")
    rows = client.get("/api/hazards?bbox=-121,38,-119,40", headers=headers).json()
    by_id = {r["name"]: r for r in rows}
    assert by_id["IRWIN-1"]["has_geometry"] is True
    assert by_id["IRWIN-2"]["has_geometry"] is False
    assert "geometry" not in by_id["IRWIN-1"]

    rows = client.get("/api/hazards?bbox=-121,38,-119,40&geometry=1",
                      headers=headers).json()
    by_id = {r["name"]: r for r in rows}
    assert by_id["IRWIN-1"]["geometry"] == SQUARE
    assert "geometry" not in by_id["IRWIN-2"], (
        "a null shape must not become a null key — the client tests truthiness")


# ---------- the map ----------

def test_the_client_only_asks_for_shapes_when_one_would_be_visible():
    src = _fn("  async refreshHazards()", "\n  },\n")
    assert "TYPHON_SHAPE_ZOOM" in src
    assert "getZoom()" in src


def test_crossing_the_zoom_threshold_re_fetches():
    """The pan guard exists to stop re-requesting the same rectangle, and it
    would just as happily stop the first request that wanted geometry. That
    guard has already caused two bugs of exactly this shape."""
    src = _fn("  async refreshHazards()", "\n  },\n")
    assert 'wantShapes ? "|g" : ""' in src
    assert "if (key === this._hazKey) return;" in src


def test_a_measured_perimeter_and_a_computed_circle_are_drawn_differently():
    """The load-bearing assertion in this file. One is where the fire's edge
    is; the other is how much has burned. Styled alike, the second becomes a
    confident wrong answer to the question the first exists to answer."""
    src = _fn("function hazardShape")
    perimeter = src[src.index("if (h.geometry)"):src.index("const acres")]
    circle = src[src.index("return L.circle"):]
    assert "fillOpacity" in perimeter and "dashArray" not in perimeter
    assert "dashArray" in circle and "fill: false" in circle


def test_the_circle_is_equal_area_and_matches_the_arithmetic():
    """100,000 acres is 404.7 km², which is a circle of radius 11.35 km. If
    this ever drifts, every large fire on the map is the wrong size."""
    src = _fn("function acreCircleM")
    assert "Math.PI" in src and "SQM_PER_ACRE" in src
    import math
    sqm_per_acre = float(APP.split("const SQM_PER_ACRE = ")[1].split(";")[0])
    radius_m = math.sqrt(100_000 * sqm_per_acre / math.pi)
    assert radius_m / 1000 == pytest.approx(11.35, abs=0.05)


def test_a_small_fire_gets_no_circle():
    """Below about a thousand acres the circle is smaller than the marker
    sitting inside it, so it is clutter that says nothing."""
    src = _fn("function hazardShape")
    assert "if (acres < MIN_CIRCLE_ACRES) return null;" in src


def test_a_station_never_gets_a_shape():
    src = _fn("function hazardShape")
    assert 'if (h.kind === "air_quality") return null;' in src


def test_the_popup_says_which_shape_it_is_looking_at():
    """Every branch is a different claim and the reader is entitled to know
    which one they have."""
    src = _fn("function hazardPopup")
    assert "Perimeter mapped" in src
    assert "approximate area burned" in src
    assert "zoom in to see it" in src


def test_the_shape_is_drawn_under_the_dot():
    """A perimeter at continental zoom is one pixel, so the dot stays: it is
    the click target, the label anchor, and the thing the flame replaces."""
    src = _fn("  drawHazards()", "\n  },\n")
    assert src.index("hazardShape(h") < src.index("const marker = near")


def test_the_request_carries_the_flag_only_when_asked():
    assert "geometry ? \"&geometry=1\" : \"\"" in API_JS
