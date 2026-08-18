"""The rest of what makes a place dangerous.

Typhon shipped as two United States layers on a product read in twenty-three
languages, and no amount of careful labelling fixes that. Three feeds close
most of the gap, all keyless, all one request:

  · **GDACS** — the global multi-hazard spine. Earthquakes, cyclones, floods,
    volcanoes, droughts and fires, each carrying a green/orange/red level that
    is an *impact* judgement rather than a measurement, which is the right
    input to a severity scale: a magnitude 6 under a city and a magnitude 6
    under an ocean are not the same event.
  · **USGS** — earthquakes, minutes behind the shaking, with PAGER's own
    impact colour where there is one.
  · **NWS** — United States severe weather, and the only one of the three that
    publishes a polygon. The geometry column the fire perimeters added is what
    draws it.

Two rules this file exists to hold:

  · **an empty answer is a failed poll — except where it is declared not to
    be**, and the exception list is short and deliberate;
  · **a warning ends when it says it ends**, whether or not the provider that
    issued it can be reached.
"""
import pathlib

import pytest

from backend.app import hazards
from backend.app.models import Hazard, utcnow

FRONTEND = pathlib.Path(__file__).resolve().parent.parent / "frontend"
APP = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
CSS = (FRONTEND / "css" / "styles.css").read_text(encoding="utf-8")


def _fn(name: str, end: str = "\n}\n") -> str:
    start = APP.index(name)
    return APP[start:APP.find(end, start)]


# ---------- GDACS ----------

def _gdacs(event_type="EQ", event_id="1", level="Orange", iso3="ITA",
           lon=12.5, lat=41.9):
    return {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {"eventtype": event_type, "eventid": event_id,
                       "eventname": "Test event", "alertlevel": level,
                       "iso3": iso3, "country": "Italy",
                       "fromdate": "2026-08-01T00:00:00",
                       "severitydata": {"severitytext": "Magnitude 6.1M"}},
    }]}


@pytest.mark.parametrize("code,kind", [
    ("EQ", "earthquake"), ("TC", "storm"), ("FL", "flood"),
    ("VO", "volcano"), ("DR", "drought"), ("WF", "wildfire"),
])
def test_every_gdacs_code_lands_on_a_kind_the_map_can_draw(code, kind):
    rows = hazards.normalize_gdacs(_gdacs(event_type=code))
    assert rows[0]["kind"] == kind
    assert kind in hazards.KINDS, "a kind with no layer is a row nobody sees"


@pytest.mark.parametrize("level,severity", [
    ("Red", 90), ("Orange", 60), ("Green", 25), ("", 25),
])
def test_the_alert_level_is_the_severity(level, severity):
    """Deliberately not the magnitude. GDACS's level is an assessment of what
    the event did to people, which is the question a reader is actually
    asking."""
    assert hazards.normalize_gdacs(_gdacs(level=level))[0]["severity"] == severity


def test_gdacs_wildfires_stay_out_of_the_united_states():
    """WFIGS is strictly better there — an incident point with acreage and
    containment against a continent-scale bulletin — and two providers drawing
    the same fire would put two dots on it under two different names."""
    assert hazards.normalize_gdacs(_gdacs("WF", iso3="USA")) == []
    assert len(hazards.normalize_gdacs(_gdacs("WF", iso3="GRC"))) == 1


def test_a_non_wildfire_us_event_is_still_drawn():
    """The exclusion is about the one kind another provider covers better, not
    about the country."""
    assert len(hazards.normalize_gdacs(_gdacs("EQ", iso3="USA"))) == 1


def test_the_event_id_is_the_identity_so_an_escalation_overwrites():
    """GDACS re-issues an event as its assessment changes, and the whole point
    of a table that expects rewriting is that "the alert went from orange to
    red" reaches a reader at all."""
    green = hazards.normalize_gdacs(_gdacs(level="Green"))[0]
    red = hazards.normalize_gdacs(_gdacs(level="Red"))[0]
    assert green["external_id"] == red["external_id"]
    assert red["severity"] > green["severity"]


@pytest.mark.parametrize("payload", [
    "not a dict", {"features": "no"}, {},
])
def test_a_malformed_gdacs_body_is_a_failure(payload):
    assert hazards.normalize_gdacs(payload) is None


def test_an_unknown_event_code_is_skipped_rather_than_guessed():
    assert hazards.normalize_gdacs(_gdacs(event_type="ZZ")) == []


# ---------- USGS ----------

def _usgs(mag=6.2, pager="", event_id="us1", lon=-122.0, lat=38.0, depth=10.0):
    return {"type": "FeatureCollection", "features": [{
        "type": "Feature", "id": event_id,
        "geometry": {"type": "Point", "coordinates": [lon, lat, depth]},
        "properties": {"mag": mag, "place": "10km NE of Somewhere",
                       "time": 1_700_000_000_000, "alert": pager,
                       "title": f"M {mag} - somewhere", "tsunami": 0},
    }]}


@pytest.mark.parametrize("mag,expected", [
    (8.5, 95), (7.2, 85), (6.2, 70), (5.1, 50), (4.2, 35), (2.6, 20),
])
def test_magnitude_bands_when_there_is_no_impact_estimate(mag, expected):
    assert hazards.normalize_usgs(_usgs(mag=mag))[0]["severity"] == expected


def test_the_impact_estimate_beats_the_magnitude_when_there_is_one():
    """A magnitude 5 that flattened a town and a magnitude 7 under an ocean
    are not equally serious, and PAGER is the only one of the two numbers that
    knows the difference."""
    quiet = hazards.normalize_usgs(_usgs(mag=7.2))[0]["severity"]
    loud = hazards.normalize_usgs(_usgs(mag=5.0, pager="red"))[0]["severity"]
    assert loud > quiet


def test_depth_and_place_ride_along_for_the_popup():
    raw = hazards.normalize_usgs(_usgs())[0]["raw"]
    assert raw["depth_km"] == 10.0
    assert raw["place"].startswith("10km")


def test_an_event_with_no_magnitude_is_dropped():
    """A quake row with no magnitude cannot be scored, and a severity invented
    for it would colour the map on nothing."""
    payload = _usgs()
    payload["features"][0]["properties"]["mag"] = None
    assert hazards.normalize_usgs(payload) == []


def test_the_feed_chosen_is_one_that_is_never_legitimately_empty():
    """The hourly USGS feeds are empty on a quiet hour, and this module treats
    an empty answer as a failed poll — so an hourly feed would report failure
    for ever on exactly the nights when nothing is wrong."""
    assert "2.5_day" in hazards.USGS_URL
    assert "hour" not in hazards.USGS_URL


# ---------- NWS ----------

POLY = {"type": "Polygon", "coordinates":
        [[[-97.0, 35.0], [-96.0, 35.0], [-96.0, 36.0],
          [-97.0, 36.0], [-97.0, 35.0]]]}


def _nws(geometry=POLY, severity="Severe", expires="2026-08-17T18:00:00+00:00"):
    return {"type": "FeatureCollection", "features": [{
        "type": "Feature", "id": "urn:oid:2.49.0.1.840.0.abc",
        "geometry": geometry,
        "properties": {"event": "Tornado Warning", "severity": severity,
                       "urgency": "Immediate", "certainty": "Observed",
                       "areaDesc": "Canadian, OK", "expires": expires,
                       "effective": "2026-08-17T17:30:00+00:00",
                       "headline": "Tornado Warning until 1pm",
                       "senderName": "NWS Norman OK"},
    }]}


def test_a_warning_keeps_its_polygon():
    """The first non-fire use of the geometry column — and the reason it was
    written as a general shape rather than a perimeter field."""
    row = hazards.normalize_nws(_nws())[0]
    assert row["geometry"] == POLY
    assert row["kind"] == "severe_weather"


def test_the_marker_sits_in_the_middle_of_the_warning():
    row = hazards.normalize_nws(_nws())[0]
    assert row["lat"] == pytest.approx(35.5)
    assert row["lon"] == pytest.approx(-96.5)


def test_a_zone_coded_alert_with_no_outline_is_skipped():
    """Placing it would mean a request per zone to look the boundary up, which
    is the one-request-per-provider property this module is built on. What
    survives is the sharp-edged half — tornado, severe thunderstorm, flash
    flood — which is also the half where an edge is worth having."""
    assert hazards.normalize_nws(_nws(geometry=None)) == []


@pytest.mark.parametrize("severity,score", [
    ("Extreme", 90), ("Severe", 70), ("Moderate", 45), ("Minor", 25),
])
def test_the_nws_severity_words_map_to_the_shared_scale(severity, score):
    assert hazards.normalize_nws(_nws(severity=severity))[0]["severity"] == score


def test_the_query_asks_only_for_the_severe_half(monkeypatch):
    """Unfiltered this is thousands of frost advisories and small-craft
    warnings, and a map showing everything shows nothing."""
    import inspect
    src = inspect.getsource(hazards.fetch_nws)
    assert "Severe,Extreme" in src


def test_no_contact_address_means_no_provider(monkeypatch):
    """api.weather.gov rejects requests without a descriptive User-Agent
    carrying a contact, and a self-hosted copy is not us — so the address is an
    env var, and without it the provider is simply absent."""
    import asyncio
    monkeypatch.setattr(hazards, "NWS_CONTACT", "")
    assert asyncio.run(hazards.fetch_nws(None)) is None


def test_the_user_agent_carries_the_contact():
    import inspect
    src = inspect.getsource(hazards.fetch_nws)
    assert "User-Agent" in src and "NWS_CONTACT" in src


# ---------- an empty answer, and when it is allowed to mean something ----------

def test_an_empty_answer_is_a_failed_poll_by_default():
    """The rule the whole module turns on: a wiped table re-inserts everything
    on the next good poll, and once proximity alerting exists that means
    notifying every watcher about every fire they were already told about."""
    assert "wfigs" not in hazards.EMPTY_IS_PLAUSIBLE
    assert "gdacs" not in hazards.EMPTY_IS_PLAUSIBLE
    assert "usgs" not in hazards.EMPTY_IS_PLAUSIBLE
    assert "airnow" not in hazards.EMPTY_IS_PLAUSIBLE


def test_the_exception_is_declared_and_short():
    """A quiet night genuinely does mean no severe weather warnings anywhere in
    the country, and refusing that answer would leave expired tornado warnings
    drawn until something else went wrong."""
    assert hazards.EMPTY_IS_PLAUSIBLE == {"nws"}


def test_a_declared_empty_provider_still_counts_as_having_answered():
    """Which is what lets its prune run. If it did not count, the empty answer
    would change nothing and the warnings would stay."""
    import asyncio
    import unittest.mock as mock

    async def nothing(client):
        return []

    async def something(client):
        return [{"provider": "usgs", "kind": "earthquake"}]

    with mock.patch.dict(hazards.PROVIDERS,
                         {"nws": nothing, "usgs": something}, clear=True):
        with mock.patch.object(hazards.safefetch, "client"):
            rows, answered = asyncio.run(hazards._fetch_all())
    assert "nws" in answered
    assert "usgs" in answered


# ---------- a warning ends when it says it ends ----------

def _warning(db, expires):
    row = Hazard(kind="severe_weather", provider="nws",
                 external_id=f"w:{expires}", name="Tornado Warning",
                 lat=35.5, lon=-96.5, country="US", severity=70,
                 raw={"expires": expires})
    db.add(row)
    db.commit()
    return row


def test_a_lapsed_warning_is_removed(db):
    _warning(db, "2020-01-01T00:00:00+00:00")
    assert hazards._prune_expired(db) == 1
    assert db.query(Hazard).count() == 0


def test_a_live_warning_is_left_alone(db):
    _warning(db, "2099-01-01T00:00:00+00:00")
    assert hazards._prune_expired(db) == 0
    assert db.query(Hazard).count() == 1


def test_a_warning_with_no_stated_end_is_left_to_the_retention_rule(db):
    _warning(db, "")
    assert hazards._prune_expired(db) == 0


def test_expiry_is_checked_whether_or_not_the_provider_answered(db):
    """The point of this being separate from _prune, which is scoped to
    providers that spoke. An expired warning is expired regardless of whether
    NWS could be reached, and leaving one drawn because the service was down
    would be showing a warning that no longer exists."""
    import inspect
    src = inspect.getsource(hazards.poll)
    assert "_prune_expired, db" in src
    assert list(inspect.signature(hazards._prune_expired).parameters) == ["db"], (
        "it must not be able to take the answered set, because there is no "
        "reading of it that should change whether a lapsed warning goes")


def test_an_earthquake_does_not_linger_for_a_week(db):
    """A fire that stopped being listed is still worth showing while the
    picture settles. An earthquake is over the moment it happens, and a week of
    them is a map of last week."""
    days = hazards._retention_days()
    assert days["earthquake"] < days["wildfire"]
    assert set(days) == set(hazards.KINDS), (
        "a kind with no retention rule would live in the table for ever")


# ---------- the layers panel ----------

def test_the_panel_is_two_folders():
    assert 'id="layer-folder-bases"' in HTML
    assert 'id="layer-folder-typhon"' in HTML
    assert HTML.count("<details") >= 2


def test_the_folders_are_native_disclosure_not_a_state_machine():
    """<details>/<summary> brings keyboard and screen-reader behaviour with it,
    and `open` is one attribute to remember."""
    panel = HTML[HTML.index('id="map-layers"'):HTML.index("</aside>",
                                                          HTML.index('id="map-layers"'))]
    assert "<summary" in panel
    assert panel.count('class="layer-folder"') == 2


def test_typhon_ships_open_and_base_maps_ship_closed():
    """A ground is chosen once; the hazards are switched constantly."""
    typhon = HTML[HTML.index('id="layer-folder-typhon"'):]
    assert typhon[:120].count(" open") == 1
    bases = HTML[HTML.index('id="layer-folder-bases"'):
                 HTML.index('id="layer-folder-typhon"')]
    assert " open" not in bases[:120]


def test_there_is_one_switch_over_all_of_them():
    assert 'id="layer-all"' in HTML


def test_the_parent_uses_the_real_partial_state():
    """`indeterminate` is a genuine checkbox state the browser draws, so "some
    of these are on" needs no third control invented for it."""
    src = _fn("  renderParentSwitch()", "\n  },\n")
    assert "all.indeterminate = on > 0 && on < TYPHON_KINDS.length;" in src
    assert "all.checked = on === TYPHON_KINDS.length;" in src


def test_pressing_the_parent_does_not_also_open_the_folder():
    src = _fn("  renderParentSwitch()", "\n  },\n")
    assert "e.stopPropagation()" in src


def test_the_parent_drives_the_children_through_the_same_path():
    """Remembering, the empty-state note and the fetch all hang off setOverlay.
    A second path for the parent would be a second place for each of them to be
    forgotten."""
    src = _fn("  renderParentSwitch()", "\n  },\n")
    assert "this.setOverlay(entry.kind, wanted)" in src
    child = _fn("  renderLayerPanel()", "\n  },\n")
    assert "this.setOverlay(entry.kind, input.checked);" in child


def test_a_partial_state_fills_rather_than_clears():
    """Somebody with three of eight on who presses the parent wants everything,
    not nothing — which is what the browser's own click sequence gives, since
    an indeterminate box goes to checked."""
    src = _fn("  renderParentSwitch()", "\n  },\n")
    assert "const wanted = all.checked;" in src


def test_every_kind_has_a_switch():
    for kind in hazards.KINDS:
        assert f'kind: "{kind}"' in APP, f"{kind} has no layer to switch on"


def test_the_overlays_are_keyed_by_kind_not_by_label():
    """drawHazards has always branched on `h.kind` while the panel was keyed by
    its display string, so the two agreed only by coincidence and a reworded
    label was a silent bug."""
    init = _fn("  async _init()", "\n  },\n")
    assert "this._overlays[entry.kind] = { ...entry, group: L.featureGroup() };" in init
    assert "this.hazards = this._overlays.wildfire.group;" in init


def test_the_storage_key_is_separate_from_the_label():
    """So rewording a label never forgets somebody's choice."""
    assert 'key: "gnd_overlay_hazards"' in APP
    assert 'key: "gnd_overlay_air"' in APP


def test_the_folders_remember_whether_they_were_open():
    src = _fn("  rememberFolders()", "\n  },\n")
    assert "gnd_folder_typhon" in src and "gnd_folder_bases" in src
    assert 'box.addEventListener("toggle"' in src


def test_the_request_asks_only_for_the_kinds_on_show():
    src = _fn("  async refreshHazards()", "\n  },\n")
    assert "const kinds = this.activeKinds();" in src
    assert "API.hazards(bbox, TYPHON_MAX, wantShapes, kinds)" in src


def test_switching_a_layer_on_re_fetches_although_the_map_has_not_moved():
    """The pan guard exists to stop re-requesting the same rectangle, and now
    that the request carries the kind set it would just as happily stop the
    first request that wanted a newly ticked kind. That guard has already
    caused two bugs of this exact shape."""
    src = _fn("  async refreshHazards()", "\n  },\n")
    assert "kinds.join(\",\")" in src
    assert src.index("kinds.join") < src.index("this._hazKey = key;")


def test_a_row_of_a_kind_with_no_layer_is_skipped_not_dumped_on_another():
    src = _fn("  drawHazards()", "\n  },\n")
    assert "const owner = this._overlays[h.kind];" in src
    assert "if (!owner) continue;" in src


def test_the_note_still_sits_inside_the_folder_it_explains():
    panel = HTML[HTML.index('id="layer-folder-typhon"'):
                 HTML.index("</aside>", HTML.index('id="layer-folder-typhon"'))]
    assert 'id="map-note"' in panel
    assert panel.index('id="layer-overlays"') < panel.index('id="map-note"')


# ---------- what the kinds look like ----------

def test_each_kind_has_a_glyph_of_its_own():
    for kind in ("earthquake", "storm", "flood", "volcano", "drought",
                 "severe_weather"):
        assert f"{kind}:" in APP.split("_KIND_GLYPHS = ")[1][:400]


def test_fires_and_stations_keep_what_they_had():
    """A fire is a dot sized by acreage or a flame inside a ring; a station is
    a dot whose colour is the whole of its meaning. Neither gains from a glyph
    and changing them would be churn."""
    glyphs = APP.split("_KIND_GLYPHS = ")[1][:400]
    assert "wildfire" not in glyphs and "air_quality" not in glyphs


def test_the_glyph_is_coloured_by_severity_not_by_kind():
    """Shape says what sort of thing it is; colour still says how bad. Losing
    the second to gain the first would be a poor trade."""
    src = _fn("function hazardIcon")
    assert "background:${colour}" in src


def test_the_icons_are_memoised_so_panning_does_not_rebuild_them():
    src = _fn("function hazardIcon")
    assert "_KIND_ICONS.has(cacheKey)" in src


def test_nothing_third_party_reaches_innerhtml():
    """The glyph comes from a table in this file and the colour from
    hazardStyle. A provider's own text never goes near it."""
    src = _fn("function hazardIcon")
    assert "h.name" not in src and "raw" not in src


def test_a_warning_polygon_draws_like_a_perimeter():
    src = _fn("function hazardShape")
    assert 'if (h.kind !== "wildfire") return null;' in src, (
        "the acreage circle is arithmetic on reported acres and no other kind "
        "reports an area to do arithmetic on")


def test_the_popup_knows_the_new_kinds():
    src = _fn("function hazardPopup")
    for phrase in ("Magnitude", "GDACS alert level", "Expires"):
        assert phrase in src


def test_the_folders_have_a_disclosure_marker_in_both_themes():
    assert ".layer-folder > summary::before" in CSS
    assert ".layer-folder[open] > summary::before" in CSS


def test_the_panel_still_works_stacked_under_the_map():
    """820px is where the three columns become a stack, and the folders have to
    lay out sideways there or they push the map off the screen."""
    narrow = CSS[CSS.index("max-width: 820px"):]
    assert ".layer-folder" in narrow
