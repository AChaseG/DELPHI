"""A distance you can type as well as drag.

A drag bar is the right control for "roughly this far" and the wrong one for
"exactly 75": hitting one value in five hundred with a mouse is a game, and on
a phone it is not winnable. Both distance bars now carry a number box over the
same value.

These pin the parts a source-level test can see — the markup, the shared
wiring, and the traps the implementation has to avoid. What they cannot see is
whether typing works, which is why the helper was also driven with real
keystrokes in a browser.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend/index.html").read_text()
APP = (ROOT / "frontend/js/app.js").read_text()
CSS = (ROOT / "frontend/css/styles.css").read_text()

# (bar id, box id, unit id) for every distance control in the app.
DISTANCE_CONTROLS = [
    ("loc-radius", "loc-radius-num", "loc-radius-val"),
    ("loc-fire-km", "loc-fire-num", "loc-fire-val"),
]


def _input(el_id: str) -> str:
    m = re.search(r'<input[^>]*id="' + re.escape(el_id) + r'"[^>]*>', HTML)
    if m:
        return m.group(0)
    m = re.search(r'<input[^>]*id="' + re.escape(el_id) + r'"[^>]*$', HTML, re.M)
    assert m, f"no <input id={el_id}>"
    return m.group(0)


def _attr(tag: str, name: str) -> str:
    m = re.search(name + r'="([^"]*)"', tag)
    return m.group(1) if m else ""


def _multiline_input(el_id: str) -> str:
    """An input whose attributes are wrapped across lines."""
    m = re.search(r'<input(?:[^<>]|\n)*?id="' + re.escape(el_id) + r'"(?:[^<>]|\n)*?>', HTML)
    assert m, f"no <input id={el_id}>"
    return " ".join(m.group(0).split())


@pytest.mark.parametrize("bar,box,unit", DISTANCE_CONTROLS)
def test_every_distance_bar_has_a_number_box(bar, box, unit):
    assert f'id="{box}"' in HTML, f"{bar} has no box to type into"
    assert 'type="number"' in _multiline_input(box)


@pytest.mark.parametrize("bar,box,unit", DISTANCE_CONTROLS)
def test_the_box_and_the_bar_agree_on_their_range(bar, box, unit):
    """A box that accepted a number the bar cannot hold would show one value
    and store another."""
    b, n = _multiline_input(bar), _multiline_input(box)
    assert _attr(b, "min") == _attr(n, "min")
    assert _attr(b, "max") == _attr(n, "max")


@pytest.mark.parametrize("bar,box,unit", DISTANCE_CONTROLS)
def test_the_bar_steps_by_one(bar, box, unit):
    """The fire ring used to step by 5. With a box beside it a typed 77 has to
    mean 77, and a bar that snapped to 75 would silently disagree with the
    number printed next to it."""
    assert _attr(_multiline_input(bar), "step") == "1"


@pytest.mark.parametrize("bar,box,unit", DISTANCE_CONTROLS)
def test_the_box_offers_a_number_pad_on_a_phone(bar, box, unit):
    assert 'inputmode="numeric"' in _multiline_input(box)


@pytest.mark.parametrize("bar,box,unit", DISTANCE_CONTROLS)
def test_both_controls_are_named_for_a_screen_reader(bar, box, unit):
    assert _attr(_multiline_input(box), "aria-label")
    assert _attr(_multiline_input(bar), "aria-label")


@pytest.mark.parametrize("bar,box,unit", DISTANCE_CONTROLS)
def test_each_one_is_wired_through_the_shared_helper(bar, box, unit):
    """One mechanism, used twice. Two hand-rolled copies would drift, and the
    unit conversion is the half that must not."""
    anchor = f'slider: "{bar}"'
    assert anchor in APP, f"{bar} is not wired through wireDistanceBox"
    start = APP.rindex("wireDistanceBox({", 0, APP.index(anchor))
    wiring = APP[start:APP.index("});", start) + 3]
    assert f'box: "{box}"' in wiring
    assert f'unit: "{unit}"' in wiring
    assert "fallback:" in wiring, "no value to fall back to when the box is emptied"


def test_the_row_is_no_longer_a_label():
    """A <label> wrapping two inputs associates with the first one, so clicking
    anywhere on the row — including the bar — would have focused the box."""
    for row in re.findall(r'<(label|div) class="loc-radius"', HTML):
        assert row == "div"


# --- the traps the helper has to avoid ------------------------------------

def _helper() -> str:
    m = re.search(r"function wireDistanceBox\(.*?\n}\n", APP, re.S)
    assert m, "wireDistanceBox is gone"
    return m.group(0)


def test_a_half_typed_number_does_not_get_clamped():
    """Clamping on every keystroke makes 150 impossible to enter: the 1 is
    below the minimum, is clamped and rewritten, and the 5 lands in a field
    that now says 1. Keystrokes may only move the bar when what has been typed
    so far is already valid."""
    body = _helper()
    keystroke = body[body.index('num.addEventListener("input"'):]
    keystroke = keystroke[:keystroke.index("const settle")]
    assert "return" in keystroke, "the keystroke handler clamps instead of waiting"
    assert "Math.min" not in keystroke and "Math.max" not in keystroke


def test_it_settles_when_the_reader_has_finished():
    body = _helper()
    for moment in ('"change"', '"blur"'):
        assert moment in body, f"nothing settles the value on {moment}"


def test_enter_commits_rather_than_submitting():
    assert 'e.key === "Enter"' in _helper()


def test_the_box_works_in_the_unit_the_reader_can_see():
    """Everything in app.js is kilometres, translated on the way out. A number
    typed into a box labelled "mi" means miles, and converting it is the whole
    difference between this and an <input type="number"> next to a slider."""
    body = _helper()
    assert "useMiles()" in body
    assert "KM_PER_MILE" in body
    assert 'useMiles() ? "mi" : "km"' in body


def test_switching_units_rewrites_the_boxes():
    """A box still reading 25 beside "mi" would be a different distance from
    the one the reader set — this is a number, not only its wording."""
    handler = APP[APP.index('Settings.set("units", units.value)'):]
    handler = handler[:handler.index("theme.onchange")]
    assert "repaintRadius" in handler and "repaintFire" in handler


def test_the_box_is_sized_to_its_digits():
    assert ".dist-num" in CSS
    assert "tabular-nums" in CSS[CSS.index(".dist-num"):CSS.index(".dist-num") + 400]
