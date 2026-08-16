"""A saved location gets a star at the point it was saved on.

The map used to draw a ring and nothing else. That is fine for a 25 km radius
on a zoomed-in map and useless everywhere else: at a wide radius the circle has
no visible centre, at a low zoom a small one is smaller than a pixel and
disappears entirely, and two overlapping rings had no way to say which was
which. So the place itself is marked.

A star rather than a pin, because the map already drops a pin for the point
being *picked* — the two states have to look different at a glance — and it is
centred on the coordinate rather than balanced on a tip, because a saved
location is that point rather than something standing on it.
"""
import pathlib
import re

FRONTEND = pathlib.Path(__file__).resolve().parent.parent / "frontend"
APP = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
CSS = (FRONTEND / "css" / "styles.css").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    """The source of one function or object method, ending where it ends.

    Bounded properly rather than by a character count: a slice that overruns
    into the next method makes "this one does not mention X" pass or fail on
    whether the neighbour mentions it.
    """
    start = APP.index(name)
    end = APP.find("\n  },\n", start)                 # an object method
    close = APP.find("\n}\n", start)                  # a plain function
    if end == -1 or (close != -1 and close < end):
        end = close
    assert end > start, f"could not find the end of {name}"
    return APP[start:end]


# ---------- every saved location is marked ----------

def test_saving_a_location_puts_a_star_on_the_map():
    saved = _fn("  renderSaved()")
    assert "L.marker(" in saved
    assert "locationStar(" in saved
    assert "for (const loc of LOCATIONS)" in saved


def test_the_star_sits_on_the_point_not_above_it():
    """A pin's tip marks the spot and its body sits above; a star centred on
    the coordinate *is* the spot. Anchor must be half the icon."""
    icon = _fn("function locationStar")
    size = re.search(r"iconSize:\s*\[(\d+),\s*(\d+)\]", icon)
    anchor = re.search(r"iconAnchor:\s*\[(\d+),\s*(\d+)\]", icon)
    assert size and anchor
    w, h = int(size.group(1)), int(size.group(2))
    assert (int(anchor.group(1)), int(anchor.group(2))) == (w // 2, h // 2)


def test_a_star_says_which_place_it_is():
    """Two rings that overlap were indistinguishable; two stars would be too."""
    saved = _fn("  renderSaved()")
    assert "bindTooltip(" in saved
    assert "loc.name" in saved
    assert "loc.radius_km" in saved


def test_the_ring_is_still_drawn():
    """The star says where; the ring says how far. Neither replaces the other."""
    saved = _fn("  renderSaved()")
    assert "L.circle(" in saved and "radius: loc.radius_km * 1000" in saved


# ---------- and it is not the same mark as an unsaved point ----------

def test_the_point_being_picked_is_not_a_star():
    """Saved and not-yet-saved have to be told apart on the map, or the reader
    cannot see whether the thing they just clicked is kept."""
    pending = _fn("  _drawPending()")
    assert "locationStar" not in pending, (
        "the pending point uses the default pin; only saved places get a star")


# ---------- it stays legible ----------

def test_the_star_carries_the_locations_own_colour():
    icon = _fn("function locationStar")
    assert "LOC_COLOURS" in icon
    saved = _fn("  renderSaved()")
    assert "locationStar(loc.color)" in saved


def test_an_unknown_colour_falls_back_to_gold_rather_than_vanishing():
    icon = _fn("function locationStar")
    assert "LOC_COLOURS.gold" in icon
    assert re.search(r"/\^#\[0-9a-f\]\{6\}\$/i", icon), (
        "a hex colour saved by hand should still be honoured")


def test_the_star_is_outlined_so_it_reads_on_a_pale_tile():
    """Gold on the light end of an OpenStreetMap tile is nearly nothing without
    something behind it."""
    icon = _fn("function locationStar")
    assert 'stroke="#12140f"' in icon
    assert "paint-order" in icon, (
        "without paint-order the stroke eats into the star's points")


def test_leaflets_white_box_is_turned_off():
    """A div icon is a bordered white square by default, which would frame
    every star in a little card."""
    rule = re.search(r"\.loc-star\s*\{[^}]*\}", CSS)
    assert rule, "the star's icon class has no styling"
    assert "background: none" in rule.group(0)
    assert "border: 0" in rule.group(0)


def test_the_icon_is_drawn_not_downloaded():
    """An <img> marker is another request on a panel that already waits for
    Leaflet and a tile server."""
    icon = _fn("function locationStar")
    assert "L.divIcon(" in icon
    assert "<svg" in icon


def test_identical_stars_are_not_rebuilt_per_location():
    """Someone with forty watched places redraws this list on every save."""
    icon = _fn("function locationStar")
    assert "_STAR_ICONS" in icon and "has(fill)" in icon
