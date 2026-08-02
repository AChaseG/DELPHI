"""The source catalog is served from a rendered cache.

It is half a megabyte across more than a thousand outlets, and it was rebuilt
per caller: ORM rows, a dict each, FastAPI's encoder walking every value again,
then JSON. About 80ms of Python that never releases the interpreter — fine
once, and ruinous concurrently. Measured on a 1,200-source catalog: ten callers
at once took 2.9s each and forty never finished. A browser asks for this
whenever the Sources panel or the feed editor opens, so one reader with a few
tabs was enough to make every other request queue behind it.

These check the two things a cache has to get right — that it still answers
with the truth, and that an edit is never waited for.
"""
import time

import pytest

from backend.app import main
from backend.app.models import Article, Source, utcnow


@pytest.fixture(autouse=True)
def _clear_cache():
    main._invalidate_sources_cache()
    yield
    main._invalidate_sources_cache()


def _source(db, name, *, articles=0, **kw):
    s = Source(name=name, rss_url=f"http://{name}.test/rss", scope="national",
               tier=2, last_fetched_at=utcnow(), last_status="ok", **kw)
    db.add(s)
    db.flush()
    for i in range(articles):
        db.add(Article(source_id=s.id, title=f"{name} {i}", url=f"http://{name}.test/{i}",
                       summary="", content="", published_at=utcnow(),
                       fetched_at=utcnow(), importance=40))
    db.commit()
    return s


def test_it_lists_every_source_with_the_fields_the_panel_needs(client, register, db):
    _source(db, "outlet", articles=2, country="GB", categories=["world"])
    headers = register("reader")

    rows = client.get("/api/sources", headers=headers).json()

    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "outlet"
    assert row["country"] == "GB"
    assert row["categories"] == ["world"]      # a JSON column, not a string
    assert row["paywall"] is False             # a real bool, not 0
    assert row["enabled"] is True
    assert row["has_produced"] is True
    assert row["last_fetched_at"].endswith("Z")


def test_slim_is_only_what_names_a_source(client, register, db):
    _source(db, "outlet")
    headers = register("reader")

    rows = client.get("/api/sources?slim=1", headers=headers).json()

    assert sorted(rows[0]) == ["id", "name"]


def test_the_full_and_slim_lists_do_not_share_a_cache_entry(client, register, db):
    """They are different shapes at the same path; handing one out for the
    other would empty the Sources panel or bloat the startup request."""
    _source(db, "outlet")
    headers = register("reader")

    slim = client.get("/api/sources?slim=1", headers=headers).json()
    full = client.get("/api/sources", headers=headers).json()

    assert sorted(slim[0]) == ["id", "name"]
    assert "rss_url" in full[0]


def test_adding_a_source_shows_up_at_once(client, register, db):
    """The cache must never make somebody's own edit look like it failed."""
    headers = register("reader")
    client.get("/api/sources", headers=headers)          # fill the cache

    client.post("/api/sources", headers=headers, json={
        "name": "Brand new", "rss_url": "http://brandnew.test/rss",
        "homepage": "", "country": "", "region": "", "language": "en",
        "scope": "national", "categories": [], "tier": 2})

    names = [s["name"] for s in client.get("/api/sources", headers=headers).json()]
    assert "Brand new" in names


def test_editing_a_source_shows_up_at_once(client, register, db):
    s = _source(db, "before")
    headers = register("reader")
    client.get("/api/sources", headers=headers)

    client.patch(f"/api/sources/{s.id}", headers=headers, json={"name": "after"})

    names = [x["name"] for x in client.get("/api/sources", headers=headers).json()]
    assert names == ["after"]


def test_disabling_a_source_shows_up_at_once(client, register, db):
    """⏸ is the control everyone has; it has to look like it did something."""
    s = _source(db, "noisy")
    headers = register("reader")
    client.get("/api/sources", headers=headers)

    client.patch(f"/api/sources/{s.id}", headers=headers, json={"enabled": False})

    assert client.get("/api/sources", headers=headers).json()[0]["enabled"] is False


def test_a_change_nobody_made_is_bounded_by_the_ttl(client, register, db, monkeypatch):
    """Poll status changes under the cache without invalidating it — sources
    are polled minutes apart, so seconds of lag costs nothing. What must not
    happen is lag without end."""
    s = _source(db, "outlet")
    headers = register("reader")
    assert client.get("/api/sources", headers=headers).json()[0]["last_status"] == "ok"

    s.last_status = "error: 404 Not Found"      # as a poll would write it
    db.commit()
    assert client.get("/api/sources", headers=headers).json()[0]["last_status"] == "ok", \
        "the cache is not being used at all"

    monkeypatch.setattr(main, "SOURCES_CACHE_TTL", 0.0)
    assert client.get("/api/sources", headers=headers).json()[0]["last_status"] \
        == "error: 404 Not Found", "the cache never expires"


def test_the_catalog_is_built_once_for_repeated_callers(client, register, db):
    """The whole point: the second reader pays for none of it."""
    for i in range(30):
        _source(db, f"outlet{i}")
    headers = register("reader")

    from sqlalchemy import event
    from backend.app.database import engine
    seen = []

    def record(conn, cursor, statement, *rest):
        seen.append(statement)

    client.get("/api/sources", headers=headers)          # fill it
    event.listen(engine, "before_cursor_execute", record)
    try:
        for _ in range(5):
            rows = client.get("/api/sources", headers=headers).json()
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert len(rows) == 30
    from_sources = [q for q in seen if "from sources" in " ".join(q.lower().split())]
    assert not from_sources, \
        f"{len(from_sources)} catalog queries for 5 cached reads"


def test_it_is_still_json_the_browser_can_read(client, register, db):
    _source(db, "outlet")
    headers = register("reader")

    resp = client.get("/api/sources", headers=headers)

    assert resp.headers["content-type"].startswith("application/json")
    assert isinstance(resp.json(), list)


def test_it_needs_a_session(client):
    assert client.get("/api/sources").status_code == 401


def test_an_empty_catalog_is_an_empty_list(client, register, db):
    headers = register("reader")
    assert client.get("/api/sources", headers=headers).json() == []
