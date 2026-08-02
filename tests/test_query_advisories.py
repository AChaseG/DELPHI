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
    assert not advised(query), query


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
