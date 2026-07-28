"""Ingest core: URL safety, per-source language detection, rolling-poll
scheduling primitives (HostPacer, due-selection, intervals)."""
import asyncio

from backend.app import ingest
from backend.app.models import Source, utcnow


def test_url_scheme_allowlist():
    assert ingest._is_http_url("https://x/y") and ingest._is_http_url("HTTP://x")
    assert not ingest._is_http_url("javascript:alert(1)")
    assert not ingest._is_http_url("data:text/html,x")
    assert not ingest._is_http_url("ftp://h/x")


def test_process_entries_drops_non_http_and_detects_language(db):
    src = Source(name="Agg", rss_url="http://agg.test/rss", scope="international",
                 tier=2, language="en", added_by="topic-tracker")
    db.add(src); db.flush()
    entries = [
        {"link": "javascript:alert(1)", "title": "XSS one", "id": "1"},
        {"link": "https://ok.test/jp", "title": "東京で大地震、津波警報が発令されました", "id": "2"},
        {"link": "https://ok.test/en", "title": "Breaking earthquake hits the capital", "id": "3"},
    ]
    new = ingest.process_entries(db, src, entries, [], set())
    urls = {a.url for a in new}
    assert urls == {"https://ok.test/jp", "https://ok.test/en"}   # javascript: dropped
    by = {a.url: a for a in new}
    assert by["https://ok.test/jp"].language == "ja"              # detected, not source "en"
    assert by["https://ok.test/en"].language == "en"


def test_effective_interval_and_backoff():
    wire = Source(name="w", rss_url="http://w/x", added_by="catalog")
    city = Source(name="c", rss_url="http://news.google.com/rss/search?q=x",
                  added_by="city-catalog", idle_polls=0)
    assert ingest.effective_interval(wire) == ingest.BASE_INTERVAL
    assert ingest.effective_interval(city) == ingest.CITY_INTERVAL
    city.idle_polls = 3
    assert ingest.effective_interval(city) == ingest.CITY_INTERVAL * 8
    city.idle_polls = 99
    assert ingest.effective_interval(city) == ingest.CITY_INTERVAL * (2 ** ingest.CITY_IDLE_BACKOFF_MAX)


def test_host_gap_rules():
    assert ingest.host_gap("news.google.com", 400) == ingest.GOOGLE_GAP
    assert ingest.host_gap("reddit.com", 5) == ingest.SHARED_HOST_GAP
    assert ingest.host_gap("feeds.bbci.co.uk", 1) == 0.0


def test_host_pacer_drips_per_host():
    async def run():
        p = ingest.HostPacer()
        a = [await p.reserve("h1", 0.5) for _ in range(4)]
        b = await p.reserve("h2", 0.5)
        return a, b
    delays, other = asyncio.run(run())
    assert all(abs(delays[i] - i * 0.5) < 0.05 for i in range(4))
    assert other < 0.05
    assert asyncio.run(ingest.HostPacer().reserve("x", 0.0)) == 0.0


def test_due_selection_orders_wires_before_city(db):
    now = utcnow()
    db.add_all([
        Source(name="wire", rss_url="http://w/x", added_by="catalog"),
        Source(name="city", rss_url="http://news.google.com/rss/search?q=t",
               added_by="city-catalog"),
    ])
    db.commit()
    from sqlalchemy import select
    enabled = db.scalars(select(Source)).all()
    due = ingest.due_sources(enabled, now)
    assert len(due) == 2 and not ingest._is_city(due[0]) and ingest._is_city(due[1])
