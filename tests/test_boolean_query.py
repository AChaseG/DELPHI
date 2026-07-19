"""Boolean search operators: AND/OR/NOT, phrases, wildcards, NEAR, errors."""
from backend.app.boolean_query import compile_query, validate_query


def m(q, text):
    return compile_query(q)(text)


def test_and_or_not_and_phrases():
    assert m('(a OR b) AND c NOT d', "b and c here")
    assert not m('(a OR b) AND c NOT d', "b and c and d")
    assert m('"supply chain"', "the global supply chain buckles")
    assert not m('"supply chain"', "supply and demand chain")


def test_google_style_synonyms():
    assert m("-sports breaking", "breaking news on politics")
    assert not m("-sports breaking", "breaking sports coverage")
    assert m("a | b", "only b present")


def test_wildcards():
    assert m("strik*", "dock workers strike again")
    assert m("strik*", "striking imagery")
    assert not m("strik*", "we ate strudel")
    assert m("organi?ation", "the organisation") and m("organi?ation", "the organization")
    assert not m("organi?ation", "the organiszation")


def test_near_proximity():
    assert m("earthquake NEAR/3 tokyo", "earthquake shakes central tokyo")
    assert m("earthquake NEAR/3 tokyo", "tokyo felt the earthquake")
    assert not m("earthquake NEAR/3 tokyo", "earthquake far from the city of tokyo today now")
    assert m("earthquake NEAR tokyo", "earthquake hit and later shook greater tokyo")


def test_invalid_queries_reported():
    for bad in ["n* here", "(a b) NEAR/3 c", "NEAR/3 b", "a NEAR/x b", "a NEAR/500 b"]:
        assert validate_query(bad) is not None, bad
    assert validate_query('("supply chain" OR semiconductor) AND (china | taiwan) -rumor') is None
