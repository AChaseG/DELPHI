"""A word said once, deep in an article, is not what the article is about.

Reported with a link and a query. The query was ten energy terms OR'd together;
the article was a vote on the worst football kit of the month. Nothing was
broken — a kit colourway is called "Solar Yellow", so the page genuinely
contains the word "solar" — and that is the whole problem: the boolean was
right and the result was useless. The same thing had already been reported as a
Taiwanese concert listing in an energy feed, because one of the songs is called
"Innocent Wind".

No dictionary of word senses can settle these. "Solar" means whatever the
sentence around it means, and a self-hosted news reader is not going to read the
sentence. What it can do is ask how *prominent* the word is, which is the
judgement a person makes skimming the page:

  · a quoted phrase passes on its own — "power plant" is not said in passing;
  · a word in the headline or summary passes, because that is where a story
    says what it is about;
  · two of the query's own words turning up passes — an energy story says
    "solar" and "coal", a kit review says one energy word once;
  · otherwise a lone word has to be used more than once in the body.

Everything the reader wants still arrives: a story about a solar farm says so in
its headline, and if it somehow doesn't, it says "solar" again a paragraph
later. What stops arriving is the single incidental mention.

`passing_mentions: true` in a feed's criteria turns this off and restores the
old behaviour, which is the right escape hatch for a query hunting for rare
one-line mentions.
"""
import pytest

from backend.app.matching import CriteriaMatcher
from backend.app.models import Article, Source

ENERGY = ('(“solar” OR “wind” OR “natural gas” OR “nuclear” OR “coal” OR '
          '“power plant” OR “hydropower” OR “geothermal” OR “renewable energy” '
          'OR “shale gas”)')


def article(title="", summary="", content=""):
    a = Article(title=title, summary=summary, content=content, importance=0,
                language="en", country="", categories=[], places=[], source_id=1,
                cluster_tokens=[])
    a.source = Source(name="Wire", homepage="", rss_url="", scope="national",
                      country="GB")
    return a


def matches(criteria, art):
    return CriteriaMatcher(criteria).matches(art)


# ---------- the reported failures ----------

def test_a_kit_colourway_does_not_belong_in_an_energy_feed():
    kit = article(
        title="Vote now: worst football kit of July 2026",
        summary="Four contenders, one winner. Have your say.",
        content="The home shirt pairs a bright Solar Yellow trim with navy "
                "sleeves. Supporters were split over the collar, and the away "
                "kit is plain white with a gold crest.")
    assert not matches({"queries": [ENERGY]}, kit)


def test_a_place_name_containing_a_query_word_does_not_either():
    listing = article(
        title="Hatfield McCoy Marathon",
        summary="Route, registration and road closures",
        content="The course runs along the old Coal Heritage Trail before "
                "finishing beside the river in town.")
    assert not matches({"queries": [ENERGY]}, listing)


def test_the_song_title_case_that_started_it():
    """An energy feed carried a concert listing because one of the songs is
    called "Innocent Wind"."""
    listing = article(
        title="Setlist: three nights at the Taipei Arena",
        summary="Every song, in order",
        content="The encore opened with Innocent Wind before the band closed "
                "on the title track.")
    assert not matches({"queries": [ENERGY]}, listing)


# ---------- and what must still arrive ----------

def test_a_story_that_announces_itself_in_the_headline():
    assert matches({"queries": [ENERGY]}, article(
        title="Britain's largest solar farm approved",
        summary="Ministers back the scheme",
        content="The 500 MW project will connect to the grid in 2028."))


def test_a_term_in_the_summary_counts_too():
    assert matches({"queries": [ENERGY]}, article(
        title="Ministers back Kent scheme",
        summary="Approval for a solar project covering 900 acres",
        content="Construction begins next spring."))


def test_a_word_used_twice_in_the_body_counts():
    """Body-only is not the test — being incidental is. A story that never says
    it in the headline still says it more than once."""
    assert matches({"queries": [ENERGY]}, article(
        title="Ministers back Kent scheme",
        summary="Approval granted on Tuesday",
        content="The solar project covers 900 acres. Solar developers "
                "welcomed the decision."))


def test_two_of_the_querys_words_agreeing_counts():
    """Neither word is in the headline and neither is repeated — but a page
    that mentions two of them is talking about the subject."""
    assert matches({"queries": [ENERGY]}, article(
        title="Kent scheme approved",
        summary="Approval granted on Tuesday",
        content="The plan pairs a wind array with a coal retirement."))


def test_a_quoted_phrase_is_specific_enough_on_its_own():
    """Somebody who wrote "shale gas" has already been precise. Holding a
    phrase to the same bar would punish the exact thing the query advisories
    ask people to do."""
    assert matches({"queries": ['"shale gas"']}, article(
        title="Quarterly results beat expectations",
        summary="Revenue up 4%",
        content="The division's shale gas assets carried the quarter."))


def test_plain_keywords_are_held_to_the_same_rule():
    """Keywords and boolean queries are the same thing to a reader."""
    kit = article(title="Vote now: worst football kit of July 2026",
                  content="A bright Solar Yellow trim across the shoulders.")
    assert not matches({"keywords": ["solar"]}, kit)
    assert matches({"keywords": ["solar"]},
                   article(title="Solar farm approved", content="Work begins."))


# ---------- the escape hatch ----------

def test_a_feed_can_ask_for_passing_mentions():
    kit = article(title="Vote now: worst football kit of July 2026",
                  content="A bright Solar Yellow trim across the shoulders.")
    assert matches({"queries": [ENERGY], "passing_mentions": True}, kit)


def test_exclusions_are_never_subject_to_it():
    """An excluded word must still bite on a single mention: "not this" is a
    different promise from "about this", and a reader who excludes a word means
    it wherever it appears."""
    art = article(title="Solar farm approved", summary="Ministers back it",
                  content="Our columnist's opinion is that this is overdue.")
    assert not matches({"queries": [ENERGY], "exclude_keywords": ["opinion"]}, art)


def test_a_feed_with_no_text_terms_is_untouched():
    """Nothing here should reach a feed that filters on category or place."""
    assert matches({"categories": []}, article(title="Anything at all"))


# ---------- shapes that must not throw ----------

@pytest.mark.parametrize("criteria", [
    {"queries": ['"wind power" NEAR/5 grid']},        # NEAR pairs have no literal
    {"queries": ["NOT football"]},                    # nothing literal matches
    {"queries": ["solar"], "keywords": ["solar"]},    # the same word twice over
    {"keywords": ["  "]},                             # blank
])
def test_odd_criteria_still_answer(criteria):
    art = article(title="Grid operator connects wind power array",
                  content="The array feeds the grid from next month.")
    assert isinstance(matches(criteria, art), bool)


def test_the_same_word_twice_over_cannot_agree_with_itself():
    """A word given as both a keyword and a query term is one word. Counting it
    as two would make "two of your terms agree" true for every single match."""
    kit = article(title="Vote now: worst football kit",
                  content="A bright Solar Yellow trim across the shoulders.")
    assert not matches({"keywords": ["solar"], "queries": ["solar"]}, kit)


# ---------- the reader is told which it was ----------

def test_the_explanation_counts_the_mentions():
    """"Once, in the body" is the difference between a search that is wrong and
    a word that is being used in another sense, and the reader asking why an
    article is in their feed is owed the number."""
    from backend.app.matching import explain_text_match
    hits = explain_text_match({"keywords": ["solar"]}, article(
        title="Ministers back Kent scheme",
        content="The solar project covers 900 acres. Solar developers agreed."))
    assert hits[0]["where"] == "body"
    assert hits[0]["count"] == 2
