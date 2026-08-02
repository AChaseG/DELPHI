"""Pressing ✎ has to show the editor before it fetches anything.

The wizard used to `await ensureSources()` first, so nothing appeared at all —
no frame, no spinner — until the whole outlet catalog had arrived and been
parsed. That is 484 kB over ~1,200 sources, and it grows every time discovery
adds another one, so the delay got worse over time on a control that should be
instant. Measured cold, shaping the connection: 79ms on localhost, 201ms on an
ordinary home line, 438ms on hotel wifi. Opening first and letting the catalog
land behind the modal: 46 / 28 / 30ms, and flat, because nothing in it waits on
the network any more.

Timing needs a browser and CI has none, so what is pinned here is the structure
that produces the timing: that the open happens before the fetch rather than
after it, and that the picker admits which of its three states it is in. An
`await` slipped back in front of `Builder.open` would restore the old delay
without failing anything else.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf8")
BUILDER_JS = (ROOT / "frontend" / "js" / "builder.js").read_text(encoding="utf8")


def _open_builder():
    """The body of openBuilder, from its signature to the closing brace at
    column zero."""
    m = re.search(r"^function openBuilder\([^)]*\)\s*\{\n(.*?)^\}", APP_JS,
                  re.S | re.M)
    assert m, "openBuilder is no longer a plain top-level function"
    return m.group(1)


def test_the_editor_is_opened_before_the_catalog_is_asked_for():
    body = _open_builder()
    opened = body.index("Builder.open(")
    fetched = body.index("ensureSources(")
    assert opened < fetched, (
        "openBuilder asks for the source catalog before showing the modal; "
        "that is the delay this was written to remove")


def test_opening_the_editor_does_not_await_anything():
    """`await` is the specific mistake: it is one keyword, it reads as
    harmless, and it puts the whole 484 kB fetch back in front of the frame."""
    body = _open_builder()
    assert "await " not in body, "openBuilder awaits again"
    assert not re.search(r"^async function openBuilder", APP_JS, re.M), (
        "openBuilder is async again, which invites an await back into it")


def test_a_warm_catalog_skips_the_fetch_entirely():
    """Reopening the editor must not re-render the picker through its loading
    state — the outlets are already in memory."""
    body = _open_builder()
    assert re.search(r"if \(SOURCES\.length\)\s*return", body), (
        "openBuilder no longer short-circuits when the catalog is in memory")


def test_the_catalog_arriving_and_failing_are_both_handled():
    body = _open_builder()
    assert "Builder.sourcesPending()" in body
    assert "Builder.sourcesArrived()" in body
    assert "Builder.sourcesFailed(" in body, (
        "a failed catalog fetch would leave the picker saying 'loading' for "
        "as long as the modal is open")
    assert ".catch(" in body, "an unhandled rejection would strand the picker"


def test_the_picker_has_the_three_states_the_opener_drives():
    for name in ("sourcesPending", "sourcesArrived", "sourcesFailed"):
        assert re.search(rf"^\s+{name}\(", BUILDER_JS, re.M), (
            f"Builder.{name} is gone; openBuilder calls it")


def test_each_state_repaints_the_picker():
    """Setting the flag without repainting leaves whatever was on screen
    before, which is the empty list the message exists to explain."""
    for name in ("sourcesPending", "sourcesArrived", "sourcesFailed"):
        m = re.search(rf"^\s+{name}\([^)]*\)\s*\{{(.*?)^\s+\}}", BUILDER_JS,
                      re.S | re.M)
        assert m, f"cannot read Builder.{name}"
        assert "_renderSources()" in m.group(1), f"{name} does not repaint"


def test_a_loading_picker_says_so_rather_than_looking_empty():
    """An empty box reads as 'this server has no sources', which is a more
    alarming thing to be told than 'still loading'."""
    m = re.search(r"_renderSources\(\)\s*\{(.*?)const needle", BUILDER_JS, re.S)
    assert m, "the guard at the top of _renderSources is gone"
    guard = m.group(1)
    assert '_sourcesState !== "ready"' in guard
    assert "Loading" in guard or "loading" in guard


def test_the_rest_of_the_editor_is_usable_while_the_catalog_loads():
    """The promise of opening early is that the step the reader lands on
    works. Countries, categories and languages come from META at startup, so
    the loading notice must not be a modal-wide block."""
    m = re.search(r"_renderSources\(\)\s*\{(.*?)const needle", BUILDER_JS, re.S)
    guard = m.group(1)
    assert "b-sources" in BUILDER_JS
    assert "disabled" not in guard, (
        "the loading state disables controls beyond the outlet picker")


def test_the_notice_is_written_for_a_reader():
    """It appears in front of someone who pressed ✎ and is looking at an
    otherwise working form; it should tell them that, not name a function."""
    for bad in ("ensureSources", "SOURCES", "undefined", "null", "Promise"):
        m = re.search(r'"Loading the outlet list[^"]*"', BUILDER_JS)
        assert m, "the loading notice is gone"
        assert bad not in m.group(0)


def test_the_failure_notice_says_what_still_works():
    m = re.search(r"_sourcesState === \"loading\"(.*?)box\.appendChild",
                  BUILDER_JS, re.S)
    assert m, "the failed branch of the picker notice is gone"
    assert "still works" in m.group(1), (
        "a failed catalog should not read as a broken editor")
