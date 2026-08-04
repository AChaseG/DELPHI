"""Translation must be done before the reader arrives, not while they wait.

A board column of forty foreign-language stories was costing a reader dozens of
calls to a translation service inside their own request — `slow:7.2s POST
/api/articles/search` in the live log — because a translation is only cached
after the first person has paid for it.

The obvious fix is the wrong one. Serving the original text immediately and
translating afterwards makes the page appear sooner while making it useless:
someone who reads only English is not better served by Mandarin arriving
quickly. So the work is moved rather than skipped — Home's warmed stories are
translated between poll cycles, into the languages this server's accounts
actually read, so the request finds them already in the database.

Two things are pinned here: that the warm-up exists and is wired to the poller,
and that a title and its summary are fetched together rather than one after the
other, which was doubling the depth of every article that did have to be
translated live.
"""
import asyncio
import inspect
import json

import pytest

from backend.app import home, ingest, translate
from backend.app.models import Article, Source, User, utcnow


# ---------- the wait is moved off the reader's request ----------

def test_the_poller_warms_translations_after_it_warms_home():
    loop = inspect.getsource(ingest.ingest_loop)
    assert "await warm_translations()" in loop, (
        "nothing translates Home's stories ahead of a reader")
    assert loop.index("await warm_home()") < loop.index("await warm_translations()"), (
        "translations are warmed before the columns that decide what to warm")


def test_it_translates_exactly_what_home_will_show(db):
    """Warming the wrong set would be pure waste: the point is that the next
    reader's request is a cache hit."""
    body = inspect.getsource(ingest.warm_translations)
    assert "home.warm_article_ids()" in body


def test_warm_article_ids_reports_each_story_once(db):
    """Columns overlap — a big story is in several — and translating it once
    per column would multiply the cost by the number of columns."""
    home.clear()
    home._warm = {"a": ([1, 2, 3], 0.0), "b": ([3, 2, 9], 0.0)}
    try:
        assert sorted(home.warm_article_ids()) == [1, 2, 3, 9]
    finally:
        home.clear()


def test_nothing_warm_means_nothing_to_do(db):
    home.clear()
    assert home.warm_article_ids() == []


# ---------- only the languages someone actually reads ----------

def _user(db, name, settings):
    u = User(username=name, email=f"{name}@x.test", password_hash="x",
             settings=json.dumps(settings) if settings is not None else None)
    db.add(u)
    db.commit()
    return u


def test_only_languages_accounts_have_chosen(db):
    _user(db, "ana", {"lang": "en"})
    _user(db, "bo", {"lang": "fr"})
    _user(db, "cy", {"lang": "en"})
    assert sorted(ingest.reading_languages(db)) == ["en", "fr"]


def test_an_account_with_no_settings_asks_for_nothing(db):
    _user(db, "dee", None)
    _user(db, "eli", {})
    assert ingest.reading_languages(db) == []


def test_a_corrupt_settings_blob_is_skipped_not_fatal(db):
    u = User(username="fay", email="fay@x.test", password_hash="x",
             settings="{not json")
    db.add(u)
    _user(db, "gil", {"lang": "de"})
    db.commit()
    assert ingest.reading_languages(db) == ["de"]


def test_a_server_nobody_reads_translates_nothing(db):
    """Sixteen languages are offered. Translating into all of them because
    they exist would cost sixteen times what anyone needs."""
    assert ingest.reading_languages(db) == []
    assert len(translate.UI_LANGUAGES) > 1


# ---------- a live translation is one round-trip deep, not two ----------

def test_title_and_summary_are_fetched_together():
    body = inspect.getsource(translate._translate_one)
    assert "asyncio.gather(" in body, (
        "the summary waits for the title again; that doubles the depth of "
        "every article a reader does have to wait for")


def test_an_article_without_a_summary_still_works():
    body = inspect.getsource(translate._translate_one)
    assert "_nothing()" in body, "the gather is asymmetric when there is no summary"


@pytest.mark.anyio
async def test_both_halves_are_actually_in_flight_at_once(monkeypatch):
    """The proxy above only reads the source. This runs it: with a provider
    that takes 50ms a call, an article must cost about 50ms, not 100."""
    order = []

    async def fake(_client, text, _target):
        order.append(("start", text))
        await asyncio.sleep(0.05)
        order.append(("end", text))
        return f"[{text}]"

    monkeypatch.setattr(translate, "_google", fake)
    monkeypatch.setattr(translate, "PROVIDER", "google")
    article = Article(id=1, title="titre", summary="resume", language="fr",
                      url="http://x/1", published_at=utcnow())

    loop = asyncio.get_event_loop()
    started = loop.time()
    _id, title, summary = await translate._translate_one(
        None, asyncio.Semaphore(4), article, "en")
    took = loop.time() - started

    assert (title, summary) == ("[titre]", "[resume]")
    assert took < 0.09, f"the two halves ran in series: {took:.3f}s"
    # Both started before either finished — the definition of concurrent.
    assert order[0][0] == "start" and order[1][0] == "start", order


# ---------- failure must not stop the news ----------

@pytest.mark.anyio
async def test_a_broken_translation_service_does_not_break_the_cycle(monkeypatch):
    """It runs inside the poll loop. An exception escaping here would stop
    ingestion over a service that is merely unavailable."""
    def boom():
        raise RuntimeError("translation service is down")

    monkeypatch.setattr(translate, "enabled", lambda: True)
    monkeypatch.setattr(home, "warm_article_ids", boom)
    await ingest.warm_translations()      # must not raise


@pytest.mark.anyio
async def test_translation_switched_off_costs_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(translate, "enabled", lambda: False)
    monkeypatch.setattr(home, "warm_article_ids", lambda: called.append(1) or [])
    await ingest.warm_translations()
    assert not called, "the warm-up looked up work it could never do"


@pytest.fixture
def anyio_backend():
    return "asyncio"
