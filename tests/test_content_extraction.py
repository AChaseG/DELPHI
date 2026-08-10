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


def test_a_bulleted_point_containing_a_link_keeps_the_whole_point():
    """A point that says something of its own is prose, link and all — the
    link is a citation inside it, not the reason the line exists."""
    body = extract_text(page(STORY, """<ul>
        <li>The complaint, <a href="/y">filed on Tuesday</a>, names two clubs.</li>
        </ul>"""))
    assert "names two clubs" in body
    assert "filed on Tuesday" in body


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


# ---------- a rail is a rail whichever tag holds it ----------

RAIL_TERMS = ("data center", "hyperscale", "colocation")


def _leaks(html: str) -> list[str]:
    body = extract_text(html).lower()
    return [t for t in RAIL_TERMS if t in body]


def test_a_rail_built_from_paragraphs_is_not_the_story():
    """The tag list was the whole test before, and `<p class="related">` walked
    straight through it: one link, one paragraph, and a WNBA report contains
    "data center"."""
    assert _leaks(page(STORY, '<p class="related">'
                              '<a href="/a">Inside the new AI data center boom</a>'
                              '</p>')) == []


def test_a_rail_built_from_divs_is_not_the_story():
    assert _leaks(page(STORY, '<div class="rail">'
                              '<a href="/a">Inside the new AI data center boom</a>'
                              '<a href="/b">Hyperscale colocation deals surge</a>'
                              '</div>')) == []


# ---------- tags the page never closed ----------
#
# `</p>` and `</li>` are optional in HTML. Nothing used to close them, so one
# omission meant every promo, rail and pitch below it was inside the article as
# far as the extractor could tell.

PROMO_DIV = ('<div class="promo">Sign up for our Data Center Weekly newsletter '
             'and never miss a hyperscale colocation deal.</div>')


def test_an_unclosed_paragraph_does_not_swallow_the_rest_of_the_page():
    assert _leaks(page(STORY.replace("</p>", ""), PROMO_DIV)) == []


def test_an_unclosed_list_item_does_not_swallow_the_rest_of_the_page():
    assert _leaks(page(STORY, "<ul><li>Vote was Tuesday<li>Effective in June</ul>",
                       PROMO_DIV)) == []


def test_an_unclosed_paragraph_still_gives_up_its_own_text():
    """Closing it early must not lose it."""
    body = extract_text(page("<div><p>The league said it would review the policy"
                             "<div>Subscribe today</div></div>"))
    assert "review the policy" in body


def test_a_stray_end_tag_closes_nothing():
    body = extract_text(page("</p></div><p>The league said it would review the "
                             "eligibility policy before the new season.</p>"))
    assert "review the eligibility policy" in body


def test_the_text_stays_in_the_order_it_was_written():
    """A block is judged when it closes, which is after anything nested in it —
    so the pieces are finished with inside-out. Proximity searches (`a NEAR/5
    b`) read the result as one string, so it has to come back out in the order
    it was written."""
    body = extract_text(page(
        "<ul><li>The complaint was filed by two clubs on Tuesday afternoon."
        "<p>The league confirmed receipt the following morning.</p></li></ul>"
        "<p>Officials declined to say when the review would conclude.</p>"))
    assert (body.index("filed by two clubs")
            < body.index("confirmed receipt")
            < body.index("declined to say"))


# ---------- the page talking about itself ----------

def test_a_consent_notice_is_not_the_story():
    body = extract_text(page(STORY, "<div><p>We use cookies to personalise "
                                    "content and ads and to analyse our "
                                    "traffic.</p></div>")).lower()
    assert "personalise content" not in body
    assert "eligibility policy" in body


def test_an_article_about_cookies_keeps_the_word():
    """The site-voice list is phrases, not topics. A ruling about cookie
    consent is a real story about cookies."""
    body = extract_text(page("<p>The regulator fined the company four million "
                             "euros over its cookie consent banner, which it "
                             "said made refusal harder than acceptance.</p>"))
    assert "cookie consent banner" in body


# ---------- the fallback, for pages with no paragraphs ----------

def test_the_fallback_keeps_sentences_and_drops_the_chrome():
    body = extract_text(page(
        '<div class="menu">Home Politics Business Data Center Sports</div>',
        "<div>The league said on Friday that it would review the eligibility "
        "policy before the new season begins, and declined to say when that "
        "review might conclude.</div>",
        '<div class="foot">Sign up for hyperscale colocation updates.</div>'))
    assert "review the eligibility policy" in body
    assert "data center" not in body.lower()
    assert "hyperscale" not in body.lower()


def test_a_page_with_no_sentences_yields_no_body_at_all():
    """A JavaScript shell used to hand over its entire navigation as the
    article. Nothing is the honest answer: the article is then matched on its
    headline and feed summary, which is all that was ever retrieved."""
    assert extract_text(page(
        '<div class="menu">Home Politics Business Data Center Sports</div>',
        '<div id="app">Enable JavaScript to read this.</div>')) == ""


def test_a_short_article_keeps_its_own_words():
    """A two-sentence story is under the 200 characters that ask for the
    fallback, and the fallback used to *replace* what the markup found rather
    than fill in for it — so the shortest articles were the ones most likely to
    be stored as somebody's promo rail."""
    body = extract_text(page(
        "<p>Two clubs filed a complaint on Tuesday over the eligibility "
        "policy.</p>",
        '<div class="rail"><a href="/a">Inside the new AI data center boom</a>'
        '<a href="/b">Hyperscale colocation deals surge</a></div>',
        "<div>Subscribe to our daily technology briefing for colocation and "
        "hyperscale coverage from across the industry.</div>"))
    assert "Two clubs filed a complaint" in body
    assert [t for t in RAIL_TERMS if t in body.lower()] == []


def test_an_empty_page_is_empty_not_an_error():
    assert extract_text("<html><body></body></html>") == ""
    assert extract_text("") == ""


def test_malformed_markup_keeps_what_was_parsed():
    body = extract_text("<html><body><p>Real prose here about the league.<p>")
    assert "Real prose here" in body
