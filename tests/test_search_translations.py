"""An English query finding a Korean story.

Delphi reads 23 languages and matched queries against each article's own words,
so a foreign story was invisible to an English feed however relevant it was —
the single largest recall gap in a product whose whole premise is global
coverage. Translations were already being made and stored; they simply played
no part in matching.

Off by default, because switching it on widens an existing feed and that is the
reader's decision.
"""
import pytest

from backend.app.matching import CriteriaMatcher, match_fields, translated_text
from backend.app.models import Article, Source, Translation, utcnow

KO_TITLE = "천안시, 상반기 고용률 70.3% 경신"
KO_SUMMARY = "충남 천안시의 고용 지표가 상승세를 나타냈다."
EN_TITLE = "Cheonan employment rate hits a record in the first half"
EN_SUMMARY = "Employment indicators in the South Chungcheong city rose sharply."


@pytest.fixture
def wire(db):
    s = Source(name="Break News", rss_url="http://break.test/feed", scope="national")
    db.add(s)
    db.commit()
    return s


def _korean_article(db, wire, *, translated=True):
    a = Article(source_id=wire.id, title=KO_TITLE, summary=KO_SUMMARY, content="",
                url=f"http://break.test/{db.query(Article).count()}",
                language="ko", published_at=utcnow(), fetched_at=utcnow(),
                importance=50)
    db.add(a)
    db.flush()
    if translated:
        db.add(Translation(article_id=a.id, lang="en",
                           title=EN_TITLE, summary=EN_SUMMARY))
    db.commit()
    db.refresh(a)
    return a


# --- the gap -------------------------------------------------------------

def test_by_default_an_english_query_cannot_see_a_korean_story(db, wire):
    art = _korean_article(db, wire)
    m = CriteriaMatcher({"queries": ["employment"]})
    assert m.matches(art) is False


def test_with_the_option_on_it_can(db, wire):
    art = _korean_article(db, wire)
    m = CriteriaMatcher({"queries": ["employment"], "search_translations": True})
    assert m.matches(art) is True


def test_the_article_is_still_findable_in_its_own_language(db, wire):
    art = _korean_article(db, wire)
    m = CriteriaMatcher({"queries": ["고용"], "search_translations": True})
    assert m.matches(art) is True


def test_an_untranslated_article_is_unaffected(db, wire):
    art = _korean_article(db, wire, translated=False)
    m = CriteriaMatcher({"queries": ["employment"], "search_translations": True})
    assert m.matches(art) is False


# --- which field a translated word counts as -----------------------------

def test_a_translated_headline_still_counts_as_a_headline(db, wire):
    # `intitle:` asks about the headline. A reader wants the Japanese story
    # whose headline says so; putting the translation only in the body would
    # quietly answer a different question.
    art = _korean_article(db, wire)
    m = CriteriaMatcher({"queries": ["intitle:employment"],
                         "search_translations": True})
    assert m.matches(art) is True


def test_a_failure_record_contributes_no_text(db, wire):
    """An empty title is a record that translation failed, not a translation.

    The summary here is deliberately non-empty. That state cannot arise today —
    a row is only written with a summary when the title came back — and the
    guard is what keeps it from ever mattering if that changes. Written with an
    all-empty row instead, this test passed whether or not the guard was
    there."""
    art = _korean_article(db, wire, translated=False)
    db.add(Translation(article_id=art.id, lang="en", title="",
                       summary="employment indicators rose", attempts=4))
    db.commit()
    db.refresh(art)
    assert translated_text(art) == ""
    m = CriteriaMatcher({"queries": ["employment"], "search_translations": True})
    assert m.matches(art) is False


def test_every_stored_language_is_searched_not_just_one(db, wire):
    """A feed is a standing question and does not know who will read it, so
    'the reader's language' is not available — and picking one would give the
    same feed different contents for different people."""
    art = _korean_article(db, wire)
    db.add(Translation(article_id=art.id, lang="fr",
                       title="Le taux d'emploi de Cheonan atteint un record",
                       summary=""))
    db.commit()
    db.refresh(art)
    m = CriteriaMatcher({"queries": ["emploi"], "search_translations": True})
    assert m.matches(art) is True


# --- the trap this shares with the Hangul bug ----------------------------

def test_a_translation_feed_is_never_narrowed_by_the_index():
    """The index holds an article's own words and not its translations, so
    narrowing with it would return fewer rows than match — the superset
    guarantee broken, which is exactly what the Hangul gap did by another
    route."""
    from backend.app.matching import _fts_expr
    plain = CriteriaMatcher({"queries": ["employment"]})
    assert _fts_expr(plain) is not None

    widened = CriteriaMatcher({"queries": ["employment"],
                               "search_translations": True})
    # The matcher still has a usable expression; the caller must not use it.
    import inspect
    from backend.app import matching
    src = inspect.getsource(matching.query_articles)
    assert "not matcher.search_translations else None" in src


def test_it_is_off_unless_asked_for():
    assert CriteriaMatcher({}).search_translations is False
    assert CriteriaMatcher({"queries": ["x"]}).search_translations is False
    assert CriteriaMatcher({"search_translations": True}).search_translations is True


# --- through the API ------------------------------------------------------

def test_a_feed_remembers_the_setting(client, register, db):
    """It has to survive the schema, which drops anything it does not know."""
    from backend.app.models import Feed
    headers = register("reader")
    feed_id = client.post("/api/feeds", headers=headers, json={
        "name": "Global employment",
        "criteria": {"queries": ["employment"], "search_translations": True},
    }).json()["id"]
    stored = db.get(Feed, feed_id)
    assert stored.criteria["search_translations"] is True

    listed = client.get("/api/feeds", headers=headers).json()
    mine = next(f for f in listed if f["id"] == feed_id)
    assert mine["criteria"]["search_translations"] is True


def test_a_feed_that_did_not_ask_does_not_get_it(client, register, db):
    from backend.app.models import Feed
    headers = register("reader")
    feed_id = client.post("/api/feeds", headers=headers, json={
        "name": "Plain", "criteria": {"queries": ["employment"]},
    }).json()["id"]
    assert db.get(Feed, feed_id).criteria["search_translations"] is False
