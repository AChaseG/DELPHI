"""Ordered proximity, the positional operator, and the prominence dial.

Measured against Factiva and LexisNexis: Delphi had unordered proximity,
wildcards and field scoping, and was missing ordered proximity, any positional
operator, and any way for a reader to control the prominence rule it has always
applied invisibly.
"""
import pytest

from backend.app.boolean_query import QueryError, Text, compile_query
from backend.app.matching import CriteriaMatcher, lede_text, match_fields
from backend.app.models import Article

BOEING_CRASH = "A Boeing 737 crash near the airport killed nobody"
CRASH_BOEING = "The crash involved a Boeing aircraft on approach"


def hit(query, text):
    return compile_query(query)(Text.plain(text))


# --- ordered proximity ----------------------------------------------------

def test_onear_respects_the_order_its_operands_are_written_in():
    # The reason every professional system offers both: "Boeing before crash"
    # and "crash before Boeing" are different claims about the world.
    assert hit("Boeing ONEAR/3 crash", BOEING_CRASH)
    assert not hit("Boeing ONEAR/3 crash", CRASH_BOEING)
    assert hit("crash ONEAR/3 Boeing", CRASH_BOEING)


def test_near_still_ignores_order():
    assert hit("Boeing NEAR/3 crash", BOEING_CRASH)
    assert hit("Boeing NEAR/3 crash", CRASH_BOEING)


@pytest.mark.parametrize("spelling", ["ONEAR", "PRE", "W"])
def test_the_spellings_readers_arrive_with_all_work(spelling):
    """PRE is LexisNexis, W is Factiva. Somebody who already writes one should
    not have to learn a third word to say the same thing."""
    assert hit(f"Boeing {spelling}/3 crash", BOEING_CRASH)
    assert not hit(f"Boeing {spelling}/3 crash", CRASH_BOEING)


def test_the_distance_is_honoured():
    far = "Boeing said on Tuesday that the long delayed inquiry into the crash"
    assert not hit("Boeing ONEAR/2 crash", far)
    assert hit("Boeing ONEAR/12 crash", far)


def test_a_bad_distance_is_refused_by_name():
    with pytest.raises(QueryError, match="ONEAR"):
        compile_query("a ONEAR/500 b")
    with pytest.raises(QueryError, match="PRE"):
        compile_query("a PRE/x b")


def test_ordered_proximity_works_in_an_unspaced_script():
    assert hit("地震 ONEAR/5 津波", "東京で地震が発生し津波警報が出された")
    assert not hit("津波 ONEAR/5 地震", "東京で地震が発生し津波警報が出された")


def test_it_refuses_groups_the_way_near_does():
    with pytest.raises(QueryError, match="ONEAR"):
        compile_query("(a OR b) ONEAR/3 c")


# --- the positional operator ----------------------------------------------

def _article(**kw):
    base = dict(id=1, title="", summary="", content="", importance=40)
    base.update(kw)
    return Article(**base)


def test_inlede_finds_a_word_at_the_top_and_not_one_buried():
    art = _article(title="New data center approved", summary="The county voted.",
                   content=" ".join(["filler"] * 200) + " cryptocurrency mining")
    fields = match_fields(art)
    assert compile_query('inlede:"data center"')(fields)
    assert not compile_query("inlede:cryptocurrency")(fields)
    # It is still in the article; inlede is about where, not whether.
    assert compile_query("intext:cryptocurrency")(fields)


@pytest.mark.parametrize("spelling", ["inlede", "lede", "intro"])
def test_the_lede_spellings_all_work(spelling):
    art = _article(title="New data center approved")
    assert compile_query(f'{spelling}:"data center"')(match_fields(art))


def test_the_lede_is_the_headline_summary_and_opening():
    art = _article(title="Headline here", summary="Standfirst here",
                   content="Opening line. " + " ".join(["later"] * 500))
    text = lede_text(art)
    assert "Headline here" in text and "Standfirst here" in text
    assert "Opening line." in text
    assert text.count("later") < 500, "the whole body is not the lede"


def test_a_pun_headline_is_still_found_by_the_lede():
    """The reason inlede exists beside intitle: news headlines are often
    oblique, and the standfirst underneath is where the story says plainly
    what it is about."""
    art = _article(title="Chips are down", summary="A semiconductor plant closed.")
    fields = match_fields(art)
    assert not compile_query("intitle:semiconductor")(fields)
    assert compile_query("inlede:semiconductor")(fields)


# --- the prominence dial --------------------------------------------------

KIT_REVIEW = _article(title="Solar Yellow kit revealed", summary="A new strip.",
                      content="The Solar Yellow colourway is bold.")
SOLAR_FARM = _article(id=2, title="County approves array", summary="Panels go up.",
                      content="The solar farm will power homes. "
                              "Solar output rises. More solar capacity here.")


def test_the_default_is_what_it_always_was():
    assert CriteriaMatcher({}).min_mentions == 2


def test_raising_it_drops_the_passing_mention_and_keeps_the_story():
    """The case the hidden rule was written for, now with a dial: a kit review
    says an energy word once, a story about a solar farm says it again."""
    m = CriteriaMatcher({"queries": ["solar"], "min_mentions": 3})
    assert m.matches(KIT_REVIEW) is False
    assert m.matches(SOLAR_FARM) is True


def test_raising_it_further_eventually_excludes_both():
    m = CriteriaMatcher({"queries": ["solar"], "min_mentions": 6})
    assert m.matches(SOLAR_FARM) is False


def test_above_the_default_a_headline_is_no_longer_a_free_pass():
    """A reader who asked for five is saying one mention is what they want
    excluded. Honouring the headline shortcut would make the setting do
    nothing in the case it was raised for."""
    headline_only = _article(id=3, title="Solar panels approved",
                             summary="", content="The vote passed quietly.")
    assert CriteriaMatcher({"queries": ["solar"]}).matches(headline_only) is True
    assert CriteriaMatcher({"queries": ["solar"],
                            "min_mentions": 4}).matches(headline_only) is False


def test_it_is_bounded_at_both_ends():
    from backend.app.matching import MAX_MENTIONS
    assert CriteriaMatcher({"min_mentions": 999}).min_mentions == MAX_MENTIONS
    assert CriteriaMatcher({"min_mentions": -5}).min_mentions == 1
    assert CriteriaMatcher({"min_mentions": 0}).min_mentions == 2   # 0 = unset


def test_a_feed_remembers_it(client, register, db):
    from backend.app.models import Feed
    headers = register("reader")
    feed_id = client.post("/api/feeds", headers=headers, json={
        "name": "Energy", "criteria": {"queries": ["solar"], "min_mentions": 4},
    }).json()["id"]
    assert db.get(Feed, feed_id).criteria["min_mentions"] == 4
