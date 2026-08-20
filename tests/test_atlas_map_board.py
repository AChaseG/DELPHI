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
    show = _block("function showBoard", "\n}\n")
    assert 'el("board").hidden = !columns' in show
    assert 'el("map-view").hidden = !map' in show
    assert 'el("pantheons-view").hidden = !pantheons' in show


def test_the_map_is_told_to_measure_itself_when_shown():
    """Leaflet cannot measure a hidden container. A map built or left while
    display:none comes back the size of nothing until something asks it again."""
    assert "invalidateSize()" in _block("  async open()", after="const MapBoard = {")


def test_the_decorative_columns_get_out_of_the_way():
    """They frame a board of columns. Over a map they are two opaque strips
    across the thing being read."""
    assert "full-board" in _block("function showBoard", "\n}\n")
    assert re.search(r"body\.full-board\s+\.pillar\s*\{[^}]*display:\s*none", CSS)


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


def test_the_rail_no_longer_carries_them():
    """They were in the rail's pop-out on every board, opening panels that no
    longer exist. Atlas is where they live; two buttons pointing at another
    board is a second front door to maintain."""
    assert 'id="btn-alerts-panel"' not in HTML
    assert 'id="btn-locations"' not in HTML
    assert 'el("btn-alerts-panel")' not in APP
    assert 'el("btn-locations")' not in APP


def test_the_unseen_count_moved_with_them():
    """The badge was the only live sign that an alert had fired while you were
    reading something else. Dropping the button must not drop that."""
    tab = HTML[HTML.index('id="btn-view-map"'):]
    tab = tab[:tab.index("</button>")]
    assert 'id="bell-count"' in tab
    assert HTML.count('id="bell-count"') == 1
    assert 'const bc = el("bell-count")' in APP


def test_the_badge_is_not_sliced_by_the_switch():
    """The view switch clips its overflow to keep the buttons joined up, so a
    badge pinned to the corner would be cut in half."""
    rule = re.search(r"\.view-switch\s+\.bell-count\s*\{[^}]*\}", CSS)
    assert rule and "position: static" in rule.group(0)


def test_opening_locations_still_lands_on_the_board():
    """Everything else that says "show me the locations" — the empty state, a
    toast, anything added later — goes through this."""
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

def test_moving_the_map_rerenders_both_panes():
    """Each pane is a reading of the viewport, so both follow it — the alerts
    tab's in-view groups and the locations tab's in-view/elsewhere split."""
    init = _block("  async _init()")
    assert 'this.map.on("moveend"' in init
    assert "this.renderInView()" in init
    assert "LocationsPanel.renderList()" in init


def test_only_what_is_inside_the_viewport_is_listed():
    view = _block("  renderInView()")
    assert "this.map.getBounds()" in view
    assert "bounds.contains" in view
    assert "ALERT_HITS" in view


def test_the_alerts_tab_is_a_feed_and_nothing_else():
    """It used to group its hits under the watched place each one landed in,
    which put half of the locations tab on the alerts tab. Flat, newest-first
    rows — the same ones the board draws — and no place in sight."""
    view = _block("  renderInView()")
    assert "articleRow(" in view
    assert "withinLocation(" not in view, "that is the locations tab's question"
    assert "LOCATIONS" not in view
    assert "published_at" in view, "a feed is newest-first"


def test_each_row_says_which_alert_found_it():
    view = _block("  renderInView()")
    assert "alert.name" in view


def test_the_locations_tab_is_a_feed_of_what_was_reported_inside_them():
    """The complaint was that this tab had nothing on it but a form. It is a
    column now: the circles in view become the criteria, which is the same
    question the 📍 Favourite Locations column asks."""
    feed = _block("  async renderLocationFeed()")
    assert "API.search(" in feed
    assert '"Circle"' in feed and "radius_km" in feed
    assert "articleRow(" in feed
    assert "ALERT_HITS" not in feed, "that is the alerts tab's question"


def test_the_locations_feed_follows_the_map_without_asking_twice():
    """Panning fires this continuously; the same circles must not be re-queried
    on every frame."""
    feed = _block("  async renderLocationFeed()")
    assert "this._locKey" in feed
    assert "this._locSeq" in feed, "a later pan has to win the race"


def test_each_tab_loads_only_its_own_subject():
    pane = _block("  showPane(which)")
    assert 'if (which === "alerts") { renderAlertsPanel(); this.renderInView(); }' in pane
    assert "renderLocationFeed()" in pane
    move = _block("  async _init()")
    assert 'if (this.pane === "alerts") this.renderInView();' in move


def test_the_panes_say_what_they_are_showing():
    assert "alert hit" in _block("  renderInView()")
    assert "watched place" in _block("  async renderLocationFeed()")


# ---------- two tabs, and each carries its own subject whole ----------

def test_there_are_two_tabs_one_per_thing():
    tabs = re.findall(r'<button data-pane="(\w+)"', HTML)
    assert tabs == ["alerts", "locations"]


def test_the_alerts_tab_opens_first():
    assert 'data-pane="alerts" class="active"' in HTML
    assert 'pane: "alerts"' in APP


def test_an_alert_is_created_from_the_alerts_tab():
    assert 'id="btn-new-alert"' in HTML
    assert 'el("btn-new-alert").onclick = () => openBuilder("alert")' in APP


def test_a_location_is_created_from_the_locations_tab():
    assert 'id="btn-new-location"' in HTML
    wire = _block("function wireMapBoard", "\n}\n")
    assert 'el("btn-new-location").onclick' in wire
    assert 'el("loc-create").hidden = false' in wire
    assert 'el("loc-search").focus()' in wire


def test_the_location_form_is_put_away_until_it_is_wanted():
    """A half-filled form for a place nobody has picked is furniture."""
    assert 'id="loc-create" hidden' in HTML
    assert 'el("loc-create").hidden = false' in _block("  setPoint(lat, lon, name, country)")
    assert 'el("loc-create").hidden = true' in _block("  cancelEdit()")


def test_clicking_the_map_still_opens_the_form_on_the_right_tab():
    init = _block("  async _init()")
    assert 'this.showPane("locations")' in init
    assert 'el("loc-name").focus()' in init


def test_the_alerts_tab_carries_the_hits_and_the_alerts():
    """In-view groups above, the alert list with its controls below — one tab
    is the whole subject, or the reader has to hunt for the half of it."""
    pane = HTML[HTML.index('data-pane="alerts">'):HTML.index('data-pane="locations" hidden')]
    assert 'id="inview-list"' in pane
    assert 'id="alerts-list"' in pane
    assert 'id="btn-new-alert"' in pane


def test_the_locations_tab_lists_in_view_and_elsewhere():
    """Split rather than filtered: a saved place you cannot find because you
    panned away from it is a place you cannot delete either."""
    pane = HTML[HTML.index('data-pane="locations" hidden'):]
    assert 'id="loc-list"' in pane and 'id="loc-elsewhere"' in pane
    listing = _block("  renderList()")
    assert "MapBoard.map.getBounds()" in listing
    assert "bounds.contains" in listing
    assert 'el("loc-elsewhere-head").hidden' in listing


# ---------- what is where in a pane ----------

def test_each_fold_out_sits_above_its_feed():
    """A feed is long. Anything a reader goes looking for — the alert to pause,
    the place to rename — has to be reachable without scrolling past forty
    articles first."""
    alerts = HTML[HTML.index('data-pane="alerts">'):HTML.index('data-pane="locations" hidden')]
    assert alerts.index('id="alerts-manage"') < alerts.index('id="inview-list"')
    locations = HTML[HTML.index('data-pane="locations" hidden"'.rstrip('"')):]
    assert locations.index('id="loc-manage"') < locations.index('id="loc-feed"')


def test_the_edit_form_is_next_to_the_list_that_opens_it():
    """Reported as "Edit does the same thing as Show". It didn't — the form
    filled in correctly — but the form was at the top of the pane and the list
    at the bottom, so with a feed between them the only visible effect was the
    map recentring, which is what Show does."""
    locations = HTML[HTML.index('data-pane="locations" hidden'):]
    assert (locations.index('id="loc-manage"')
            < locations.index('id="loc-create"')
            < locations.index('id="loc-feed"'))


def test_starting_an_edit_puts_the_form_in_front_of_you():
    edit = _block("  startEdit(loc)")
    assert "scrollIntoView(" in edit
    assert "name.focus()" in edit
    assert "name.select()" in edit, (
        "renaming is the common case; typing should replace what is there")


# ---------- the ground it is read on ----------

def test_there_is_more_than_one_base_map():
    """A city block, a coastline and a mountain range are not best read on the
    same ground, and a light map is the brightest thing on a dark dashboard."""
    maps = _block("function baseMaps", "\n}\n")
    names = set(re.findall(r"^\s{4}(\w+): L\.tileLayer", maps, re.M))
    assert {"Street", "Muted", "Night", "Terrain", "Satellite"} <= names


def test_every_base_map_carries_its_attribution():
    """It is the condition each of these is offered under, not decoration."""
    maps = _block("function baseMaps", "\n}\n")
    assert maps.count("attribution:") == maps.count("L.tileLayer(")


def test_none_of_them_needs_a_key():
    """A key in the page is a key anybody can read."""
    maps = _block("function baseMaps", "\n}\n")
    for suspicious in ("apikey", "api_key", "access_token", "?key="):
        assert suspicious not in maps.lower()


def test_the_choice_is_remembered():
    """Which map somebody reads best on is a fact about their eyes and their
    screen, so it is remembered per browser. The control moved out of Leaflet's
    floating box and into the panel on the right; the remembering did not."""
    init = _block("  async _init()")
    assert 'localStorage.getItem("gnd_basemap")' in init
    panel = _block("  renderLayerPanel()")
    assert 'localStorage.setItem("gnd_basemap"' in panel
    assert 'input.type = "radio"' in panel, "one ground at a time"


def test_the_board_reads_left_to_right_feed_map_switches():
    """Three columns, in the order somebody uses them: what was found, the map
    it was found on, then the controls deciding what the map draws. The feed
    leads because it is the thing being read; the switches are furniture and
    sit at the far edge."""
    view = HTML[HTML.index('id="map-view"'):HTML.index("</section>",
                                                        HTML.index('id="map-view"'))]
    order = [m for m in re.findall(r'id="(map-side|world-map|map-layers)"', view)]
    assert order == ["map-side", "world-map", "map-layers"], order


def test_the_layers_panel_offers_the_grounds_and_the_overlays():
    view = HTML[HTML.index('id="map-layers"'):]
    view = view[:view.index("</aside>")]
    assert 'id="layer-bases"' in view and 'id="layer-overlays"' in view
    assert 'role="radiogroup"' in view, "one ground at a time"


def test_the_flanks_narrow_before_anything_wraps():
    """A wrapped flex row divides the container's height between its lines,
    which turned the map into a two-pixel sliver the first time this was tried.
    So the intermediate breakpoint only takes width off the two flanks."""
    rule = re.search(r"@media \(max-width: 1180px\) \{.*?\n\}", CSS, re.S)
    assert rule, "no intermediate breakpoint for the three-column board"
    assert "flex-wrap" not in rule.group(0)
    assert ".map-layers" in rule.group(0) and ".map-side" in rule.group(0)


def test_when_it_stacks_the_switches_go_last():
    """The container is a column at this width, which is what makes `order` do
    the obvious thing rather than merely reshuffling a row."""
    rule = re.search(r"@media \(max-width: 820px\) \{.*?\n\}", CSS, re.S).group(0)
    assert "flex-direction: column" in rule
    assert "order: 3" in rule, "the switches are the one that can be a strip"


def test_nothing_floats_over_the_map_any_more():
    """The pop-out rail is gone entirely — see tests/test_chrome_layout.py.
    What is left is that the map board reserves nothing for it."""
    assert "action-rail" not in CSS
    assert "action-rail" not in HTML


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
