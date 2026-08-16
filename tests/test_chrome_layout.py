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
SETTINGS = HTML[HTML.index('id="settings-panel"'):HTML.index('id="sources-panel"')]


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


# ---------- the corner ----------

def test_the_header_ends_with_settings_then_the_account():
    actions = HEADER[HEADER.index('class="topbar-actions"'):]
    ids = re.findall(r'id="([\w-]+)"', actions)
    assert ids[-2:] == ["btn-settings", "btn-profile"], ids


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


# ---------- and nothing measures itself against a rail that isn't there ----------

def test_the_width_the_rail_reserved_is_now_nothing():
    """Three things sized themselves against it — the board, the map board and
    the right-hand decorative column. One token, set to zero, rather than three
    edits that could disagree."""
    assert re.search(r"--rail-w:\s*0px", CSS)


def test_the_header_still_clears_its_own_corner():
    rule = re.search(r"\.topbar\s*\{[^}]*\}", CSS).group(0)
    assert "padding-right" in rule
