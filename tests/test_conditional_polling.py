"""Conditional polling, and the backlog of article bodies it pays for."""
from datetime import timedelta

import httpx
import pytest

from backend.app import ingest
from backend.app.models import Article, Source, utcnow

FEED = """<?xml version="1.0"?><rss version="2.0"><channel><title>W</title>
<item><title>First report</title><link>http://w.test/1</link>
<guid>1</guid><description>Something happened.</description></item>
</channel></rss>"""


def _source(db, **kw):
    src = Source(name="Wire", rss_url="http://w.test/feed", scope="national",
                 tier=2, added_by="catalog", **kw)
    db.add(src)
    db.commit()
    return src


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run(coro):
    import asyncio
    return asyncio.run(coro)


def test_a_first_poll_remembers_what_to_ask_with_next_time(db):
    src = _source(db)
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, text=FEED,
                              headers={"ETag": '"abc123"',
                                       "Last-Modified": "Wed, 30 Jul 2026 09:00:00 GMT"})

    async def go():
        async with _client(handler) as c:
            return await ingest.fetch_source(c, src)

    _, entries, status = run(go())
    assert status == "ok" and len(entries) == 1
    # Nothing to send on a first poll, so nothing was sent.
    assert "if-none-match" not in seen
    assert src.etag == '"abc123"'
    assert src.last_modified == "Wed, 30 Jul 2026 09:00:00 GMT"


def test_an_unchanged_feed_is_not_downloaded_or_parsed(db):
    src = _source(db, etag='"abc123"', last_modified="Wed, 30 Jul 2026 09:00:00 GMT")
    asked = {}

    def handler(request):
        asked.update(request.headers)
        return httpx.Response(304)

    async def go():
        async with _client(handler) as c:
            return await ingest.fetch_source(c, src)

    _, entries, status = run(go())
    assert asked["if-none-match"] == '"abc123"'
    assert asked["if-modified-since"] == "Wed, 30 Jul 2026 09:00:00 GMT"
    assert status == ingest.UNCHANGED
    assert entries == []


def test_an_unchanged_poll_is_a_success_not_a_failure(db, monkeypatch):
    """A 304 means the source answered. Counting it as a failure would send a
    perfectly healthy feed into self-repair after two quiet polls."""
    src = _source(db, etag='"abc123"', consecutive_failures=1)

    # _ingest_batch builds its own client, so the whole fetch step is replaced.
    async def fetched(sources):
        return [(s, [], ingest.UNCHANGED) for s in sources]

    monkeypatch.setattr(ingest, "_fetch_batch", fetched)

    stats = run(ingest._ingest_batch(db, [src]))
    assert stats["unchanged"] == 1
    assert stats["sources_ok"] == 1
    assert stats["new_articles"] == 0
    assert src.consecutive_failures == 0
    assert src.last_status == ingest.UNCHANGED


def test_a_validator_is_kept_when_a_later_response_omits_it(db):
    """A server that sends an ETag once and not next time has not withdrawn it;
    forgetting would quietly turn conditional polling back off."""
    src = _source(db, etag='"abc123"')

    def handler(request):
        return httpx.Response(200, text=FEED)      # no validators at all

    async def go():
        async with _client(handler) as c:
            return await ingest.fetch_source(c, src)

    run(go())
    assert src.etag == '"abc123"'


def test_the_backlog_picks_up_articles_a_busy_tick_skipped(db, monkeypatch):
    src = _source(db)
    now = utcnow()
    skipped = [Article(source_id=src.id, url=f"http://w.test/a{i}", guid=f"a{i}",
                       title=f"Report {i}", summary="", content="",
                       published_at=now - timedelta(hours=i), fetched_at=now,
                       language="en", country="US", categories=[], places=[],
                       importance=50) for i in range(4)]
    db.add_all(skipped)
    db.commit()

    async def fake_fetch(client, url):
        return f"the body of {url}"

    monkeypatch.setattr(ingest, "fetch_article_text", fake_fetch)

    got = run(ingest.backfill_content(db, spare=3))
    assert got == 3
    # Newest first: the ones a reader is most likely to be looking at.
    assert [a.content != "" for a in skipped] == [True, True, True, False]


def test_a_page_that_cannot_be_fetched_is_not_retried_every_tick(db, monkeypatch):
    """Without this the backlog would return the same dead URL forever and
    never advance to the articles behind it."""
    src = _source(db)
    dead = Article(source_id=src.id, url="http://w.test/gone", guid="gone",
                   title="Unreachable", summary="", content="",
                   published_at=utcnow(), fetched_at=utcnow(), language="en",
                   country="US", categories=[], places=[], importance=50)
    db.add(dead)
    db.commit()

    calls = []

    async def fake_fetch(client, url):
        calls.append(url)
        return ""                                   # the page is gone

    monkeypatch.setattr(ingest, "fetch_article_text", fake_fetch)

    assert run(ingest.backfill_content(db, spare=5)) == 0
    assert len(calls) == 1
    assert dead.content_tried_at is not None        # the attempt is recorded

    # A second tick straight after leaves it alone.
    assert run(ingest.backfill_content(db, spare=5)) == 0
    assert len(calls) == 1

    # Hours later it is worth one more go.
    dead.content_tried_at = utcnow() - timedelta(hours=ingest.BACKFILL_RETRY_HOURS + 1)
    db.commit()
    run(ingest.backfill_content(db, spare=5))
    assert len(calls) == 2


def test_the_backlog_leaves_paywalled_outlets_alone(db, monkeypatch):
    """Their article page is a stub, which would pollute the stored text."""
    src = _source(db, paywall=True)
    db.add(Article(source_id=src.id, url="http://w.test/paid", guid="paid",
                   title="Subscribers only", summary="", content="",
                   published_at=utcnow(), fetched_at=utcnow(), language="en",
                   country="US", categories=[], places=[], importance=50))
    db.commit()

    async def fake_fetch(client, url):
        raise AssertionError("a paywalled page must never be fetched")

    monkeypatch.setattr(ingest, "fetch_article_text", fake_fetch)
    assert run(ingest.backfill_content(db, spare=5)) == 0


def test_a_tick_at_its_cap_does_no_backlog_work(db):
    assert run(ingest.backfill_content(db, spare=0)) == 0


def test_discovery_probes_run_together_not_one_after_another(db, monkeypatch):
    """Eight unresponsive domains probed serially at the 8s timeout is over a
    minute of a tick during which no source is polled at all."""
    import asyncio
    import time as _time

    from backend.app import discovery

    order = []

    async def slow_probe(client, homepage):
        order.append(("start", homepage))
        await asyncio.sleep(0.15)
        order.append(("done", homepage))
        return None                                # no feed found

    monkeypatch.setattr(discovery, "_find_site_feed", slow_probe)
    publishers = {f"outlet{i}.test": (f"Outlet {i}", f"http://outlet{i}.test")
                  for i in range(4)}

    # Discovery waits for a second sighting before probing anything, so the
    # first pass records the domains and probes nothing. This test is about
    # how the probes overlap once they do run — get past the gate first.
    assert run(discovery.discover_new_sources(db, publishers)) == []
    assert order == [], "a domain was probed on its first sighting"
    db.commit()

    started = _time.perf_counter()
    added = run(discovery.discover_new_sources(db, publishers))
    took = _time.perf_counter() - started

    assert added == []
    # Four probes of 0.15s: together well under 0.3s, one after another 0.6s.
    assert took < 0.4, f"probes did not overlap ({took:.2f}s)"
    # The first few start before any has finished — that is the overlap.
    assert order[1][0] == "start"


def test_every_catalog_source_is_complete_and_unique():
    """A malformed catalog entry becomes a source that can never be polled."""
    import json

    from backend.app.catalog import SOURCES_PATH

    catalog = json.load(open(SOURCES_PATH, encoding="utf-8"))
    required = {"name", "rss_url", "homepage", "country", "region", "language",
                "scope", "categories", "tier"}
    urls = [c["rss_url"] for c in catalog]
    assert len(urls) == len(set(urls)), "duplicate feed URL in the catalog"
    for entry in catalog:
        assert required <= set(entry), f"{entry.get('name')} is missing fields"
        assert entry["rss_url"].startswith("http"), entry["name"]
        assert entry["scope"] in ("local", "national", "international"), entry["name"]
        assert len(entry["country"]) in (0, 2), entry["name"]

    # The reason the catalog was widened: a global monitor that only reads
    # English is not one, and the cross-language clustering has nothing to do.
    languages = {c["language"] for c in catalog}
    assert len(languages) >= 20, sorted(languages)
    assert {"ar", "es", "pt", "de", "fr", "ru", "zh", "hi", "ja"} <= languages
    # And most of the catalog should be outlets rather than one aggregator.
    google = sum(1 for c in catalog if "news.google.com" in c["rss_url"])
    assert google / len(catalog) < 0.1


def test_the_console_reports_what_would_justify_a_new_way_in(client, register, db):
    """Two numbers decide whether Delphi needs an intake path beyond RSS, and
    both were otherwise a matter of opinion: how many publishers it has met that
    publish no feed at all, and how many articles are queued for their text."""
    from datetime import timedelta

    from backend.app.models import Article, DiscoveredDomain, Source

    hdr = register("operator")
    src = Source(name="Wire", rss_url="http://w/feed", scope="national", tier=2)
    found = Source(name="Found", rss_url="http://f/feed", scope="national",
                   tier=3, added_by="auto-discovered")
    db.add_all([src, found])
    db.flush()
    db.add_all([DiscoveredDomain(domain="nofeed-a.example", status="no-feed"),
                DiscoveredDomain(domain="nofeed-b.example", status="no-feed"),
                DiscoveredDomain(domain="hasfeed.example", status="added")])
    now = utcnow()
    for i in range(5):
        db.add(Article(source_id=src.id, url=f"http://w/{i}", guid=str(i),
                       title=f"Report {i}", summary="", content="",
                       published_at=now - timedelta(hours=1), fetched_at=now,
                       language="en", country="US", categories=[], places=[],
                       importance=50,
                       content_tried_at=now if i < 2 else None))
    # One that already has its text is not waiting for anything.
    db.add(Article(source_id=src.id, url="http://w/done", guid="done",
                   title="Complete", summary="", content="the body",
                   published_at=now, fetched_at=now, language="en", country="US",
                   categories=[], places=[], importance=50))
    db.commit()

    s = client.get("/api/ingest/status", headers=hdr).json()

    d = s["discovery"]
    assert d["without_a_feed"] == 2
    assert d["with_a_feed"] == 1
    assert d["domains_probed"] == 3
    assert d["sources_found"] == 1
    assert set(d["feedless_examples"]) == {"nofeed-a.example", "nofeed-b.example"}

    c = s["content"]
    assert c["waiting_for_a_body"] == 5
    assert c["never_tried"] == 3
    assert c["tried_and_failed"] == 2


def test_a_paywalled_outlet_is_not_counted_as_waiting(client, register, db):
    """Its body is never fetched by design, so counting it as a backlog would
    make the number that decides this permanently and wrongly large."""
    from backend.app.models import Article, Source

    hdr = register("paywatcher")
    src = Source(name="Paid", rss_url="http://p/feed", scope="national",
                 tier=1, paywall=True)
    db.add(src)
    db.flush()
    db.add(Article(source_id=src.id, url="http://p/1", guid="1",
                   title="Subscribers only", summary="", content="",
                   published_at=utcnow(), fetched_at=utcnow(), language="en",
                   country="US", categories=[], places=[], importance=50))
    db.commit()

    assert client.get("/api/ingest/status",
                      headers=hdr).json()["content"]["waiting_for_a_body"] == 0
