"""Settings is a dialog in the middle of the screen, in five sections.

It was a side panel holding one column of every setting Delphi has: appearance,
language, units, clocks, feeds, notifications, help, billing, the account, the
instance's own health and the operator console, in a single run you scrolled
past to reach the one you came for. The list had outgrown the shape.

Three things this file guards, and the third is the interesting one:

  · **the middle of the screen, not the side.** A drawer is for something you
    consult while still reading. Settings is the thing you are doing, so it
    takes the middle and the rest waits.
  · **sections, and every setting kept one.** A reorganisation that quietly
    drops a control is worse than the wall it replaced.
  · **it opens on What's new, every time.** Not a remembered tab, not the first
    one alphabetically — a deliberate choice, because that section is the only
    part of this dialog with something to say the reader does not already know,
    and everything else here is looked *for* rather than stumbled upon.
"""
import pathlib

import pytest

FRONTEND = pathlib.Path(__file__).resolve().parent.parent / "frontend"
HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
APP = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
CSS = (FRONTEND / "css" / "styles.css").read_text(encoding="utf-8")

DIALOG = HTML[HTML.index('id="settings-backdrop"'):HTML.index('id="sources-panel"')]
TABS = ("updates", "display", "feeds", "account", "instance")


def _fn(name: str, end: str = "\n}\n") -> str:
    start = APP.index(name)
    return APP[start:APP.find(end, start)]


# ---------- a dialog, in the middle ----------

def test_it_is_a_modal_and_no_longer_a_drawer():
    assert 'id="settings-backdrop"' in HTML
    assert 'class="modal-backdrop centered"' in DIALOG
    assert 'id="settings-panel"' not in HTML, "the side panel is gone, not hidden"
    assert "settings-panel" not in APP, (
        "a handler bound to an element that no longer exists throws at boot and "
        "takes every line after it with it")


def test_it_is_announced_as_a_dialog():
    assert 'role="dialog"' in DIALOG
    assert 'aria-modal="true"' in DIALOG
    assert 'aria-labelledby="settings-title"' in DIALOG
    assert 'id="settings-title"' in DIALOG


def test_centring_is_a_class_rather_than_a_change_to_every_modal():
    """The long reading dialogs — help, the release notes — are right to
    top-align: their content decides their height. This one caps its own height
    and scrolls inside, so it should sit in the middle."""
    assert ".modal-backdrop.centered" in CSS
    assert "align-items: center" in CSS[CSS.index(".modal-backdrop.centered"):
                                        CSS.index(".modal-backdrop.centered") + 200]


def test_the_dialog_caps_its_height_and_scrolls_inside():
    """Five sections of wildly different length would otherwise make the box
    jump between a third of the screen and all of it on every tab press."""
    block = CSS[CSS.index(".settings-modal {"):]
    block = block[:block.index("}")]
    assert "max-height" in block
    assert "flex-direction: column" in block
    assert ".settings-modal .settings-tab" in CSS
    assert "overflow-y: auto" in CSS[CSS.index(".settings-modal .settings-tab"):
                                     CSS.index(".settings-modal .settings-tab") + 200]


def test_it_closes_the_three_ways_a_dialog_should():
    src = _fn("function wireSettings")
    assert 'el("btn-close-settings").onclick = closeSettings;' in src
    assert 'el("settings-backdrop").addEventListener("mousedown"' in src
    assert 'id="btn-close-settings"' in DIALOG


def test_the_operator_console_still_does_not_stack_on_top_of_it():
    assert "closeSettings();" in _fn("function wireAdmin")


# ---------- five sections, and nothing lost on the way ----------

@pytest.mark.parametrize("tab", TABS)
def test_every_section_has_a_button_and_a_body(tab):
    assert f'data-tab="{tab}"' in DIALOG
    assert DIALOG.count(f'data-tab="{tab}"') == 2, (
        f"{tab} needs exactly one tab button and one body")


def test_only_the_first_section_starts_visible():
    """Four of the five bodies ship hidden; showSettingsTab does the rest. A
    body that forgot its `hidden` would flash on screen before the handler
    runs."""
    bodies = DIALOG.count('class="modal-body settings-tab"')
    hidden = DIALOG.count('class="modal-body settings-tab" data-tab')
    assert DIALOG.count("settings-tab") >= len(TABS)
    for tab in TABS[1:]:
        marker = f'class="modal-body settings-tab" data-tab="{tab}" hidden'
        assert marker in DIALOG, f"{tab}'s body should ship hidden"


@pytest.mark.parametrize("control", [
    # Display
    "set-theme", "set-compact", "lang-select", "set-units", "set-timefmt",
    # Feeds & alerts
    "btn-sources", "set-stale", "set-toast-pos", "set-volume", "set-vol-val",
    "btn-test-notif", "set-desktop",
    # Account
    "set-billing", "set-billing-state", "set-billing-note", "btn-manage-sub",
    "btn-subscribe", "set-invite-row", "set-invite", "set-invite-status",
    "btn-redeem-invite", "btn-change-password", "btn-signout", "btn-signout-all",
    # Instance
    "stat-row", "stat-articles", "stat-countries", "stat-sources", "stat-alerts",
    "stat-hazards", "admin-setting", "btn-admin",
])
def test_no_control_was_lost_in_the_rearrangement(control):
    """The failure mode of any reorganisation: a setting that quietly does not
    come out the other side. Every id the old panel carried is still here."""
    assert f'id="{control}"' in DIALOG, f"{control} did not survive"


def test_the_tabs_are_a_row_of_their_own():
    """Five sections will not sit on one line beside a heading and a close
    button at any width worth designing for."""
    assert 'id="settings-tabs"' in DIALOG
    start = DIALOG.index('<div class="modal-head">')
    head = DIALOG[start:DIALOG.index("</div>", start)]
    assert "settings-tabs" not in head, "the tabs sit below the head row, not in it"
    assert DIALOG.index("</div>", start) < DIALOG.index('id="settings-tabs"')
    assert ".settings-tabs" in CSS


# ---------- help left, because it has its own door now ----------

@pytest.mark.parametrize("gone", ["btn-open-faq", "btn-whats-new", "btn-open-trouble"])
def test_the_help_buttons_are_gone_from_settings(gone):
    """How-to, FAQ and Troubleshooting are three tabs of one dialog that ❓ in
    the corner opens in a single press. A second door into the same room, from
    a dialog about something else, is a row nobody needed."""
    assert f'id="{gone}"' not in HTML
    assert gone not in APP, "and nothing is left wiring a button that is not there"


def test_help_itself_is_untouched():
    """Removed from Settings, not removed."""
    assert 'id="faq-backdrop"' in HTML
    assert 'el("btn-help").onclick = () => openHelp("howto");' in APP
    for tab in ("howto", "faq", "trouble"):
        assert f'data-tab="{tab}"' in HTML


# ---------- and it opens on What's new ----------

def test_opening_settings_always_lands_on_whats_new():
    """Not a remembered tab. What's new is the only section with something to
    say the reader does not already know; the rest are looked for, and a
    section somebody went looking for is one click away."""
    src = _fn("async function openSettings")
    assert 'showSettingsTab("updates");' in src
    assert src.index('showSettingsTab("updates")') < src.index("hidden = false")


def test_the_release_notes_are_fetched_on_open_not_held_from_page_load():
    """A copy taken at page load is the stale one on exactly the day somebody
    opens this to find out what changed."""
    src = _fn("async function openSettings")
    assert "await API.changelog()" in src


def test_a_failed_fetch_does_not_take_the_rest_of_settings_with_it():
    """Someone opening Settings to change their theme should not be stopped by
    the release history being unreachable."""
    src = _fn("async function openSettings")
    assert "catch" in src
    assert "could not be loaded" in src


def test_the_popup_and_the_section_draw_from_one_renderer():
    """Same entries, same markup — two reasons for being on screen. Two copies
    of this would drift, and the one nobody looks at would drift first."""
    assert "function renderUpdateEntries(box, entries)" in APP
    assert 'renderUpdateEntries(el("updates-body"), entries);' in APP
    assert 'renderUpdateEntries(box, await API.changelog())' in APP


def test_the_interrupting_popup_still_exists():
    """The section you visit and the popup that interrupts you are different
    things: one is navigation, the other is news arriving. Folding the second
    into the first would mean a deploy no longer tells anybody anything."""
    assert 'id="updates-backdrop"' in HTML
    assert "function showUpdates" in APP
    assert "checkForUpdates" in APP


def test_switching_section_starts_at_the_top():
    src = _fn("function showSettingsTab")
    assert "scrollTop = 0" in src
