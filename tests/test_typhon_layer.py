"""Typhon on the map: an overlay, not a third pane.

The hazard layer had two plausible shapes. A third tab beside Alerts and
Locations would have meant lockstep edits across the markup, the CSS, the
pane-switching branch and this suite's sibling file — and it would have put a
list of fires where a reader goes to read the news. An overlay is the shape
that fits: it is drawn on the map, it carries what it knows in its popups, and
it is switched on and off from the control Leaflet already puts in the corner.

The layers control's overlay argument had been `{}` since Atlas was built.
Filling it is the whole of the wiring.

Three things here are load-bearing rather than cosmetic:

  · the layer is **off unless it was left on**. It covers the United States,
    and most readers are not there. A layer that opens on an empty map reads as
    a broken feature rather than an absent one, which is why its extent is in
    its own name.
  · **nothing is fetched while it is switched off.** The map fires `moveend`
    continuously during a pan, and a request per frame for a layer nobody is
    looking at is the kind of cost that never shows up in a screenshot.
  · **incident names never reach innerHTML.** They are third-party strings and
    this file has been consistent about that since the alert popups.
"""
import pathlib
import re

FRONTEND = pathlib.Path(__file__).resolve().parent.parent / "frontend"
HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
APP = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
API = (FRONTEND / "js" / "api.js").read_text(encoding="utf-8")
CSS = (FRONTEND / "css" / "styles.css").read_text(encoding="utf-8")


def _fn(marker: str, end: str = "\n  },\n") -> str:
    start = APP.index(marker)
    stop = APP.find(end, start)
    assert stop > start, f"could not find the end of {marker}"
    return APP[start:stop]


INIT = _fn("  async _init()")


# ---------- it is an overlay ----------

def test_the_overlays_argument_is_no_longer_empty():
    """`L.control.layers(bases, {}, …)` was the state of things until Typhon."""
    assert not re.search(r"L\.control\.layers\(bases,\s*\{\}", APP), (
        "the overlays argument is still empty")
    assert "L.control.layers(bases, overlays" in INIT


def test_the_layer_is_named_typhon_and_says_where_it_covers():
    assert "🐉 Typhon" in APP
    assert re.search(r'TYPHON_LAYER\s*=\s*"[^"]*\(US\)"', APP), (
        "a layer that covers one country has to say so in its own name")


def test_there_is_no_third_pane():
    """The map tabs stay two. A third would be lockstep edits across four
    files, and a list of fires is not what the pane is for."""
    # The tab bar is a <nav>; the panes below it carry data-pane too, so the
    # slice has to end at the nav's own close rather than the first </div>.
    start = HTML.index('id="map-tabs"')
    tabs = HTML[start:HTML.index("</nav>", start)]
    assert len(re.findall(r"data-pane=", tabs)) == 2, tabs
    assert "hazard" not in tabs.lower()
    assert "typhon" not in tabs.lower()


# ---------- off by default, remembered after that ----------

def test_it_is_not_added_to_the_map_unless_it_was_left_on():
    assert 'localStorage.getItem("gnd_overlay_hazards") === "1"' in INIT
    assert "this.hazards = L.featureGroup();" in INIT, (
        "built, but not .addTo(this.map) like locations and alerts are")


def test_the_choice_is_remembered_the_way_the_basemap_is():
    assert 'this.map.on("overlayadd"' in INIT
    assert 'this.map.on("overlayremove"' in INIT
    assert 'localStorage.setItem("gnd_overlay_hazards"' in INIT


def test_switching_it_on_fetches_immediately():
    """Nothing was fetched while it was off, so the cached-rectangle guard
    would otherwise hold the layer empty until the reader panned."""
    assert 'this._hazKey = ""' in INIT
    assert "this.refreshHazards()" in INIT


# ---------- what it costs while nobody is looking ----------

REFRESH = _fn("  async refreshHazards()")


def test_a_switched_off_layer_costs_nothing():
    assert "if (!this.map.hasLayer(this.hazards)) return;" in REFRESH


def test_panning_does_not_reask_for_the_same_rectangle():
    """`moveend` fires continuously through a pan."""
    assert "if (bbox === this._hazKey) return;" in REFRESH
    assert "Math.round" in REFRESH, "an unrounded bbox never matches twice"


def test_a_slow_answer_cannot_repaint_a_map_the_reader_has_left():
    assert "++this._hazSeq" in REFRESH
    assert "if (generation !== this._hazSeq) return;" in REFRESH


def test_every_generation_counter_is_declared_with_a_number():
    """The bug this caught, which no other test here could have.

    `++this._hazSeq` on a property that was never declared is `++undefined`,
    which is NaN — and `NaN !== NaN` is true, so the guard above rejects every
    answer including the one it just asked for. The layer wired up correctly,
    fetched correctly, returned 200, and drew nothing at all. Every assertion
    in this file still passed, because they read the source rather than run it.

    So: any counter incremented with ++ has to be initialised on the object.
    """
    # Search for the end *from* the start, not from the top of the file:
    # LocationsPanel has an `open()` too and it comes first, which slices an
    # empty string and passes whatever it is asked.
    start = APP.index("const MapBoard = {")
    board = APP[start:APP.index("  async open()", start)]
    assert "_locSeq" in board, "the slice missed MapBoard's own declarations"
    for name in set(re.findall(r"\+\+this\.(_\w*Seq)", APP)):
        assert re.search(rf"{name}:\s*0\b", board), (
            f"{name} is incremented but never initialised — ++undefined is NaN, "
            f"and every comparison against NaN is false")


def test_a_failed_fetch_lets_the_next_pan_try_again():
    """Otherwise the cached key pins the layer empty until a reload."""
    catch = REFRESH[REFRESH.index("} catch"):]
    assert 'this._hazKey = ""' in catch


def test_the_overlay_refreshes_whichever_pane_is_open():
    """It is drawn on the map, so it has nothing to do with the pane."""
    moveend = APP[APP.index('this.map.on("moveend"'):]
    moveend = moveend[:moveend.index("});")]
    assert "this.refreshHazards();" in moveend


# ---------- the popup ----------

POPUP = APP[APP.index("function hazardPopup"):APP.index("\n}\n", APP.index("function hazardPopup"))]


def test_an_incident_name_never_becomes_markup():
    assert "innerHTML" not in POPUP
    assert "createElement(" in POPUP and "textContent" in POPUP


def test_the_popup_credits_the_agency():
    """A number on a map with no source is worth less than no number."""
    assert "NIFC" in POPUP
    assert re.search(r"\.haz-popup \.haz-attr\s*\{", CSS)


def test_the_popup_carries_what_a_reader_needs_first():
    for fact in ("acres", "containment", "cause"):
        assert fact in POPUP, f"the popup does not show {fact}"


# ---------- severity reads as severity ----------

STYLE = APP[APP.index("function hazardStyle"):APP.index("\n}\n", APP.index("function hazardStyle"))]


def test_a_worse_hazard_is_bigger_and_redder():
    assert "radius" in STYLE and "severity" in STYLE
    assert STYLE.index("80") < STYLE.index("20"), "bands should read worst-first"


# ---------- and the client asks the server, not itself ----------

def test_the_layer_asks_for_a_rectangle():
    assert "hazards: (bbox" in API
    assert "/api/hazards?bbox=" in API
    assert "encodeURIComponent(bbox)" in API


def test_it_is_bounded():
    """A map zoomed out to the whole country must not try to draw everything."""
    assert re.search(r"TYPHON_MAX\s*=\s*\d+", APP)
    assert "API.hazards(bbox, TYPHON_MAX)" in REFRESH
