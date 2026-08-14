"""Every operator on the cheat-sheet either works or says it doesn't.

Delphi's Boolean help pointed at the same operators every research guide lists —
Google's set — and four of them were accepted and then silently meant something
else:

    intitle:sleep        one literal word "intitle:sleep", which no article
                         contains, so the feed was empty and nothing said why
    intext:sleep         the same
    source:Education     the same
    sleep AROUND (5) x   parsed as `sleep AND 5 AND x`, demanding the digit 5
    ~academic            compiled to \\b~academic\\b and matched nothing, ever

That is the worst thing a query language can do: a feed that quietly means
something other than what was typed cannot be debugged from the results,
because the results look exactly like a topic with no news in it.

So the field operators are implemented, AROUND is Google's spelling of NEAR, and
the one that cannot be implemented honestly — `~`, which needs a thesaurus —
refuses with a message naming the alternative.
"""
import pytest

from backend.app.boolean_query import QueryError, compile_query, tokenize, validate_query
from backend.app.matching import CriteriaMatcher, match_fields, query_articles
from backend.app.models import Article, Source, utcnow


def article(title="", summary="", content="", name="Wire",
            url="https://example.com/a"):
    a = Article(title=title, summary=summary, content=content, url=url,
                importance=0, language="en", country="", categories=[], places=[],
                source_id=1, cluster_tokens=[])
    a.source = Source(name=name, homepage="", rss_url="", scope="national",
                      country="GB")
    return a


KIT = article(
    title="Vote now: worst football kit of July 2026",
    summary="Four contenders, one winner",
    content="The home shirt uses a bright Solar Yellow trim, and Solar Yellow "
            "again on the socks.",
    name="Footy Headlines", url="https://www.footyheadlines.com/5151/vote.html")

FARM = article(
    title="Solar farm approved in Kent",
    summary="Ministers back the scheme",
    content="The 500 MW solar project connects to the grid in 2028.",
    name="Reuters", url="https://www.reuters.com/business/energy/kent-solar")


def hits(query, art):
    return CriteriaMatcher({"queries": [query]}).matches(art)


# ---------- intitle: ----------

def test_intitle_matches_only_the_headline():
    assert hits("intitle:solar", FARM)
    assert not hits("intitle:solar", KIT)


def test_intitle_takes_a_quoted_phrase():
    assert hits('intitle:"solar farm"', FARM)
    assert not hits('intitle:"wind farm"', FARM)


def test_intitle_is_the_answer_to_the_reported_feed():
    """The energy query, rewritten so a colourway in a kit review cannot reach
    it however many times the page says it."""
    energy = ('intitle:solar OR intitle:wind OR intitle:"natural gas" '
              'OR intitle:nuclear OR intitle:coal')
    assert hits(energy, FARM)
    assert not hits(energy, KIT)


@pytest.mark.parametrize("alias", ["intitle", "title", "headline"])
def test_the_headline_aliases_all_work(alias):
    assert hits(f"{alias}:solar", FARM)


# ---------- intext: ----------

def test_intext_matches_the_body_and_summary_not_the_headline():
    only_head = article(title="Solar farm approved", content="Work begins.")
    assert not hits("intext:solar", only_head)
    assert hits("intext:solar", FARM)


def test_intext_and_intitle_combine():
    assert hits("intitle:solar AND intext:grid", FARM)
    assert not hits("intitle:solar AND intext:hydropower", FARM)


# ---------- source: and site: ----------

def test_source_matches_the_publication():
    assert hits("source:Reuters", FARM)
    assert not hits("source:Reuters", KIT)


def test_site_matches_the_host_the_article_came_from():
    assert hits("site:reuters.com", FARM)
    assert not hits("site:reuters.com", KIT)


def test_www_is_not_part_of_the_host():
    """`site:footyheadlines.com` has to match www.footyheadlines.com, because
    nobody means the other thing."""
    assert hits("site:footyheadlines.com", KIT)


def test_minus_site_excludes_a_publisher():
    """Straight off the cheat-sheet: `bears -site:wikipedia.org`."""
    assert hits("solar -site:footyheadlines.com", FARM)
    assert not hits("solar -site:footyheadlines.com", KIT)


def test_looking_at_the_masthead_has_to_be_asked_for_by_name():
    """The rule everywhere else is that matching sees the article and nothing
    else. `source:` is the exception, and it is an exception because the reader
    typed it — an unscoped word must never find the outlet's name."""
    assert not hits("reuters", article(title="A story", content="Some prose."))
    assert hits("source:reuters", article(title="A story", content="Some prose.",
                                          name="Reuters"))


# ---------- AROUND(n) ----------

@pytest.mark.parametrize("query", ["solar AROUND(5) grid", "solar AROUND (5) grid",
                                   "solar AROUND/5 grid", "solar around(5) grid"])
def test_around_is_googles_spelling_of_near(query):
    assert hits(query, FARM)


def test_around_still_measures_the_distance():
    far = article(title="Report", content="solar " + "filler " * 30 + "grid")
    assert not hits("solar AROUND(5) grid", far)
    assert hits("solar AROUND(40) grid", far)


def test_bare_around_defaults_like_bare_near():
    assert hits("solar AROUND grid", FARM)


def test_around_no_longer_demands_the_number_in_the_text():
    """It used to parse as `solar AND 5 AND grid` — an AND of the distance
    itself, so the query only matched articles that happened to say "5"."""
    import re
    assert not re.search(r"\b5\b", FARM.content), "the fixture would hide the bug"
    assert hits("solar AROUND(5) grid", FARM)


# ---------- the one that cannot be honest ----------

def test_the_synonym_operator_refuses_instead_of_matching_nothing():
    err = validate_query("~academic")
    assert err and "synonym" in err.lower()
    assert "OR" in err, "the message has to name the thing to do instead"


def test_a_field_with_nothing_after_it_is_an_error():
    assert validate_query("intitle:")
    assert validate_query("intitle: AND solar")


def test_an_unknown_prefix_is_still_just_a_word():
    """`foo:bar` is not a field; it must keep behaving as the word it is rather
    than becoming an error people cannot act on."""
    assert validate_query("foo:bar") is None
    assert hits("foo:bar", article(title="foo:bar in the headline"))


# ---------- and the index still offers a superset ----------

def test_a_scoped_search_still_uses_the_index(db):
    """The FTS prefilter only has to offer a superset. A term scoped to the
    headline is still in the index; one scoped to the publication is not, and
    that branch has to stop narrowing rather than exclude real matches."""
    src = Source(name="Reuters", rss_url="http://r/x", scope="international",
                 tier=1, added_by="user")
    db.add(src)
    db.flush()
    db.add_all([
        Article(source_id=src.id, url="http://r/1", title="Solar farm approved",
                summary="", content="The project connects to the grid.",
                language="en", importance=50, published_at=utcnow()),
        Article(source_id=src.id, url="http://r/2", title="Council approves bypass",
                summary="", content="A solar-powered sign will be installed. "
                                    "The solar sign replaces an older one.",
                language="en", importance=50, published_at=utcnow()),
    ])
    db.commit()

    found = query_articles(db, {"queries": ["intitle:solar"]}, limit=10)
    assert [a.url for a in found] == ["http://r/1"]

    both = query_articles(db, {"queries": ["source:Reuters AND solar"]}, limit=10)
    assert {a.url for a in both} == {"http://r/1", "http://r/2"}


# ---------- shapes that must not break ----------

def test_a_scoped_term_is_not_judged_a_passing_mention():
    """The passing-mention rule asks whether a bare word is what an article is
    about. A word pinned to the headline has already answered that, and a word
    pinned to the publication is not that question at all."""
    once = article(title="Solar farm approved", summary="", content="Work begins.")
    assert hits("intitle:solar", once)
    assert hits("source:Reuters", article(title="A story", content="Prose.",
                                          name="Reuters"))


def test_near_between_scoped_terms_says_so_rather_than_guessing():
    err = validate_query("intitle:solar NEAR/5 grid")
    assert err and "NEAR" in err


def test_a_plain_string_leaves_scoped_operators_nothing_to_match():
    """Callers that have only the article's text — the bench harness, anything
    outside the matcher — must not have `intitle:` quietly answer as though the
    whole article were the headline."""
    pred = compile_query("intitle:solar")
    assert pred("a story about solar power") is False
    assert compile_query("intext:solar")("a story about solar power") is True


def test_the_tokenizer_keeps_positions_usable():
    """Error messages quote a position; a field prefix must not throw them off
    so far that the message points at the wrong operator."""
    toks = tokenize("wind AND intitle:solar")
    kinds = [t.kind for t in toks]
    assert kinds == ["TERM", "AND", "FIELD", "TERM"]
    assert toks[-1].pos > toks[-2].pos


def test_advice_about_bare_words_leaves_scoped_ones_alone():
    from backend.app.boolean_query import ambiguous_terms
    assert ambiguous_terms("wind OR solar") == ["wind", "solar"]
    assert ambiguous_terms("intitle:wind OR intitle:solar") == []


def test_an_unclosed_quote_is_still_an_error_not_a_field():
    with pytest.raises(QueryError):
        compile_query('intitle:"solar')
