"""Boolean search operators: AND/OR/NOT, phrases, wildcards, NEAR, errors."""
from backend.app.boolean_query import compile_query, fts_expression, validate_query


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


def test_index_expression_keeps_the_query_shape():
    """The index gets the query's structure, not a flat bag of words: an AND
    hands it the intersection, which is what makes it selective."""
    assert fts_expression("earthquake") == '"earthquake"'
    assert fts_expression("(tokyo OR osaka) AND earthquake") == \
        '(("tokyo" OR "osaka") AND "earthquake")'
    # A NOT branch is dropped, never translated — the other side alone is still
    # a superset, and the matcher applies the exclusion exactly.
    assert fts_expression("earthquake NOT opinion") == '"earthquake"'


def test_index_expression_declines_what_it_cannot_promise():
    """None means "scan instead". Anything the index might tokenize differently
    from the Python matcher has to take that route, or matches go missing."""
    for unsafe in ["NOT rumor",              # nothing bounds it
                   "strik*",                 # wildcard
                   "organi?ation",
                   "地震",                    # unsegmented script
                   "a OR NOT b",             # one unbounded side of an OR
                   "earthquake NEAR/3 tokyo"]:  # no recoverable terms
        assert fts_expression(unsafe) is None, unsafe


def test_index_expression_matches_a_superset_of_the_query():
    """The property the whole optimization rests on: every text the query
    accepts, the index expression must also accept."""
    import re

    def fts_accepts(expr, text):
        """Enough of FTS5 to check the promise: phrases, AND, OR, parentheses."""
        expr = re.sub(r'"([^"]*)"',
                      lambda m: "T" if re.search(
                          r"\b" + re.escape(m.group(1)) + r"\b", text, re.I) else "F",
                      expr)
        return eval(expr.replace("AND", "and").replace("OR", "or")
                    .replace("T", "True").replace("F", "False"))

    texts = ["earthquake shakes tokyo", "osaka earthquake damage", "quiet day in tokyo",
             "an opinion piece about an earthquake", "nothing relevant at all"]
    for query in ["earthquake", "(tokyo OR osaka) AND earthquake",
                  "earthquake NOT opinion", '"supply chain" OR earthquake']:
        expr = fts_expression(query)
        assert expr is not None, query
        for text in texts:
            if compile_query(query)(text):
                assert fts_accepts(expr, text), f"{query!r} lost {text!r}"


def test_invalid_queries_reported():
    for bad in ["n* here", "(a b) NEAR/3 c", "NEAR/3 b", "a NEAR/x b", "a NEAR/500 b"]:
        assert validate_query(bad) is not None, bad
    assert validate_query('("supply chain" OR semiconductor) AND (china | taiwan) -rumor') is None
