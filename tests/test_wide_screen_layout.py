"""The board has to use a wide monitor.

Columns were a fixed 360px, so extra width bought only more empty background.
Measured in a browser with seven columns: a 3440px window left 1,140px unused
and a 5120px 32:9 left 2,820px — over half the screen. Columns now take a share
of the leftover up to a width that still reads as a column, and whatever cannot
be used is split between both sides instead of piling up on the right.

Layout itself needs a browser, and CI has none, so what is checked here is the
contract the layout rests on: that the rules exist, that the numbers are
consistent with each other, and that the two ways of opting a column out of
growing are both wired. Each of these was a way to silently undo it.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "frontend" / "css" / "styles.css").read_text(encoding="utf8")
APP_JS = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf8")


def _var(name):
    m = re.search(rf"--{name}:\s*(\d+)px", CSS)
    assert m, f"--{name} is not defined"
    return int(m.group(1))


def test_a_column_has_a_base_width_and_a_bigger_ceiling():
    base, cap = _var("col-base"), _var("col-grow-max")
    assert base < cap, f"a column may not grow: base {base}, cap {cap}"


def test_the_ceiling_still_reads_as_a_column():
    """The other failure mode. Filling a 32:9 by making one column 1,600px
    wide is not scaling, it is a very wide page."""
    assert _var("col-grow-max") <= 700


def test_the_base_is_the_width_the_board_was_built_around():
    """Narrow windows must be untouched by any of this — below the point where
    columns start sharing leftover space, nothing changes."""
    assert _var("col-base") == 360


def test_columns_grow_but_never_shrink():
    """flex-shrink has to stay 0. A board of twelve columns that shrank to fit
    would make every one of them unreadable instead of scrolling."""
    rule = re.search(r"\.board\s*>\s*\.feed-col\s*\{([^}]*)\}", CSS)
    assert rule, "the board's column rule is gone"
    body = rule.group(1)
    flex = re.search(r"flex:\s*(\d+)\s+(\d+)\s+var\(--col-base\)", body)
    assert flex, f"the column's flex shorthand is not in the expected form: {body!r}"
    grow, shrink = int(flex.group(1)), int(flex.group(2))
    assert grow >= 1, "columns do not grow into a wide monitor"
    assert shrink == 0, "columns shrink instead of the board scrolling"
    assert "max-width: var(--col-grow-max)" in body, "columns grow without a ceiling"


def test_the_leftover_is_centred_safely():
    """`safe` is the whole point: plain `center` makes the first column
    unreachable as soon as the board overflows, because the overflow goes off
    the start edge where nothing can scroll to it."""
    board = re.search(r"\.board\s*\{([^}]*)\}", CSS)
    assert board, "the board rule is gone"
    assert "justify-content: safe center" in board.group(1), \
        "a board that cannot be filled leaves all the space on one side"


def test_a_column_the_reader_sized_stops_growing():
    """A width somebody chose is not leftover space to redistribute. Without
    this, dragging a column to 300px would last until the next reflow."""
    fn = re.search(r"function applyColWidth\([^)]*\)\s*\{(.*?)\n\}", APP_JS, re.S)
    assert fn, "applyColWidth is gone"
    body = fn.group(1)
    assert "flexGrow" in body, "a hand-sized column still shares the leftover space"
    assert "maxWidth" in body, "a hand-sized column is still capped at the grow ceiling"


def test_dragging_the_grip_escapes_the_ceiling_immediately():
    """Otherwise the grip appears stuck the moment the drag passes the cap, and
    lets go somewhere the reader did not put it."""
    preview = re.search(r"const preview = \(px\) => \{(.*?)\n  \};", APP_JS, re.S)
    assert preview, "the resize preview is gone"
    assert "flexGrow" in preview.group(1) and "maxWidth" in preview.group(1)


@pytest.mark.parametrize("width,columns", [
    (1280, 3), (1920, 4), (2560, 5), (3440, 7), (5120, 9),
])
def test_a_full_board_can_use_the_whole_monitor(width, columns):
    """At the base width the columns cannot fill a wide screen; at the ceiling
    a realistic number of them can. This is the arithmetic behind the change,
    kept honest against the numbers actually in the stylesheet."""
    base, cap = _var("col-base"), _var("col-grow-max")
    rail, gap, pad = 52, 14, 16 * 2
    usable = width - rail - pad
    assert columns * base + (columns - 1) * gap <= usable, \
        "this many columns do not fit even at the base width"
    reachable = columns * cap + (columns - 1) * gap
    assert reachable >= usable * 0.85, (
        f"{columns} columns at {cap}px reach {reachable}px of {usable}px — "
        "a full board still leaves most of this monitor empty")
