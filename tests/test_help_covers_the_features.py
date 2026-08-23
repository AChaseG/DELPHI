"""The help has to describe the app that exists.

Asked directly: when was the FAQ last updated? The answer was that its last
content change predated Typhon and Athena entirely — a grep of the help modal
found zero mentions of either, while the changelog had shipped six entries
about them. Help that describes last month's app is worse than none, because
somebody trusts it.

This file is the thing that would have caught it. Not "is there a section"
prose-checking, which any wall of text passes, but: **for each shipped feature,
does the help name the control a reader would have to find?** A control that
moved and a feature that shipped are the two ways this copy goes stale, and the
first already caught Delphi out once — the FAQ described a gear that no longer
existed on any screen.
"""
import pathlib
import re

HTML = (pathlib.Path(__file__).resolve().parent.parent
        / "frontend" / "index.html").read_text(encoding="utf-8")

START = HTML.index('id="faq-backdrop"')
END = HTML.index('id="updates-backdrop"')
HELP = HTML[START:END]
# Sliced on the tab *bodies*, not the tab buttons: both spellings appear, the
# buttons come first, and slicing on them yields the two-line nav and nothing
# of the content this file is about.
_HOWTO_AT = HELP.index('help-tab" data-tab="howto"')
_FAQ_AT = HELP.index('help-tab" data-tab="faq"')
_TROUBLE_AT = HELP.index('help-tab" data-tab="trouble"')
# Whitespace-collapsed, because a browser collapses it too: a phrase that
# happens to straddle a line break in the source is one phrase on the screen,
# and a test that cannot see that fails on the indentation rather than on the
# copy.
def _flat(text):
    return re.sub(r"\s+", " ", text)


HOWTO = _flat(HELP[_HOWTO_AT:_FAQ_AT])
FAQ = _flat(HELP[_FAQ_AT:_TROUBLE_AT])


import pytest


@pytest.mark.parametrize("feature,needle", [
    ("Typhon", "Typhon"),
    ("Athena", "Athena"),
    ("the hazard layers panel", "Base map"),
    ("the fire ring", "wildfire distance"),
    ("fire perimeters", "perimeter"),
    ("filing a document", "File a document"),
    ("the theme vocabulary", "Themes"),
    ("the coverage grid", "Coverage"),
])
def test_the_how_to_names_the_control(feature, needle):
    """A reader following the How-to has to be able to find the thing. Naming
    the button is the difference between instructions and a description."""
    assert needle in HOWTO, f"{feature}: nothing in the How-to says “{needle}”"


@pytest.mark.parametrize("question,needle", [
    ("why a fire is a circle", "approximate area burned"),
    ("why a reading is not an AQI", "official AQI"),
    ("community sensors", "PurpleAir"),
    ("what Athena counts", "Athena"),
    ("why a layer is empty", "NEWS_HAZARDS=0"),
])
def test_the_faq_answers_the_question_that_will_be_asked(question, needle):
    assert needle in FAQ, f"{question}: the FAQ does not mention “{needle}”"


def test_the_privacy_promise_is_made_where_somebody_will_read_it():
    """Athena parses documents in the browser and never uploads them. That is
    the single most important thing to say about the feature, and the dialog
    that says it is shown once."""
    assert HOWTO.count("never leave your browser") >= 1
    assert "never uploaded and never stored" in FAQ


def test_the_help_only_uses_tags_the_renderer_knows():
    """The same allowlist the changelog is held to. A <span> here would render
    as the literal tag."""
    allowed = {"b", "i", "em", "code", "p", "details", "summary", "ol", "ul",
               "li", "div", "h2", "h3", "nav", "button", "a", "br", "strong",
               "span"}
    used = {t.lower().lstrip("/") for t in re.findall(r"<(/?[a-zA-Z0-9]+)[ >]", HELP)}
    assert used <= allowed, f"help uses {sorted(used - allowed)}"


def test_every_disclosure_closes():
    """An unclosed <details> swallows every section after it, and the tab looks
    half-empty rather than broken."""
    assert HELP.count("<details") == HELP.count("</details>")
    assert HELP.count("<summary") == HELP.count("</summary>")


def test_no_control_is_described_where_it_no_longer_is():
    """The way this copy went wrong last time: it named a gear that had been
    replaced, so every instruction pointed at a glyph on no screen."""
    assert "⚙" not in HELP
    assert "⚙" not in HTML
