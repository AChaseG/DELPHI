"""A flame for the fires that concern you.

Every wildfire is drawn the same dot, sized and coloured by how big it is. That
answers "how bad is that one" and not the question anybody actually opens the
map with, which is *is one of them near me* — a 120,000-acre fire in Montana
and a 900-acre one eight kilometres from the house look identical today, and
the big one takes the eye. So a fire inside a watched place's ring becomes a
flame: a different shape, readable before any judgement of size or colour.

"In range" means `fire_km` and nothing else — the same distance the alerts use,
so a flame on the map and a toast on the screen always mean the same thing. A
place with no ring has not asked to hear about fires and gets no flames.

Worked out on the client, which is the exception in Typhon and the reason is
worth pinning: both halves are already here (LOCATIONS carries lat/lon/fire_km,
the hazard rows carry lat/lon) and `haversineKm` is already used for exactly
this shape of test. A server field would be a second implementation of a
distance that already agrees with `geo.haversine_km`.
"""
import pathlib
import re

FRONTEND = pathlib.Path(__file__).resolve().parent.parent / "frontend"
APP = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
CSS = (FRONTEND / "css" / "styles.css").read_text(encoding="utf-8")


def _fn(name: str, end: str = "\n}\n") -> str:
    start = APP.index(name)
    stop = APP.find(end, start)
    assert stop > start, f"could not find the end of {name}"
    return APP[start:stop]


HIT = _fn("function fireRingHit")
FLAME = _fn("function fireFlame")
POPUP = _fn("function hazardPopup")


# ---------- what counts as in range ----------

def test_the_ring_is_the_only_thing_that_counts():
    """`fire_km`, not the news radius. The alerts use the same number, so the
    map and the notifications can never disagree about which fires matter."""
    assert "loc.fire_km" in HIT
    assert "km > loc.fire_km" in HIT
    assert "radius_km" not in HIT, (
        "the news radius answers a different question and must not leak in here")


def test_a_place_with_no_ring_is_skipped():
    assert "if (!loc.fire_km) continue;" in HIT


def test_only_wildfires_get_one():
    """An air-quality station is not a fire and has no ring around it."""
    assert 'hazard.kind !== "wildfire"' in HIT


def test_it_survives_locations_not_being_loaded_yet():
    """The map board can open before the places have arrived. Reading .length
    off undefined there would take the whole layer down with it."""
    assert "Array.isArray(LOCATIONS)" in HIT


def test_it_reuses_the_one_haversine():
    """Wildfire Command had five copies of this function and they did not all
    agree. There is one here, and it is already used for the same shape of
    question by withinLocation."""
    assert "haversineKm(" in HIT
    assert not re.search(r"function\s+\w*[Hh]aversine", HIT)


def test_the_nearest_place_is_the_one_named():
    assert "km < best.km" in HIT
    assert "others" in HIT, "a fire in two rings should say so"


# ---------- the marker ----------

def test_a_fire_in_range_is_a_flame_and_everything_else_is_a_dot():
    draw = _fn("  drawHazards()", "\n  },\n")
    assert "fireRingHit(h)" in draw
    assert "icon: fireFlame(" in draw
    assert "L.circleMarker" in draw, "out-of-range fires keep the dot"


def test_the_flame_still_carries_severity():
    """Shape says "near somewhere you watch"; colour says "and this is how bad".
    Dropping the colour to gain the shape would trade one signal for another
    rather than adding one."""
    draw = _fn("  drawHazards()", "\n  },\n")
    assert "fireFlame(style.color)" in draw
    assert "const style = hazardStyle(h);" in draw


def test_the_icons_are_made_once():
    """`moveend` fires continuously through a pan and this runs on every one of
    them; rebuilding an SVG per fire per frame is work nobody sees."""
    assert "_FLAME_ICONS" in APP
    assert "_FLAME_ICONS.has(colour)" in FLAME
    assert "_FLAME_ICONS.set(colour, icon)" in FLAME


def test_it_is_drawn_the_way_the_saved_place_star_is():
    """Same divIcon-with-inline-SVG approach, same paint-order trick so the
    outline rims the shape instead of eating into it, same CSS treatment."""
    assert "L.divIcon(" in FLAME
    assert 'paint-order="stroke"' in FLAME
    rule = re.search(r"\.haz-flame\s*\{[^}]*\}", CSS).group(0)
    assert "background: none" in rule and "drop-shadow" in rule


# ---------- the popup says why ----------

def test_the_popup_leads_with_the_place_it_is_near():
    assert "near.loc.name" in POPUP
    assert "near.km" in POPUP


def test_it_is_still_worded_as_a_reported_point():
    """WFIGS gives an incident point, not a perimeter. "21 km away" would be a
    claim about the nearest flame that nobody here can make."""
    assert "Reported" in POPUP


def test_the_place_name_never_becomes_markup():
    """A location name is the reader's own text, but this file does not hand
    any string to innerHTML and should not start now."""
    assert "innerHTML" not in POPUP


# ---------- and it updates when the rings do, not only when the map moves ----------

def test_changing_a_place_redraws_the_map():
    """The trap this feature has: `_hazKey` stops a re-fetch for the same
    rectangle, so without this a reader would switch a ring on and see nothing
    change until they panned."""
    src = APP[APP.index("  async refresh()"):]
    src = src[:src.index("\n  },\n")]
    assert "MapBoard.drawHazards()" in src
