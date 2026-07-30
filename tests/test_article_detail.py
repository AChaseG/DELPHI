"""The focused view a headline opens: one article, in full.

A column truncates — 400 characters of summary, a few tags — because a column
is narrow. This endpoint is what a reader actually reads from, so it carries
the publisher's summary whole, an extract of the body, when and where the story
was published, and who else is covering the same event.
"""
from datetime import timedelta

from backend.app.main import ARTICLE_EXCERPT_CHARS, ARTICLE_SIBLINGS
from backend.app.models import Article, Event, Source, utcnow


def _source(db, name="Wire", country="us", paywall=False):
    src = Source(name=name, rss_url=f"http://example.com/{name}", country=country,
                 language="en", scope="international", platform="news", paywall=paywall)
    db.add(src)
    db.flush()
    return src


def _article(db, src, **kw):
    fields = dict(
        source_id=src.id, title="Quake hits Tokyo", summary="A summary.",
        content="", url=f"http://example.com/{kw.get('slug', 'a')}",
        language="en", country="jp", categories=["disaster"],
        places=[{"name": "Tokyo", "country": "jp", "lat": 35.7, "lon": 139.7}],
        importance=71, published_at=utcnow() - timedelta(hours=2), fetched_at=utcnow(),
    )
    fields.update({k: v for k, v in kw.items() if k != "slug"})
    a = Article(**fields)
    db.add(a)
    db.commit()
    return a


def test_unknown_article_is_a_clean_404(client, register, db):
    assert client.get("/api/articles/999999", headers=register("annie")).status_code == 404


def test_it_needs_an_account(client, db):
    src = _source(db)
    a = _article(db, src)
    assert client.get(f"/api/articles/{a.id}").status_code == 401


def test_it_carries_what_the_view_shows(client, register, db):
    src = _source(db)
    a = _article(db, src)
    got = client.get(f"/api/articles/{a.id}", headers=register("reader")).json()
    assert got["title"] == "Quake hits Tokyo"
    assert got["published_at"] and got["fetched_at"]
    assert got["source"]["name"] == "Wire"
    assert got["source"]["scope"] == "international"
    assert got["source"]["platform"] == "news"      # only the detail view sends these
    assert got["source"]["language"] == "en"
    assert got["importance"] == 71
    assert [p["name"] for p in got["places"]] == ["Tokyo"]
    assert got["categories"] == ["disaster"]
    assert got["url"] == a.url                       # so the reader can choose to leave


def test_the_summary_is_not_truncated_the_way_a_card_truncates_it(client, register, db):
    src = _source(db)
    long_summary = "S" * 900
    a = _article(db, src, summary=long_summary)
    hdr = register("reader")

    card = client.post("/api/articles/search?limit=10", headers=hdr,
                       json={"criteria": {}}).json()[0]
    assert len(card["summary"]) == 400            # the column's truncation

    full = client.get(f"/api/articles/{a.id}", headers=hdr).json()
    assert full["summary"] == long_summary


def test_the_body_extract_is_bounded_and_says_when_it_is_cut(client, register, db):
    src = _source(db)
    a = _article(db, src, content="B" * (ARTICLE_EXCERPT_CHARS + 500))
    got = client.get(f"/api/articles/{a.id}", headers=register("reader")).json()
    assert len(got["excerpt"]) == ARTICLE_EXCERPT_CHARS
    assert got["excerpt_truncated"] is True


def test_a_short_body_is_not_marked_as_cut(client, register, db):
    src = _source(db)
    a = _article(db, src, content="Just a short body.")
    got = client.get(f"/api/articles/{a.id}", headers=register("reader")).json()
    assert got["excerpt"] == "Just a short body."
    assert got["excerpt_truncated"] is False


def test_an_article_with_no_body_still_opens(client, register, db):
    src = _source(db)
    a = _article(db, src, content="")
    got = client.get(f"/api/articles/{a.id}", headers=register("reader")).json()
    assert got["excerpt"] == "" and got["excerpt_truncated"] is False


def test_a_paywalled_article_offers_a_way_to_read_it(client, register, db):
    src = _source(db, name="Walled", paywall=True)
    a = _article(db, src)
    got = client.get(f"/api/articles/{a.id}", headers=register("reader")).json()
    assert got["paywall"] is True
    assert got["archive_url"].startswith("https://archive.ph/newest/")


def test_a_stray_article_reports_no_event_and_no_siblings(client, register, db):
    src = _source(db)
    a = _article(db, src)
    got = client.get(f"/api/articles/{a.id}", headers=register("reader")).json()
    assert got["event"] is None
    assert got["also_covered_by"] == []


def test_a_clustered_article_lists_who_else_has_the_story(client, register, db):
    ev = Event(title="Quake", article_count=3, first_seen=utcnow(), updated_at=utcnow())
    db.add(ev)
    db.flush()
    one = _source(db, name="Wire A")
    two = _source(db, name="Wire B")
    mine = _article(db, one, slug="mine", event_id=ev.id)
    _article(db, two, slug="theirs", title="Tokyo shaken", event_id=ev.id)

    got = client.get(f"/api/articles/{mine.id}", headers=register("reader")).json()
    assert got["event"]["id"] == ev.id
    assert got["event"]["article_count"] == 3
    assert got["event"]["source_count"] == 2
    assert [o["title"] for o in got["also_covered_by"]] == ["Tokyo shaken"]
    assert got["also_covered_by"][0]["source"]["name"] == "Wire B"
    # the article itself is never listed as covering itself
    assert all(o["id"] != mine.id for o in got["also_covered_by"])


def test_the_sibling_list_is_bounded(client, register, db):
    ev = Event(title="Big", article_count=40, first_seen=utcnow(), updated_at=utcnow())
    db.add(ev)
    db.flush()
    src = _source(db)
    mine = _article(db, src, slug="mine", event_id=ev.id)
    for i in range(ARTICLE_SIBLINGS + 5):
        _article(db, src, slug=f"s{i}", event_id=ev.id)
    got = client.get(f"/api/articles/{mine.id}", headers=register("reader")).json()
    assert len(got["also_covered_by"]) == ARTICLE_SIBLINGS


def test_it_says_whether_this_reader_has_seen_the_event(client, register, db):
    ev = Event(title="Quake", article_count=1, first_seen=utcnow(), updated_at=utcnow())
    db.add(ev)
    db.flush()
    src = _source(db)
    a = _article(db, src, event_id=ev.id)
    alice, bob = register("alice2"), register("bob2")

    assert client.get(f"/api/articles/{a.id}", headers=alice).json()["viewed"] is False
    client.post(f"/api/events/{ev.id}/viewed", headers=alice)
    assert client.get(f"/api/articles/{a.id}", headers=alice).json()["viewed"] is True
    # and one reader's history is not another's
    assert client.get(f"/api/articles/{a.id}", headers=bob).json()["viewed"] is False
