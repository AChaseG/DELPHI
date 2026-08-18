"""Where everything is, now that the pop-out rail is gone.

The rail was a strip pinned to the right edge of every board, holding search,
Create, Pantheons, Sources, Settings and the account — collapsed to icons,
expanded with a keyboard shortcut. It has been taken apart and its contents put
where each one belongs:

    the four counters   → Settings, under "This instance"
    Sources             → Settings, next to them
    Pantheons           → the tab bar, as a board of its own
    Search, Create      → the header
    Settings, account   → the top-right corner, in that order

What the tests here are really guarding is that nothing was *lost* on the way,
and that the two ends of the header stay where a hand reaches for them. One of
those was found the hard way: the account button used to write the username into
a `.rail-label` span, and with the rail gone that threw — taking the rest of the
header's wiring down with it, so Sources opened nothing at all.
"""
import pathlib
import re

FRONTEND = pathlib.Path(__file__).resolve().parent.parent / "frontend"
HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
APP = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
CSS = (FRONTEND / "css" / "styles.css").read_text(encoding="utf-8")

HEADER = HTML[HTML.index("<header"):HTML.index("</header>")]


def _view_switch_src() -> str:
    start = APP.index("function renderViewSwitch")
    return APP[start:APP.index("function renderBoardHeader")]

SETTINGS = HTML[HTML.index('id="settings-backdrop"'):HTML.index('id="sources-panel"')]


# ---------- the rail is gone, and so is everything that served it ----------

def test_the_rail_is_gone():
    for trace in ('id="action-rail"', 'class="rail-btn"', 'id="btn-rail-toggle"',
                  "rail-label", "rail-icon"):
        assert trace not in HTML, f"{trace} is still in the page"
    assert "action-rail" not in CSS
    assert "wireActionRail" not in APP


def test_nothing_still_reaches_for_a_rail_button():
    """A handler bound to an element that no longer exists throws at boot, and
    the throw takes every line after it — which is how Sources came to open
    nothing at all."""
    for gone in ('el("btn-rail-toggle")', 'el("btn-pantheons")',
                 'el("btn-alerts-panel")', 'el("btn-locations")',
                 '.querySelector(".rail-label")'):
        assert gone not in APP, f"{gone} is still called"


def test_the_account_button_carries_its_name_in_the_tooltip():
    """It is a single glyph in the corner now: a username of any length in the
    label would push the rest of the header along."""
    src = APP[APP.index("function wireAuth"):APP.index("function wireGate")]
    assert 'el("btn-profile").title' in src
    assert "aria-label" in src


# ---------- the tab bar ----------

def test_every_board_is_a_tab():
    switch = HEADER[HEADER.index('class="view-switch"'):]
    ids = re.findall(r'id="(btn-view-[\w-]+)"', switch)
    assert ids == ["btn-view-home", "btn-view-mine", "btn-view-map",
                   "btn-view-pantheons"]


def test_pantheons_is_a_board_not_a_panel():
    assert 'id="pantheons-view"' in HTML
    assert 'id="pantheons-panel"' not in HTML
    assert 'setView("pantheons")' in APP


def test_the_pantheons_board_lists_what_the_panel_listed():
    view = HTML[HTML.index('id="pantheons-view"'):HTML.index("</section>",
                                                             HTML.index('id="pantheons-view"'))]
    for needed in ('id="pn-invites"', 'id="pn-mine"', 'id="pn-public"',
                   'id="btn-create-pantheon"'):
        assert needed in view, f"{needed} did not survive the move"


def test_the_pantheons_board_replaces_the_columns_rather_than_covering_them():
    show = APP[APP.index("function showBoard"):APP.index("\n}\n", APP.index("function showBoard"))]
    assert 'el("pantheons-view").hidden = !pantheons' in show
    assert 'el("board").hidden = !columns' in show


def test_a_pantheon_no_longer_takes_a_tab_of_its_own():
    """Fine at three, a scrolling mess at a dozen — and unnecessary once the
    Pantheons board lists them all with what they are."""
    src = _view_switch_src()
    assert "view-pantheon" not in src
    assert "view-pantheon" not in CSS
    assert "createElement" not in src, "the bar is four boards, built in the HTML"


def test_a_pantheons_board_says_which_one_it_is():
    """Nothing else would. Home and My feeds are named by the lit tab; a
    Pantheon's board had only its tab, and the tab is gone."""
    assert 'id="board-header"' in HTML
    src = APP[APP.index("function renderBoardHeader"):APP.index("let BOARD_GENERATION")]
    assert "p.name" in src and "p.member_count" in src
    assert 'setView("pantheons")' in src, "there has to be a way back"
    assert "PantheonModal.open(p)" in src


def test_the_board_header_is_only_for_pantheons():
    src = APP[APP.index("function renderBoardHeader"):APP.index("let BOARD_GENERATION")]
    assert 'VIEW.startsWith("pantheon:")' in src
    assert "bar.hidden = !p" in src


def test_the_pantheons_tab_stays_lit_inside_one():
    src = APP[APP.index("function updateViewButtons"):APP.index("function renderViewSwitch")]
    assert 'VIEW === "pantheons" || VIEW.startsWith("pantheon:")' in src


# ---------- the corner ----------

def test_the_header_ends_with_help_then_the_account():
    """The corner's two slots, and neither holds what it used to.

    ❓ took the gear's place because help is what somebody needs on the days
    they have built up no habits yet, while Settings is wanted rarely and on
    purpose — a slot costing one click should hold the first. Settings then
    moved onto 👤, because both are about the same person and two corner
    buttons for one subject was one too many.
    """
    actions = HEADER[HEADER.index('class="topbar-actions"'):]
    ids = re.findall(r'id="([\w-]+)"', actions)
    assert ids[-2:] == ["btn-help", "btn-profile"], ids
    assert "btn-settings" not in ids, "the gear is gone, not renamed in place"


def test_the_account_button_opens_settings_rather_than_signing_out():
    """It used to sign out on one click in a corner. Sign-out is destructive
    and now sits behind a deliberate step, inside the panel this opens."""
    src = APP[APP.index("function wireAuth"):]
    src = src[:src.index("\n}\n")]
    assert 'el("btn-profile").onclick = openSettings;' in src
    assert "Session.clear()" not in src, (
        "signing out moved to signOutThisBrowser, called from the Settings row")


def test_signing_out_kept_a_home_and_kept_its_confirm():
    src = APP[APP.index("async function signOutThisBrowser"):]
    src = src[:src.index("\n}\n")]
    assert "confirm(" in src
    assert "Store.clear()" in src, (
        "the cached news is a reading history on a disk that may not be this "
        "person's alone")
    assert 'el("btn-signout").onclick = signOutThisBrowser;' in APP
    assert 'id="btn-signout"' in HEADER or 'id="btn-signout"' in HTML


def test_no_gear_survives_in_the_live_chrome():
    """Every "⚙ Settings" in the help copy pointed at a glyph that is no longer
    anywhere on screen. The changelog is dated history and keeps its own."""
    assert "⚙" not in HTML
    assert "⚙" not in APP


def test_search_and_create_kept_a_home():
    """They were in the rail. Losing either would be losing the only way to
    search everything, and the only way to make a feed."""
    actions = HEADER[HEADER.index('class="topbar-actions"'):]
    assert 'id="global-search"' in actions
    assert 'id="btn-create"' in actions


# ---------- what moved into Settings ----------

def test_the_counters_moved_out_of_the_header():
    assert "stat-row" not in HEADER
    assert 'id="stat-row"' in SETTINGS


def test_all_four_counters_survived():
    for stat in ("stat-articles", "stat-countries", "stat-sources", "stat-alerts"):
        assert f'id="{stat}"' in SETTINGS, f"{stat} was dropped"
    assert 'el("stat-alerts").textContent' in APP, "nothing updates the count"


def test_sources_opens_from_settings():
    assert 'id="btn-sources"' in SETTINGS
    assert 'el("btn-sources").onclick' in APP


# ---------- the Pantheons board ----------

def test_the_two_ways_in_are_on_the_left_of_a_dividing_line():
    """Make one, or join one somebody else made — both on the left; the ones
    you are already in on the right. They are different questions, and the eye
    should not have to work out which list it is reading."""
    view = HTML[HTML.index('id="pantheons-view"'):HTML.index("</section>",
                                                             HTML.index('id="pantheons-view"'))]
    side = view[view.index('class="pn-side"'):view.index('class="pn-main"')]
    assert 'id="btn-create-pantheon"' in side
    assert 'id="pn-public"' in side
    main = view[view.index('class="pn-main"'):]
    assert 'id="pn-mine"' in main and 'id="pn-invites"' in main
    assert re.search(r"\.pn-side\s*\{[^}]*border-right:\s*1px solid var\(--hairline\)", CSS)


def test_the_list_side_is_against_the_wall_and_stays_narrow():
    """It is a button and a list of names, and it was starting a third of the
    way across a 1600px window — the body was a centred 1400px band, so the
    hairline landed mid-page and the tiles were crowded into the last third."""
    body = re.search(r"\.pantheons-body\s*\{[^}]*\}", CSS).group(0)
    assert "margin: 0 auto" not in body, "centring is what pushed it off the wall"
    assert "max-width" not in body
    side = re.search(r"\.pn-side\s*\{[^}]*\}", CSS).group(0)
    assert re.search(r"flex:\s*0 0 20%", side), side
    assert "min-width" in side and "max-width" in side, (
        "a bare percentage is unreadable narrow and sprawling wide")


def test_the_room_kept_for_the_pillars_goes_when_they_do():
    """They are hidden below 1281px; the padding that cleared them was not, so
    the list sat 58px in from a wall with nothing against it."""
    band = CSS[CSS.index(".pantheons-body"):CSS.index(".pn-side")]
    assert "@media (min-width: 1281px)" in band
    assert "var(--pillar-w)" in band.split("@media")[1]


def test_a_joined_pantheon_is_a_button():
    src = APP[APP.index("function pantheonCard"):APP.index("/* ---------- alerts ----------")]
    assert 'document.createElement("button")' in src
    assert 'setView("pantheon:" + p.id)' in src


def test_the_button_says_what_the_pantheon_is():
    """A row of names tells you nothing and makes you open a modal to find
    out."""
    src = APP[APP.index("function pantheonCard"):APP.index("/* ---------- alerts ----------")]
    for fact in ("p.name", "p.description", "p.member_count", "p.owner_name",
                 "p.feed_count", "p.alert_count", "p.role"):
        assert fact in src, f"the tile does not show {fact}"


def test_an_empty_description_still_takes_its_line():
    """Otherwise one tile is shorter than the one beside it for no reason."""
    src = APP[APP.index("function pantheonCard"):APP.index("/* ---------- alerts ----------")]
    assert "No description." in src
    assert re.search(r"\.pn-tile-desc\s*\{[^}]*line-clamp:\s*2", CSS), (
        "a long description would otherwise make its tile twice the height")


def test_manage_does_not_also_open_the_board_behind_it():
    src = APP[APP.index("function pantheonCard"):APP.index("/* ---------- alerts ----------")]
    assert "stopPropagation()" in src
    assert "tabIndex" in src, "a control inside a button still has to be reachable"


# ---------- and the counters fit where they moved to ----------

def test_the_counters_wrap_instead_of_squeezing():
    """Reported from a screenshot: in a 400px panel "1187/5120" was rendering
    as "11875/12"."""
    rule = re.search(r"\.stat-row\s*\{[^}]*\}", CSS).group(0)
    assert "grid" in rule
    tile = re.search(r"\.stat-tile\s*\{[^}]*\}", CSS).group(0)
    assert "min-width: 0" in tile, "a fixed minimum is what clipped the value"


# ---------- and nothing measures itself against a rail that isn't there ----------

def test_the_width_the_rail_reserved_is_now_nothing():
    """Three things sized themselves against it — the board, the map board and
    the right-hand decorative column. One token, set to zero, rather than three
    edits that could disagree."""
    assert re.search(r"--rail-w:\s*0px", CSS)


def test_the_header_still_clears_its_own_corner():
    rule = re.search(r"\.topbar\s*\{[^}]*\}", CSS).group(0)
    assert "padding-right" in rule
