"""The focused view a headline opens: one story.

A story with one report and a story forty outlets are carrying are the same
thing at different distances, so one endpoint serves both — the report the
reader picked, and the event around it when there is one. A column truncates
because a column is narrow; this is what a reader actually reads from, so it
carries the publisher's summary whole, an extract of the body, when and where
it was published, and every other outlet on the same story.
"""
from datetime import timedelta

from backend.app.main import ARTICLE_EXCERPT_CHARS, STORY_TIMELINE_MAX
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


def _event(db, **kw):
    fields = dict(title="Quake", article_count=1, first_seen=utcnow(), updated_at=utcnow())
    fields.update(kw)
    ev = Event(**fields)
    db.add(ev)
    db.commit()   # committed, not just flushed: the API uses its own connection
    return ev


def test_unknown_story_is_a_clean_404(client, register, db):
    assert client.get("/api/story/999999", headers=register("annie")).status_code == 404


def test_unknown_event_is_a_clean_404(client, register, db):
    assert client.get("/api/story/by-event/999999",
                      headers=register("annie")).status_code == 404


def test_it_needs_an_account(client, db):
    a = _article(db, _source(db))
    assert client.get(f"/api/story/{a.id}").status_code == 401


def test_it_carries_what_the_view_shows(client, register, db):
    a = _article(db, _source(db))
    got = client.get(f"/api/story/{a.id}", headers=register("reader")).json()["article"]
    assert got["title"] == "Quake hits Tokyo"
    assert got["published_at"] and got["fetched_at"]
    assert got["source"]["name"] == "Wire"
    assert got["source"]["scope"] == "international"
    assert got["source"]["platform"] == "news"      # only the focused view sends these
    assert got["source"]["language"] == "en"
    assert got["importance"] == 71
    assert [p["name"] for p in got["places"]] == ["Tokyo"]
    assert got["categories"] == ["disaster"]
    assert got["url"] == a.url                       # so the reader can choose to leave


def test_the_summary_is_not_truncated_the_way_a_card_truncates_it(client, register, db):
    long_summary = "S" * 900
    a = _article(db, _source(db), summary=long_summary)
    hdr = register("reader")

    card = client.post("/api/articles/search?limit=10", headers=hdr,
                       json={"criteria": {}}).json()[0]
    assert len(card["summary"]) == 400            # the column's truncation

    full = client.get(f"/api/story/{a.id}", headers=hdr).json()["article"]
    assert full["summary"] == long_summary


def test_the_body_extract_is_bounded_and_says_when_it_is_cut(client, register, db):
    a = _article(db, _source(db), content="B" * (ARTICLE_EXCERPT_CHARS + 500))
    got = client.get(f"/api/story/{a.id}", headers=register("reader")).json()["article"]
    assert len(got["excerpt"]) == ARTICLE_EXCERPT_CHARS
    assert got["excerpt_truncated"] is True


def test_a_short_body_is_not_marked_as_cut(client, register, db):
    a = _article(db, _source(db), content="Just a short body.")
    got = client.get(f"/api/story/{a.id}", headers=register("reader")).json()["article"]
    assert got["excerpt"] == "Just a short body."
    assert got["excerpt_truncated"] is False


def test_an_article_with_no_body_still_opens(client, register, db):
    a = _article(db, _source(db), content="")
    got = client.get(f"/api/story/{a.id}", headers=register("reader")).json()["article"]
    assert got["excerpt"] == "" and got["excerpt_truncated"] is False


def test_a_paywalled_story_offers_a_way_to_read_it(client, register, db):
    a = _article(db, _source(db, name="Walled", paywall=True))
    got = client.get(f"/api/story/{a.id}", headers=register("reader")).json()["article"]
    assert got["paywall"] is True
    assert got["archive_url"].startswith("https://archive.ph/newest/")


def test_a_story_nobody_else_has_is_the_same_shape(client, register, db):
    """A single report is a story too — same payload, with the event parts empty."""
    a = _article(db, _source(db))
    got = client.get(f"/api/story/{a.id}", headers=register("reader")).json()
    assert got["article"]["id"] == a.id
    assert got["event"] is None
    assert got["articles"] == [] and got["sources"] == [] and got["related"] == []


def test_a_story_several_outlets_have_carries_all_of_them(client, register, db):
    ev = _event(db, article_count=3)
    one, two = _source(db, name="Wire A"), _source(db, name="Wire B")
    mine = _article(db, one, slug="mine", event_id=ev.id)
    _article(db, two, slug="theirs", title="Tokyo shaken", event_id=ev.id,
             published_at=utcnow() - timedelta(hours=5))

    got = client.get(f"/api/story/{mine.id}", headers=register("reader")).json()
    assert got["article"]["id"] == mine.id            # opened at the report picked
    assert got["event"]["id"] == ev.id
    # counted from the timeline, not the stored total, so the view can never
    # claim fewer reports than it shows outlets
    assert got["event"]["article_count"] == 2
    assert got["event"]["source_count"] == 2
    # the timeline is the whole story, newest first, the one being read included
    assert [x["id"] for x in got["articles"]][0] == mine.id
    assert len(got["articles"]) == 2
    assert sorted(s["name"] for s in got["sources"]) == ["Wire A", "Wire B"]


def test_opening_by_event_lands_on_the_latest_report(client, register, db):
    ev = _event(db, article_count=2)
    src = _source(db)
    _article(db, src, slug="old", title="First word",
             event_id=ev.id, published_at=utcnow() - timedelta(hours=9))
    newest = _article(db, src, slug="new", title="Latest word",
                      event_id=ev.id, published_at=utcnow())

    got = client.get(f"/api/story/by-event/{ev.id}", headers=register("reader")).json()
    assert got["article"]["id"] == newest.id
    assert got["event"]["id"] == ev.id
    assert len(got["articles"]) == 2


def test_an_event_whose_reports_are_all_gone_is_a_404(client, register, db):
    ev = _event(db)
    assert client.get(f"/api/story/by-event/{ev.id}",
                      headers=register("annie")).status_code == 404


def test_the_timeline_is_bounded(client, register, db):
    ev = _event(db, article_count=STORY_TIMELINE_MAX + 20)
    src = _source(db)
    mine = _article(db, src, slug="mine", event_id=ev.id)
    for i in range(STORY_TIMELINE_MAX + 10):
        _article(db, src, slug=f"s{i}", event_id=ev.id)
    got = client.get(f"/api/story/{mine.id}", headers=register("reader")).json()
    assert len(got["articles"]) == STORY_TIMELINE_MAX


def test_it_says_whether_this_reader_has_seen_the_story(client, register, db):
    ev = _event(db)
    a = _article(db, _source(db), event_id=ev.id)
    alice, bob = register("alice2"), register("bob2")

    def viewed(hdr):
        return client.get(f"/api/story/{a.id}", headers=hdr).json()["article"]["viewed"]

    assert viewed(alice) is False
    client.post(f"/api/events/{ev.id}/viewed", headers=alice)
    assert viewed(alice) is True
    assert viewed(bob) is False       # and one reader's history is not another's


def test_a_truncated_timeline_still_reports_the_real_total(client, register, db):
    """Past the timeline's bound the stored count is the honest one."""
    ev = _event(db, article_count=STORY_TIMELINE_MAX + 40)
    src = _source(db)
    mine = _article(db, src, slug="mine", event_id=ev.id)
    for i in range(STORY_TIMELINE_MAX + 10):
        _article(db, src, slug=f"t{i}", event_id=ev.id)
    got = client.get(f"/api/story/{mine.id}", headers=register("reader")).json()
    assert len(got["articles"]) == STORY_TIMELINE_MAX
    assert got["event"]["article_count"] == STORY_TIMELINE_MAX + 40
