"""What decides importance: the outlet, or what happened.

Home's two headline columns filter on importance — "Top stories" wants 55,
"Breaking now" 40 — so the floor a source starts from decides whether its
reporting can appear there at all. That floor used to run from 53 for an
international tier-1 wire down to 21 for a city feed: 32 points settled before
anything is known about the story, against 30 for the entire breaking-signal
range. The result was that a wire's most routine item outscored a local
outlet's report of people killed, and the board looked the same whether the
catalog held thirty sources or a thousand.

Reach still counts — who is carrying something is evidence about how much it
matters — it is just no longer worth more than the event.
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


def test_a_local_outlets_serious_story_outranks_a_wires_routine_one():
    """The case that was backwards. Two people dead is a bigger story than a
    council meeting whoever reports it."""
    assert score(SERIOUS, LOCAL, [{"country": "GB"}]) > score(ROUTINE, WIRE)


def test_a_local_outlet_can_reach_the_top_stories_column():
    """It could not before: a city feed's ceiling on a solo breaking story was
    47, and the column asks for 55."""
    assert score(BREAKING_STORY, LOCAL, [{"country": "GR"}]) >= TOP


def test_a_wires_routine_item_no_longer_walks_into_top_stories():
    """It scored 53 against a threshold of 55 — two points, closed by any
    breaking word at all. That is what filled the column with wire copy."""
    assert score(ROUTINE, WIRE) < TOP


def test_reach_still_counts_for_something():
    """Narrowing the gap is not removing it. The same story from a wire still
    ranks above the same story from a newly discovered blog."""
    for text in (ROUTINE, SERIOUS, BREAKING_STORY):
        assert score(text, WIRE) > score(text, DISCOVERED) > score(text, LOCAL), text


def test_the_story_can_outweigh_the_whole_spread_of_reach():
    """The point of the change, stated as a number: what happened is worth more
    than every difference between outlets put together."""
    reach_spread = score(ROUTINE, WIRE) - score(ROUTINE, LOCAL)
    story_range = score(BREAKING_STORY, LOCAL, [{"country": "GR"}, {"country": "TR"}], 3) \
        - score(ROUTINE, LOCAL)
    assert story_range > reach_spread, \
        f"reach spread {reach_spread} still rivals the {story_range} a story can earn"


def test_routine_local_copy_does_not_flood_the_headline_columns():
    """The other failure mode. Lifting the floor must not put a council meeting
    on the front page."""
    assert score(ROUTINE, LOCAL) < BREAKING
    assert score(ROUTINE, DISCOVERED) < BREAKING


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
    is doing. This one caught them already reading 45/35/25 after the change.
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
