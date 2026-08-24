"""Translation that fails should say so, back off, and eventually stop asking.

A failure used to store nothing at all. That had three consequences and all of
them reached a reader: the article was retried by every subsequent page load
for ever; an article the provider will never translate was retried hardest of
all; and because the "translated from" label only appears when a translation
exists, a failure looked exactly like an article that was already in the
reader's language.
"""
from datetime import datetime, timedelta

import pytest

from backend.app import translate
from backend.app.models import Article, Source, Translation


@pytest.fixture(autouse=True)
def _translating(monkeypatch):
    # The suite runs with NEWS_TRANSLATE_PROVIDER=off so no test ever reaches
    # a network. These tests are about what happens when the provider answers
    # badly, so they need it switched on and stubbed.
    monkeypatch.setattr(translate, "PROVIDER", "google")
    before = dict(translate.stats)
    translate.stats.update({"translated": 0, "failed": 0, "gave_up": 0})
    yield
    translate.stats.update(before)


def _article(db, *, lang="ko", title="천안시 고용률 경신", summary="충남 천안시의 고용 지표"):
    src = db.query(Source).first()
    if src is None:
        src = Source(name="Wire", rss_url="https://w.test/rss", scope="international")
        db.add(src)
        db.flush()
    art = Article(source_id=src.id, url=f"https://w.test/{title}-{db.query(Article).count()}",
                  title=title, summary=summary, language=lang)
    db.add(art)
    db.flush()
    return art


async def _run(db, articles, lang="en"):
    return await translate.translate_articles(db, articles, lang)


# --- the quality check -----------------------------------------------------

def test_an_empty_answer_is_not_a_translation():
    assert not translate._looks_translated("천안시 고용률 경신", "", "en")
    assert not translate._looks_translated("천안시 고용률 경신", "   ", "en")


def test_the_input_handed_straight_back_is_not_a_translation():
    # The free endpoint returns the input unchanged when it cannot do anything
    # with it, and storing that caches a non-answer for ever.
    same = "Cheonan employment rate hits a record"
    assert not translate._looks_translated(same, same, "en")


def test_a_short_proper_noun_surviving_unchanged_is_fine():
    # "Samsung" translates to "Samsung". Rejecting that would cost more than
    # it saves.
    assert translate._looks_translated("Samsung", "Samsung", "en")
    assert translate._looks_translated("Seoul", "Seoul", "en")


def test_a_real_translation_passes():
    assert translate._looks_translated("천안시 고용률 경신",
                                       "Cheonan employment rate renewed", "en")


# --- failure records -------------------------------------------------------

def test_a_failure_is_recorded_rather_than_forgotten(db, monkeypatch):
    art = _article(db)
    monkeypatch.setattr(translate, "_google", _boom)
    _sync(_run(db, [art]))
    row = db.query(Translation).filter_by(article_id=art.id, lang="en").one()
    assert row.title == ""          # a record of failure, not a translation
    assert row.attempts == 1
    assert row.next_try_at is not None
    assert translate.stats["failed"] == 1


def test_the_backoff_stops_an_immediate_retry(db, monkeypatch):
    art = _article(db)
    monkeypatch.setattr(translate, "_google", _boom)
    _sync(_run(db, [art]))
    calls = []
    monkeypatch.setattr(translate, "_google", _counting(calls))
    _sync(_run(db, [art]))
    assert calls == []              # still inside the backoff window


def test_it_tries_again_once_the_backoff_expires(db, monkeypatch):
    art = _article(db)
    monkeypatch.setattr(translate, "_google", _boom)
    _sync(_run(db, [art]))
    row = db.query(Translation).filter_by(article_id=art.id).one()
    row.next_try_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    _sync(_run(db, [art]))
    db.refresh(row)
    assert row.attempts == 2


def test_it_gives_up_after_enough_attempts(db, monkeypatch):
    art = _article(db)
    row = Translation(article_id=art.id, lang="en", title="", summary="",
                      attempts=translate.MAX_ATTEMPTS,
                      next_try_at=datetime.utcnow() - timedelta(days=1))
    db.add(row)
    db.commit()
    calls = []
    monkeypatch.setattr(translate, "_google", _counting(calls))
    _sync(_run(db, [art]))
    assert calls == []              # asked enough times; it is not coming


def test_a_later_success_replaces_the_failure_record(db, monkeypatch):
    art = _article(db)
    monkeypatch.setattr(translate, "_google", _boom)
    _sync(_run(db, [art]))
    row = db.query(Translation).filter_by(article_id=art.id).one()
    row.next_try_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    monkeypatch.setattr(translate, "_google", _ok("Cheonan employment record"))
    out = _sync(_run(db, [art]))
    assert out[art.id]["title"] == "Cheonan employment record"
    # Filled in, not inserted beside — the unique constraint allows only one.
    assert db.query(Translation).filter_by(article_id=art.id, lang="en").count() == 1
    db.refresh(row)
    assert row.next_try_at is None


def test_a_failure_record_is_never_served_as_a_translation(db, monkeypatch):
    art = _article(db)
    db.add(Translation(article_id=art.id, lang="en", title="", summary="",
                       attempts=translate.MAX_ATTEMPTS))
    db.commit()
    out = _sync(_run(db, [art]))
    assert art.id not in out        # the reader gets the original, not ""


# --- the counter the operator console reads --------------------------------

def test_pending_separates_cached_from_failing(db):
    a, b, c = _article(db), _article(db, title="지진"), _article(db, title="태풍")
    db.add_all([
        Translation(article_id=a.id, lang="en", title="ok", summary=""),
        Translation(article_id=b.id, lang="en", title="", summary="", attempts=1),
        Translation(article_id=c.id, lang="en", title="", summary="",
                    attempts=translate.MAX_ATTEMPTS),
    ])
    db.commit()
    got = translate.pending(db, "en")
    assert got == {"cached": 1, "failing": 2, "given_up": 1, "lang": "en"}


# --- helpers ---------------------------------------------------------------

def _sync(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _boom(client, text, target):
    raise RuntimeError("provider said no")


def _counting(calls):
    async def fn(client, text, target):
        calls.append(text)
        return "x"
    return fn


def _ok(answer):
    async def fn(client, text, target):
        return answer
    return fn
