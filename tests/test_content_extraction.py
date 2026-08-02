"""What counts as the article, and what is the page advertising other articles.

Criteria are matched against title + summary + body, so whatever the extractor
calls "body" is what a boolean query is really searched in. A page carries far
more than its story — a section menu, a "More from" rail, "Most read", a
newsletter promo — and all of those are built from list items and headings
wrapped around links.

This is the bug that was reported as the boolean engine being wrong: a WNBA
report turned up in a data-centre feed, and the reader could find none of their
terms in the article. The terms were there — in the promo rail, stored as part
of the body.
"""
from backend.app.boolean_query import compile_query
from backend.app.content import extract_text

STORY = """
  <h1>WNBA player at centre of transgender eligibility row</h1>
  <p>The league said on Friday it would review its eligibility policy before
     the new season, after a complaint was filed by two clubs.</p>
  <p>Officials declined to say when the review would conclude.</p>
"""


def page(*blocks):
    return "<!doctype html><html><body>" + "".join(blocks) + "</body></html>"


MENU = """<header><div class="menu"><ul>
  <li><a href="/world">World</a></li>
  <li><a href="/tech/data-center">Data Center</a></li>
</ul></div></header>"""

MORE_FROM = """<div class="more-from"><h2>More from Technology</h2><ul>
  <li><a href="/a">Inside the new AI data center boom</a></li>
  <li><a href="/b">Hyperscale operators bet on nuclear</a></li>
</ul></div>"""

PROMO = """<div class="promo">
  <h3><a href="/sub">Subscribe to our Data Center newsletter</a></h3></div>"""


def test_a_section_menu_is_not_part_of_the_story():
    body = extract_text(page(MENU, STORY)).lower()
    assert "data center" not in body
    assert "eligibility policy" in body


def test_a_more_from_rail_is_not_part_of_the_story():
    body = extract_text(page(STORY, MORE_FROM)).lower()
    assert "data center" not in body
    assert "hyperscale" not in body


def test_a_newsletter_promo_is_not_part_of_the_story():
    body = extract_text(page(STORY, PROMO)).lower()
    assert "data center" not in body


def test_the_reported_failure_no_longer_happens():
    """End to end, in the terms it was reported in."""
    body = extract_text(page(MENU, STORY, MORE_FROM, PROMO))
    text = f"WNBA player at centre of transgender eligibility row\n\n{body}"

    matches = compile_query('"data center" OR datacenter OR hyperscale')

    assert not matches(text), (
        "a WNBA report still satisfies a data-centre query; the terms it "
        f"matched on are in: {body!r}")


# ---------- and what must survive it ----------

def test_a_real_bulleted_list_is_kept():
    """Articles have lists. Dropping them to be rid of menus would lose the
    substance of every explainer and live blog."""
    body = extract_text(page(STORY, """<ul>
        <li>The policy was last revised four years ago.</li>
        <li>Two clubs filed the complaint.</li></ul>"""))
    assert "policy was last revised four years ago" in body
    assert "Two clubs filed the complaint" in body


def test_a_link_inside_a_paragraph_is_kept():
    """An inline citation is prose. Only a link that *is* the whole list item
    or heading is another page."""
    body = extract_text(page(
        '<p>The clubs, <a href="/x">as reported on Tuesday</a>, filed jointly.</p>'))
    assert "as reported on Tuesday" in body


def test_a_link_inside_a_bulleted_point_loses_only_the_link():
    body = extract_text(page(STORY, """<ul>
        <li>The complaint, <a href="/y">filed on Tuesday</a>, names two clubs.</li>
        </ul>"""))
    assert "names two clubs" in body


def test_the_headline_of_the_article_itself_is_kept():
    """The story's own <h1> is not a link, so nothing here touches it."""
    assert "WNBA player at centre" in extract_text(page(MENU, STORY))


def test_a_page_with_no_paragraphs_still_yields_its_text():
    """The fallback for sites that mark up with divs. It must not start
    returning the menu instead."""
    body = extract_text(page(MENU, """<div>The league said on Friday that it
        would review the eligibility policy before the new season begins, and
        declined to say when that review might conclude or who would sit on
        it.</div>"""))
    assert "review the eligibility policy" in body
    assert "data center" not in body.lower()


def test_an_empty_page_is_empty_not_an_error():
    assert extract_text("<html><body></body></html>") == ""
    assert extract_text("") == ""


def test_malformed_markup_keeps_what_was_parsed():
    body = extract_text("<html><body><p>Real prose here about the league.<p>")
    assert "Real prose here" in body
