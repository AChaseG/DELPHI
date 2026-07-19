"""Paywalled sources: headline-only ingest and archive.ph links."""
import asyncio

from backend.app import ingest
from backend.app.main import _article_json
from backend.app.models import Article, Source, utcnow


def test_article_json_archive_link_only_for_paywalled(db):
    paywalled = Source(name="Ledger", rss_url="http://p/rss", scope="international",
                       tier=1, added_by="user", paywall=True)
    free = Source(name="Wire", rss_url="http://f/rss", scope="international",
                  tier=1, added_by="user", paywall=False)
    db.add_all([paywalled, free]); db.flush()
    pa = Article(source_id=paywalled.id, url="https://p/story", title="Locked",
                 summary="s", published_at=utcnow(), importance=50)
    fa = Article(source_id=free.id, url="https://f/story", title="Open",
                 summary="s", published_at=utcnow(), importance=50)
    db.add_all([pa, fa]); db.commit()

    pj, fj = _article_json(pa), _article_json(fa)
    assert pj["paywall"] is True
    assert pj["archive_url"] == "https://archive.ph/newest/https://p/story"
    assert fj["paywall"] is False and fj["archive_url"] is None


def test_content_fetch_skips_paywalled(db, monkeypatch):
    paywalled = Source(name="Ledger", rss_url="http://p/rss", tier=1,
                       added_by="user", paywall=True)
    free = Source(name="Wire", rss_url="http://f/rss", tier=1, added_by="user")
    db.add_all([paywalled, free]); db.flush()
    pa = Article(source_id=paywalled.id, url="https://p/a", title="Locked",
                 summary="s", published_at=utcnow(), importance=50)
    fa = Article(source_id=free.id, url="https://f/a", title="Open",
                 summary="s", published_at=utcnow(), importance=50)
    db.add_all([pa, fa]); db.commit()

    fetched = []
    async def fake(client, url):
        fetched.append(url)
        return "FULL BODY " * 20
    monkeypatch.setattr(ingest, "fetch_article_text", fake)
    monkeypatch.setattr(ingest, "CONTENT_FETCH", True)

    n = asyncio.run(ingest.enrich_with_content(db, [pa, fa]))
    assert fetched == ["https://f/a"]      # only the free article's body fetched
    assert n == 1 and not pa.content and fa.content
