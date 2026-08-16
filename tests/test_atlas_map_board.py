"""Atlas: one map board, and a pane that only shows what the map is looking at.

The map used to be two maps, both of them hidden inside side panels — one in
Locations for picking a place, one in Alerts, off by default, for seeing where
hits landed. Neither could show the other's layer, which made the question
worth asking impossible to ask: *did that alert fire inside somewhere I watch?*

So there is one map, it is a board rather than a panel, and the pane beside it
is tied to the viewport. Panning is the filter. The pane groups what it finds by
the watched place it landed in, because "an alert fired" and "an alert fired
inside the harbour you watch" are different pieces of news.

Driven in a browser while it was built: two places 8,000 km apart, four seeded
hits, and the pane checked at four different viewports. These tests hold the
parts of that which can rot quietly.
"""
import pathlib
import re

FRONTEND = pathlib.Path(__file__).resolve().parent.parent / "frontend"
HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
APP = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
CSS = (FRONTEND / "css" / "styles.css").read_text(encoding="utf-8")


def _block(marker: str, end: str = "\n  },\n", after: str = "") -> str:
    """One method's source. `after` picks which object's copy: MapBoard and
    LocationsPanel both have an `open()`, and the first one in the file is not
    always the one under test."""
    start = APP.index(marker, APP.index(after) if after else 0)
    stop = APP.find(end, start)
    assert stop > start, f"could not find the end of {marker}"
    return APP[start:stop]


# ---------- it is a board, not a panel ----------

def test_the_board_switch_offers_it():
    assert 'id="btn-view-map"' in HTML
    assert "🗺 Atlas" in HTML


def test_choosing_it_replaces_the_board_rather_than_floating_over_it():
    show = _block("function showMapBoard", "\n}\n")
    assert 'el("board").hidden = on' in show
    assert 'el("map-view").hidden = !on' in show


def test_the_map_is_told_to_measure_itself_when_shown():
    """Leaflet cannot measure a hidden container. A map built or left while
    display:none comes back the size of nothing until something asks it again."""
    assert "invalidateSize()" in _block("  async open()", after="const MapBoard = {")


def test_the_decorative_columns_get_out_of_the_way():
    """They frame a board of columns. Over a map they are two opaque strips
    across the thing being read."""
    assert "map-board" in _block("function showMapBoard", "\n}\n")
    assert re.search(r"body\.map-board\s+\.pillar\s*\{[^}]*display:\s*none", CSS)


# ---------- the alerts and the places moved here ----------

def test_both_side_panels_are_gone():
    assert 'id="alerts-panel"' not in HTML
    assert 'id="locations-panel"' not in HTML


def test_there_is_exactly_one_map_container():
    """Two maps of the same world is one too many, and was the whole problem:
    neither could show the other's layer."""
    assert 'id="world-map"' in HTML
    assert 'id="alerts-map"' not in HTML
    assert 'id="loc-map"' not in HTML
    assert HTML.count('class="world-map"') == 1


def test_the_alerts_and_locations_panes_live_in_the_board():
    side = HTML[HTML.index('id="map-side"'):HTML.index("</section>", HTML.index('id="map-side"'))]
    for needed in ('id="alerts-list"', 'id="loc-list"', 'id="loc-name"',
                   'id="loc-search"', 'id="btn-loc-save"', 'id="inview-list"'):
        assert needed in side, f"{needed} did not move into the map board"


def test_the_rail_leads_to_the_board():
    """The bell and the pin used to open panels that no longer exist."""
    assert 'setView("map"); MapBoard.showPane("alerts")' in APP
    assert 'el("btn-locations").onclick = () => LocationsPanel.open()' in APP
    assert 'setView("map")' in _block("  async open()", "\n  },\n")


def test_the_alert_layers_are_drawn_on_the_one_map():
    draw = _block("  drawAlerts(eventsByAlert)")
    assert "this.alerts.addLayer" in draw
    assert "L.circleMarker" in draw          # the hits
    assert "geo.radius_km * 1000" in draw    # the alerts' own geofences


def test_watched_places_and_hits_are_separate_layers():
    """So one can be redrawn without wiping the other — saving a place must not
    clear the hits, and an alert firing must not clear the stars."""
    init = _block("  async _init()")
    assert "this.locations = L.featureGroup()" in init
    assert "this.alerts = L.featureGroup()" in init
    assert "LocationsPanel.saved = this.locations" in init


# ---------- the pane is tied to the map ----------

def test_moving_the_map_rerenders_the_pane():
    init = _block("  async _init()")
    assert re.search(r'this\.map\.on\("moveend",\s*\(\)\s*=>\s*this\.renderInView\(\)\)', init)


def test_only_what_is_inside_the_viewport_is_listed():
    view = _block("  renderInView()")
    assert "this.map.getBounds()" in view
    assert "bounds.contains" in view
    assert "LOCATIONS.filter" in view and "ALERT_HITS.filter" in view


def test_hits_are_grouped_by_the_watched_place_they_landed_in():
    view = _block("  renderInView()")
    assert "withinLocation(" in view
    assert "elsewhere" in view, (
        "a hit inside the viewport but outside every watched place still has to "
        "appear somewhere, or the pane quietly loses it")


def test_a_hit_inside_two_overlapping_places_belongs_to_both():
    """Stopping at the first match would make the second place look quiet."""
    view = _block("  renderInView()")
    assert "placed = true" in view and "break" not in view.split("for (const p of places)")[1][:200]


def test_the_hits_are_collected_even_when_the_alerts_pane_is_not_showing():
    """The "in view" pane is built out of them, and it is the pane that opens
    first."""
    open_src = _block("  async open()", after="const MapBoard = {")
    assert "renderAlertsPanel()" in open_src
    assert "renderInView()" in open_src


def test_each_row_says_which_alert_found_it():
    group = _block("  _group(title, meta, hits, loc)")
    assert "alert.name" in group


def test_the_pane_says_what_it_is_showing():
    view = _block("  renderInView()")
    assert "watched place" in view and "alert hit" in view


# ---------- and it stays usable ----------

def test_the_pane_wraps_rather_than_clipping():
    """Caught in a screenshot: a fixed-width pane cut "300 km" to "300 kr"."""
    assert re.search(r"\.map-pane\s+\.alert-head\s*\{[^}]*flex-wrap:\s*wrap", CSS)


def test_the_map_and_the_pane_share_the_board_area():
    rule = re.search(r"\.map-view\s*\{[^}]*\}", CSS).group(0)
    assert "display: flex" in rule
    assert "flex: 1 1 auto" in rule and "min-height: 0" in rule
    assert "var(--rail-w)" in rule, "the action rail would sit over the pane"


def test_it_stacks_instead_of_squeezing_on_a_narrow_window():
    assert re.search(r"@media\s*\(max-width:\s*820px\)\s*\{[^}]*\.map-view\s*\{"
                     r"[^}]*flex-direction:\s*column", CSS, re.S)


def test_clicking_the_map_still_starts_a_new_place():
    init = _block("  async _init()")
    assert "LocationsPanel.setPoint(e.latlng.lat, e.latlng.lng)" in init
    assert 'this.showPane("locations")' in init
