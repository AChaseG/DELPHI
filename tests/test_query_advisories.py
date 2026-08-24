"""Valid, and not what it looks like it means.

AND binds tighter than OR, so `a OR b NOT c` is `a OR (b AND NOT c)` — the
exclusion covers the second alternative and nothing else. That is the standard
reading and the engine is right to use it, but almost nobody writing that line
means it: they mean "either of these, and never c". Sports coverage matching
the first alternative then arrives in a feed that explicitly excluded sports.

The precedence is not something to change — it would silently alter every query
already saved, including the ones written by people who did mean it. Saying so
is. These check the advisory says it exactly when it applies.
"""
import pytest

from backend.app.boolean_query import compile_query, query_advisories


def advised(query):
    return bool(query_advisories(query))


def precedence_advised(query):
    """Just the AND-binds-tighter-than-OR note.

    There is more than one advisory now, so a test about this one has to name
    it rather than asking whether anything at all was said."""
    return any("binds tighter" in a for a in query_advisories(query))


def breadth_advised(query):
    """Just the "this query matches everything" note."""
    return any("only an exclusion" in a for a in query_advisories(query))


# ---------- what the trap actually does ----------

def test_the_exclusion_really_does_miss_the_first_alternative():
    """The behaviour the advisory is about, stated as a fact about matching."""
    matches = compile_query('"data center" OR datacenter NOT sports')
    sporty = "The new data center opened. In sports, the WNBA season begins."

    assert matches(sporty), "if this stops being true the advisory is obsolete"
    assert not compile_query('("data center" OR datacenter) NOT sports')(sporty)


# ---------- when it is said ----------

@pytest.mark.parametrize("query", [
    '"data center" OR datacenter NOT sports',
    'a OR b NOT c',
    'a OR b -c',                                  # Google-style negation
    'a OR b AND NOT c',
    'x AND (a OR b) OR c NOT d',
])
def test_an_unbracketed_exclusion_after_an_or_is_flagged(query):
    assert advised(query), query


def test_it_says_what_to_do_about_it():
    note = query_advisories("a OR b NOT c")[0]
    assert "brackets" in note.lower() or "bracket" in note.lower()


# ---------- when it is not ----------

@pytest.mark.parametrize("query", [
    '("data center" OR datacenter) NOT sports',   # already bracketed
    'a AND b NOT c',                              # no OR to be ambiguous about
    'NOT c',
    'a OR b OR c',                                # no exclusion at all
    '(a NOT b) OR c',                             # the NOT is inside the group
    'a NOT b OR c',                               # the NOT comes before the OR
])
def test_an_unambiguous_query_is_left_alone(query):
    assert not precedence_advised(query), query


# ---------- a query that admits everything ----------

@pytest.mark.parametrize("query", [
    'NOT c',                                  # nothing positive at all
    '"data center" OR NOT sports',            # one alternative is only an exclusion
    'datacenter OR -sports',                  # the Google spelling of the same
])
def test_a_query_with_nothing_to_match_is_flagged(query):
    """Not a near miss. A feed built on one of these is every article Delphi
    holds, in every language, with the reader's own terms playing no part."""
    assert breadth_advised(query), query


@pytest.mark.parametrize("query", [
    '"data center" NOT sports',               # the positive half still has to hold
    '(a OR b) NOT c',
    'a AND NOT b',
    'a OR b OR c',
])
def test_a_query_that_still_requires_something_is_not_flagged(query):
    assert not breadth_advised(query), query


def test_a_bracketed_or_elsewhere_does_not_trigger_it():
    """The OR that matters is the one at the same level as the NOT."""
    assert not advised('(a OR b) AND c NOT d')


def test_a_broken_query_gets_no_advice():
    """It already has an error; a second opinion on top of it is noise."""
    assert query_advisories("a OR (b") == []
    assert query_advisories("") == []


def test_advice_is_never_an_error(client, register):
    """The query still saves. This is a note, not a refusal."""
    headers = register("reader")

    r = client.post("/api/query/validate", headers=headers,
                    json={"query": "a OR b NOT c"}).json()

    assert r["valid"] is True
    assert r["error"] is None
    assert len(r["advisories"]) == 1


def test_a_clean_query_carries_no_advisories(client, register):
    headers = register("reader")
    r = client.post("/api/query/validate", headers=headers,
                    json={"query": "(a OR b) NOT c"}).json()
    assert r["valid"] is True and r["advisories"] == []


def test_an_invalid_query_still_reports_its_error(client, register):
    headers = register("reader")
    r = client.post("/api/query/validate", headers=headers,
                    json={"query": "a OR (b"}).json()
    assert r["valid"] is False and r["error"]
    assert r["advisories"] == []


# ---------- bare words that mean something else in the world ----------
#
# From a real report: an energy feed built from ("solar" OR "wind" OR "coal"
# OR "nuclear" OR …) carried a Taiwanese concert listing. Nothing was broken —
# the page lists the performer's songs and one is called "Innocent Wind", so
# the article genuinely contains the word, and whole-word matching found it
# correctly. A bare common word finds every use of it, and most uses of these
# words are not about energy.

from backend.app.boolean_query import AMBIGUOUS_TERMS, ambiguous_terms


ENERGY_QUERY = ('("solar" OR "wind" OR "natural gas" OR "nuclear" OR "coal" '
                'OR "power plant" OR "hydropower" OR "geothermal" '
                'OR "renewable energy" OR "shale gas")')


def test_it_flags_the_query_that_prompted_this():
    assert ambiguous_terms(ENERGY_QUERY) == ["solar", "wind", "nuclear", "coal"]


def test_it_leaves_the_phrases_alone():
    """"natural gas", "power plant", "renewable energy" and "shale gas" are
    already specific — flagging them would be noise, and would teach the
    reader to ignore the advisory."""
    flagged = ambiguous_terms(ENERGY_QUERY)
    for phrase in ("natural gas", "power plant", "renewable energy", "shale gas"):
        assert phrase not in flagged


def test_somebody_who_already_did_the_right_thing_is_not_nagged():
    assert ambiguous_terms('("wind power" OR "solar farm" OR "coal-fired")') == []


def test_a_wildcard_is_a_deliberate_widening():
    """Someone writing wind* has decided to widen it. That is a different
    decision and not one to second-guess."""
    assert ambiguous_terms("wind*") == []
    assert ambiguous_terms("nuclear?") == []


def test_an_unambiguous_query_says_nothing():
    assert ambiguous_terms("earthquake AND japan") == []
    assert query_advisories("earthquake AND japan") == []


def test_the_advice_names_the_word_and_the_fix():
    notes = query_advisories('"wind"')
    assert len(notes) == 1
    assert "wind" in notes[0]
    # Not just "this is ambiguous" — the pairing that fixes it.
    assert "wind power" in notes[0]


def test_a_long_list_is_summarised_rather_than_repeated():
    notes = query_advisories(ENERGY_QUERY)
    assert len(notes) == 4          # three spelled out, one summary line
    assert notes[-1].startswith("1 more bare term ")
    assert "has the same problem" in notes[-1]
    assert "coal" in notes[-1]


def test_the_summary_line_reads_correctly_when_plural():
    notes = query_advisories("wind OR solar OR coal OR nuclear OR oil OR gas")
    assert notes[-1].startswith("3 more bare terms ")
    assert "have the same problem" in notes[-1]


def test_a_broken_query_gets_no_bare_term_advice():
    """Advice on a query that cannot parse is noise on top of an error.

    This used to carry the same name as the test above the ambiguous-terms
    section, so Python bound the name to whichever came second and the first
    was never collected — a test file quietly one test shorter than it read.
    Its second assertion was `== [] or True`, which is true whatever the code
    does; if the parser's behaviour on an unterminated quote is worth pinning,
    it is worth pinning to one answer, so it is asserted here.
    """
    assert ambiguous_terms('"unclosed') == []
    assert query_advisories("AND OR") == []


def test_every_entry_suggests_something_concrete():
    """An advisory without a next move is a complaint."""
    for word, suggestion in AMBIGUOUS_TERMS.items():
        assert word.islower() and " " not in word
        assert '"' in suggestion, f"{word} has no suggested pairing"
        assert word in suggestion.lower(), (
            f"{word}'s suggestion does not contain the word itself")


def test_it_does_not_flag_a_term_that_is_only_ever_specialist():
    """The list has to stay short. Flagging "hydropower" or "earthquake" would
    train people to dismiss the notice."""
    for safe in ("hydropower", "geothermal", "earthquake", "tsunami", "ukraine"):
        assert safe not in AMBIGUOUS_TERMS
