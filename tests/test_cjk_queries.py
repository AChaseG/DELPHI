"""Queries written in scripts that do not put spaces between words.

Every term in the Boolean engine compiled to `\\bTERM\\b`. A word boundary is a
transition between a word character and a non-word one, and in Chinese,
Japanese, Korean and Thai every character is a word character — so no term in
any of those languages could ever match. The query parsed, validated, and
returned nothing for ever, which is the worst shape a bug can take.

`scoring.py` had found and fixed this for its own matching and documented it in
a comment. The Boolean engine and the keyword path each kept their own boundary
rule and never learned. The tests at the foot of this file are the ones that
stop the three drifting apart again.
"""
import re
import sqlite3

import pytest

from backend.app import boolean_query as bq
from backend.app import matching, scoring
from backend.app.boolean_query import Text, compile_query, fts_expression

KO = "천안시, 상반기 고용률 70.3% 경신 '취업자 40만 명' 돌파"
JA = "東京で大規模な地震が発生し、津波警報が出された"
ZH = "台积电宣布在亚利桑那州新建半导体工厂"
TH = "เกิดแผ่นดินไหวขนาดใหญ่ในกรุงเทพมหานคร"


def _hit(query, text):
    return compile_query(query)(Text(all=text, headline=text, text="",
                                     source="", site=""))


# --- the bug ---------------------------------------------------------------

@pytest.mark.parametrize("term,text", [
    ("고용", KO), ("천안시", KO),
    ("地震", JA), ("津波", JA),
    ("半导体", ZH), ("工厂", ZH),
    ("แผ่นดินไหว", TH),
])
def test_a_term_in_an_unspaced_script_matches(term, text):
    assert _hit(term, text)


@pytest.mark.parametrize("query,text,expected", [
    ("地震 AND 津波", JA, True),
    ("地震 AND 火災", JA, False),
    ("地震 OR 火災", JA, True),
    ("地震 NOT 津波", JA, False),
    ("半导体 AND (工厂 OR 晶圆)", ZH, True),
])
def test_the_operators_work_on_them_too(query, text, expected):
    assert _hit(query, text) is expected


def test_a_mixed_term_keeps_a_boundary_on_its_latin_end():
    # "AI기업" is how these languages actually write about technology: the
    # Latin end needs a real boundary, the Hangul end must not have one.
    assert _hit("AI기업", "국내 AI기업들이 투자를 늘리고 있다")
    assert not _hit("AI기업", "PAI기업")


def test_proximity_works_in_an_unspaced_script():
    # "Words" is the wrong unit where words are not spaced, so the gap is
    # counted in characters — the alternative was NEAR matching nothing.
    assert _hit("地震 NEAR/5 津波", JA)
    assert not _hit("地震 NEAR/1 警報", JA)


# --- the guard that must survive the fix -----------------------------------

@pytest.mark.parametrize("term,text", [
    ("quake", "An earthquake struck the coast"),
    ("art", "A large cartoon appeared"),
    ("cat", "The concatenation failed"),
])
def test_a_latin_term_still_needs_a_word_boundary(term, text):
    assert not _hit(term, text)


def test_latin_phrases_and_wildcards_are_unaffected():
    assert _hit('"supply chain"', "a global supply chain problem")
    assert _hit("strik*", "workers are striking today")
    assert not _hit("strik*", "a strident tone")


# --- the index has to agree with the matcher -------------------------------

@pytest.mark.parametrize("ch,script", [
    ("ま", "kana"), ("震", "han"), ("고", "hangul"), ("ก", "thai"),
])
def test_every_unspaced_script_is_kept_away_from_the_index(ch, script):
    # SQLite's tokenizer reads a run of these as one indivisible token, so a
    # substring term finds nothing and the row is dropped before the matcher
    # sees it — which breaks the superset guarantee narrowing depends on.
    assert bq.FTS_UNSAFE.search(ch), script
    assert fts_expression(ch) is None


def test_sqlite_really_does_drop_the_row():
    # The reason for the rule above, asserted rather than assumed.
    db = sqlite3.connect(":memory:")
    db.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
    db.execute("INSERT INTO t VALUES (?)", (KO,))
    got = db.execute('SELECT count(*) FROM t WHERE t MATCH ?', ('"고용"',)).fetchone()[0]
    assert got == 0
    db.close()


def test_latin_queries_still_reach_the_index():
    assert fts_expression("earthquake") == '"earthquake"'
    assert fts_expression("tokyo AND earthquake") is not None


# --- one definition, used everywhere ---------------------------------------

def test_the_three_boundary_rules_are_the_same_object():
    # They were three separate range lists that disagreed about Hangul. A
    # shared object cannot disagree with itself.
    assert scoring._CJK_RE is bq._UNSPACED_SCRIPT
    assert matching._FTS_UNSAFE is bq.FTS_UNSAFE


@pytest.mark.parametrize("ch", ["ま", "震", "고", "ก", "한"])
def test_every_module_agrees_a_character_is_unspaced(ch):
    assert bq.unspaced(ch)
    assert scoring._CJK_RE.search(ch)
    assert matching._FTS_UNSAFE.search(ch)


@pytest.mark.parametrize("ch", ["a", "Z", "é", "ñ", "щ"])
def test_every_module_agrees_a_spaced_script_is_spaced(ch):
    assert not bq.unspaced(ch)
    assert not scoring._CJK_RE.search(ch)
    assert not matching._FTS_UNSAFE.search(ch)


# --- the keyword path is the same thing to a reader ------------------------

def test_a_plain_keyword_matches_cjk_too():
    assert matching._kw_regex("地震").search(JA)
    assert matching._kw_regex("고용").search(KO)


def test_a_plain_keyword_still_bounds_latin():
    assert not matching._kw_regex("quake").search("An earthquake struck")
