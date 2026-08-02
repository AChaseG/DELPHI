"""Is a source actually a source, or just a row?

A catalog that grows by itself is easy to mistake for a catalog that works —
a thousand outlets reads like breadth. But a feed that has been polled and has
never once produced an article contributes nothing, and until now there was
nowhere to see that: the health dot said the feed answered, not that answering
ever amounted to news.
"""
from datetime import timedelta

from backend.app.models import Article, Source, utcnow


def _source(db, name, *, polled=True, articles=0, enabled=True, status="ok"):
    s = Source(name=name, rss_url=f"http://{name}.test/rss", scope="national",
               tier=2, enabled=enabled, last_status=status,
               last_fetched_at=utcnow() if polled else None)
    db.add(s)
    db.flush()
    for i in range(articles):
        db.add(Article(source_id=s.id, title=f"{name} {i}",
                       url=f"http://{name}.test/{i}", summary="", content="",
                       published_at=utcnow() - timedelta(minutes=i),
                       fetched_at=utcnow(), importance=40))
    db.commit()
    return s


def test_a_source_says_whether_it_has_ever_produced(client, register, db):
    _source(db, "carrying", articles=3)
    _source(db, "silent")
    headers = register("reader")

    by_name = {s["name"]: s for s in client.get("/api/sources", headers=headers).json()}

    assert by_name["carrying"]["has_produced"] is True
    assert by_name["silent"]["has_produced"] is False


def test_health_counts_the_sources_that_produce_nothing(client, register, db):
    _source(db, "carrying", articles=2)
    _source(db, "alsocarrying", articles=1)
    _source(db, "silentone")
    _source(db, "silenttwo")
    _source(db, "brandnew", polled=False)      # not silent — never asked yet
    headers = register("reader")

    health = client.get("/api/ingest/status", headers=headers).json()["sources"]

    assert health["total"] == 5
    assert health["producing"] == 2
    assert health["silent"] == 2, "a source polled with nothing to show is not counted"
    assert health["never_polled"] == 1


def test_a_retired_source_is_counted_as_retired(client, register, db):
    _source(db, "gone", enabled=False,
            status="retired: no answer in 5 tries (error: 404 Not Found).")
    _source(db, "off", enabled=False, status="ok")   # disabled by hand, not retired
    headers = register("reader")

    health = client.get("/api/ingest/status", headers=headers).json()["sources"]

    assert health["retired"] == 1
    assert health["enabled"] == 0
    assert health["silent"] == 0, \
        "a retired source is reported as retired, not as one still to deal with"


def test_health_publishes_the_limit_it_removes_at(client, register):
    """So the number in the console is the number in the code, not a guess."""
    from backend.app import ingest
    headers = register("reader")

    health = client.get("/api/ingest/status", headers=headers).json()["sources"]

    assert health["remove_after"] == ingest.REMOVE_AFTER


def test_the_listing_does_not_ask_once_per_source(client, register, db):
    """Over a thousand sources, one query per source to answer "has it ever
    produced" is a thousand queries on every panel open."""
    for i in range(25):
        _source(db, f"outlet{i}", articles=1 if i % 2 else 0)
    headers = register("reader")

    from sqlalchemy import event
    from backend.app.database import engine
    seen = []

    def record(conn, cursor, statement, *rest):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        rows = client.get("/api/sources", headers=headers).json()
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert len(rows) == 25
    article_queries = [q for q in seen if "from articles" in " ".join(q.lower().split())]
    assert len(article_queries) == 1, \
        f"{len(article_queries)} article queries for 25 sources:\n" + "\n".join(article_queries[:3])
