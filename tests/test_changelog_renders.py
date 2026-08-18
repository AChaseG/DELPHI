"""The What's-new popup has to render its own emphasis.

Entries in `changelog.py` are written with a little markup — <b> for the thing
that changed, <code> for something to type. The popup set them as textContent,
so every reader who opened it saw the tags: "…was taking <b>78 seconds</b>".
Thirteen of the entries were affected, and it had been shipping for weeks before
a screenshot taken for something else caught it.

Rendering them means turning a string into nodes, so the way it is done matters
more than that it is done. The tags are matched against an allowlist and built
as elements; nothing is handed to innerHTML, and anything that is not on the
list stays the text it was. This file pins both halves: the renderer's shape,
and the fact that the entries only ever use tags the renderer knows.
"""
import pathlib
import re

from backend.app.changelog import CHANGELOG

APP = (pathlib.Path(__file__).resolve().parent.parent / "frontend" / "js"
       / "app.js").read_text(encoding="utf-8")

ALLOWED_TAGS = {"b", "i", "em", "code"}
ALLOWED_ENTITIES = {"amp", "lt", "gt", "quot", "#39"}


def _fn(name: str) -> str:
    start = APP.index(name)
    end = APP.find("\n}\n", start)
    assert end > start
    return APP[start:end]


# ---------- the popup renders it ----------

def test_items_are_not_set_as_plain_text():
    """Checked on the shared renderer rather than on showUpdates. The drawing
    moved there when Settings gained a What's-new section of its own, and both
    the interrupting popup and that section now call it — so this is the one
    place the rule has to hold, and holding it here covers both."""
    src = _fn("function renderUpdateEntries")
    assert "li.textContent = item" not in src, (
        "this is the bug: it shows the tags to the reader")
    assert "updateItemNodes(item)" in src


def test_both_places_that_show_release_notes_use_that_renderer():
    """A second copy would drift, and the one nobody looks at would drift
    first — into exactly the innerHTML shortcut this file exists to forbid."""
    assert 'renderUpdateEntries(el("updates-body"), entries);' in APP
    assert "renderUpdateEntries(box, await API.changelog())" in APP


def test_the_renderer_never_hands_a_string_to_innerhtml():
    """The one thing that must stay true of this file: no string in it becomes
    markup. An allowlist that builds elements keeps the count at zero."""
    src = _fn("function updateItemNodes")
    assert "innerHTML" not in src
    assert "createElement(" in src and "createTextNode(" in src


def test_only_a_short_list_of_tags_is_understood():
    src = _fn("const UPDATE_TAGS")
    assert set(re.findall(r'"(\w+)"', src)) == ALLOWED_TAGS


def test_an_unknown_tag_stays_text():
    src = _fn("function updateItemNodes")
    assert "UPDATE_TAGS.has(name)" in src
    assert "continue" in src, "an unrecognised tag has to fall through as text"


def test_a_stray_closing_tag_closes_nothing():
    src = _fn("function updateItemNodes")
    assert "open.length > 1" in src


def test_the_handful_of_entities_are_decoded():
    """`&amp;` in an entry would otherwise be read out as five characters."""
    src = _fn("const UPDATE_ENTITIES")
    assert set(re.findall(r'"?([\w#]+)"?:\s*[\'"]', src)) >= ALLOWED_ENTITIES


# ---------- and the entries stay inside what it renders ----------

def test_every_entry_uses_only_tags_the_popup_knows():
    """A future entry reaching for <span> or <a> would show the tag rather than
    the styling, which is the same bug in a new place."""
    used = set()
    for entry in CHANGELOG:
        for item in entry["items"]:
            used |= {t.lower().lstrip("/") for t in re.findall(r"<(/?[a-zA-Z]+)>", item)}
    assert used <= ALLOWED_TAGS, f"changelog uses {sorted(used - ALLOWED_TAGS)}"


def test_every_entity_used_is_one_that_gets_decoded():
    used = set()
    for entry in CHANGELOG:
        for item in entry["items"]:
            used |= set(re.findall(r"&([a-zA-Z#0-9]+);", item))
    assert used <= ALLOWED_ENTITIES, f"changelog uses &{sorted(used - ALLOWED_ENTITIES)};"


def test_tags_in_the_entries_are_balanced():
    """An unclosed <b> would bold the rest of the line and nothing would say so."""
    for entry in CHANGELOG:
        for item in entry["items"]:
            depth = {}
            for tag in re.findall(r"<(/?[a-zA-Z]+)>", item):
                name = tag.lstrip("/").lower()
                if name not in ALLOWED_TAGS:
                    continue
                depth[name] = depth.get(name, 0) + (-1 if tag.startswith("/") else 1)
                assert depth[name] >= 0, f"{entry['date']}: stray </{name}>"
            assert not any(depth.values()), (
                f"{entry['date']} — {entry['title']}: unclosed {sorted(k for k, v in depth.items() if v)}")
