"""What decides importance: the outlet, or what happened.

Both, and the balance between them has been set both ways, so this file records
the arithmetic either version produces rather than only the one in force.

The outlet is worth 32 points — 45/35/25 by scope, +8 for a major wire, −4 for a
niche or local feed — which is more than the whole 30-point breaking-signal
range. Home's headline columns filter on importance ("Top stories" 55, "Breaking
now" 40), so that floor decides which outlets can appear there at all.

It was narrowed to 15 points (42/38/34, +4/−3) for exactly that reason: a wire's
most routine item scored 53 against a threshold of 55 while a city outlet's
report of two people dead in an explosion scored 40, and the front page showed
the same thirty mastheads however many sources were added underneath them.

It is back to 32 because narrowing it broke the other side. Every feed and alert
filters on a minimum importance, and with reach worth 15 points a newly
discovered blog's routine post scores within a few points of a wire's — so a
floor stopped separating outlets worth reading from anything that publishes an
RSS feed, and feeds admitted material their owner did not want. A filter that
discriminates was judged worth more than an evenly-spread board.

The trade is the one it always was, and the tests below state it as numbers so
whoever revisits this can see both costs at once.
"""
import pytest

from backend.app.home import HOME_COLUMNS
from backend.app.scoring import score_importance

TOP = next(c for c in HOME_COLUMNS if c["id"] == "top")["criteria"]["min_importance"]
BREAKING = next(c for c in HOME_COLUMNS if c["id"] == "breaking")["criteria"]["min_importance"]

WIRE = ("international", 1)      # BBC, Reuters — catalog tier 1
DISCOVERED = ("national", 3)     # discovery.py gives every find these
LOCAL = ("local", 3)             # city feeds, and a watched place's search

ROUTINE = "Council approves the new bypass"
SERIOUS = "Two dead after explosion at the docks"
BREAKING_STORY = "BREAKING: magnitude 7.1 earthquake strikes off the coast"


def score(text, source, places=(), corroborating=0):
    scope, tier = source
    return score_importance(text, scope, tier, list(places), corroborating)


# ---------- what the wide spread is for ----------

def test_reach_separates_outlets_far_enough_for_a_floor_to_use():
    """The reason it is wide. A minimum-importance filter is the only tool a
    feed has for "reported by somebody worth reading", and it can only be that
    if the same story from different outlets lands in different bands."""
    assert score(ROUTINE, WIRE) - score(ROUTINE, LOCAL) >= 30


def test_a_discovered_blogs_routine_post_is_far_below_any_floor():
    """Auto-discovery and watched places add sources nobody chose. Their
    everyday output has to sit well under the floors people actually set, or
    setting one achieves nothing."""
    assert score(ROUTINE, DISCOVERED) < BREAKING - 5
    assert score(ROUTINE, LOCAL) < BREAKING - 15


def test_the_same_story_ranks_by_who_carried_it():
    for text in (ROUTINE, SERIOUS, BREAKING_STORY):
        assert score(text, WIRE) > score(text, DISCOVERED) > score(text, LOCAL), text


# ---------- and what it costs ----------

def test_routine_wire_copy_sits_just_under_the_top_stories_threshold():
    """The known cost, stated exactly: two points, closed by any breaking word
    at all. This is the number to look at first if the front page fills up with
    wire copy again."""
    assert score(ROUTINE, WIRE) == 53
    assert score(ROUTINE, WIRE) < TOP


def test_a_solo_local_breaking_story_cannot_reach_top_stories():
    """The other side of the same coin. A city feed's ceiling on a breaking
    story nobody else has yet is 47 against a threshold of 55 — it gets there by
    being corroborated, not by being serious."""
    assert score(BREAKING_STORY, LOCAL, [{"country": "GR"}]) < TOP
    assert score(BREAKING_STORY, LOCAL, [{"country": "GR"}], corroborating=2) >= TOP


def test_corroboration_is_how_a_local_story_climbs():
    """+8 an outlet, to +24. Several independent outlets carrying it is evidence
    about the story, and it is worth more than the whole tier adjustment."""
    alone = score(SERIOUS, LOCAL, [{"country": "GB"}])
    carried = score(SERIOUS, LOCAL, [{"country": "GB"}], corroborating=2)
    assert carried - alone == 16
    assert carried >= BREAKING


def test_the_story_can_still_outweigh_the_whole_spread_of_reach():
    """Reach is 32 points and it is not the largest thing in the score: what
    happened, corroborated, is worth more."""
    reach_spread = score(ROUTINE, WIRE) - score(ROUTINE, LOCAL)
    story_range = score(BREAKING_STORY, LOCAL, [{"country": "GR"}, {"country": "TR"}], 3) \
        - score(ROUTINE, LOCAL)
    assert story_range > reach_spread, \
        f"reach spread {reach_spread} now rivals the {story_range} a story can earn"


def test_routine_local_copy_does_not_flood_the_headline_columns():
    assert score(ROUTINE, LOCAL) < BREAKING
    assert score(ROUTINE, DISCOVERED) < BREAKING


# ---------- invariants, whichever way the balance is set ----------

@pytest.mark.parametrize("source", [WIRE, DISCOVERED, LOCAL])
def test_a_widely_carried_disaster_is_top_news_from_anywhere(source):
    assert score(BREAKING_STORY, source,
                 [{"country": "GR"}, {"country": "TR"}], 3) >= TOP


@pytest.mark.parametrize("source", [WIRE, DISCOVERED, LOCAL])
def test_every_score_stays_inside_the_scale(source):
    for text in ("", ROUTINE, SERIOUS, BREAKING_STORY):
        s = score(text, source, [{"country": "A"}, {"country": "B"}], 9)
        assert 5 <= s <= 100, f"{text!r} from {source} scored {s}"


def test_an_unknown_scope_still_scores_sensibly():
    """Sources arrive from feeds and from people; scope is a free string in the
    database and a typo must not put an article at the bottom of every list."""
    assert BREAKING <= score_importance(SERIOUS, "regional", 2, [{"country": "GB"}]) <= TOP


def test_the_help_quotes_the_numbers_the_code_actually_uses():
    """The help spells out the scoring weights, and stale numbers there are
    worse than none: they are the only place a reader can check what the board
    is doing. This one has caught them twice now — once after the narrowing and
    once after it was undone.
    """
    import re
    from pathlib import Path

    from backend.app.scoring import _SCOPE_BASE, _TIER_ADJUST

    html = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
    text = " ".join(html.read_text(encoding="utf8").split())
    claim = re.search(r"base score by scope: international (\d+), national (\d+),\s*"
                      r"local (\d+); major wires \(tier 1\) \+(\d+), "
                      r"niche/local outlets \(tier 3\) −(\d+)", text)
    assert claim, "the help no longer states the weights in the form this checks"
    intl, nat, loc, up, down = (int(g) for g in claim.groups())

    assert (intl, nat, loc) == (_SCOPE_BASE["international"], _SCOPE_BASE["national"],
                                _SCOPE_BASE["local"])
    assert up == _TIER_ADJUST[1] and down == -_TIER_ADJUST[3]
