"""Which of a column's words are in this story, and where they are.

The report that produced this: a WNBA story in a feed whose query was six
phrases about data centres, none of them anywhere the reader could see. They
were in the page's own furniture — a section link and a promo rail — stored as
part of the article, and nothing on screen said so.

A reader who cannot find their terms in a result has to choose between "the
search is broken" and "the words are somewhere I am not looking", and those are
fixed in completely different places. This is what tells them which.
"""
import pytest

from backend.app.matching import explain_text_match
from backend.app.models import Article, Source, utcnow

QUERY = ('("data center" OR "server farm" OR "cryptocurrency farm" OR '
         '"cryptocurrency mining" OR "crypto mine" OR "AI industry")')

HEADLINE = ("WNBA followers enraged after three head coaches' comments on "
            "protecting women's sports in two days")
SUMMARY = "Three coaches spoke about eligibility within two days of each other."


@pytest.fixture
def wire(db):
    s = Source(name="Outlet", rss_url="http://outlet.test/feed", scope="national")
    db.add(s)
    db.commit()
    return s


def _article(db, wire, title=HEADLINE, summary=SUMMARY, content=""):
    a = Article(source_id=wire.id, title=title, summary=summary, content=content,
                url=f"http://outlet.test/{abs(hash((title, content))) % 10**8}",
                published_at=utcnow(), fetched_at=utcnow(), importance=40)
    db.add(a)
    db.commit()
    return a


def test_it_names_the_term_and_where_it_is(db, wire):
    """The reported case, reconstructed."""
    art = _article(db, wire, content=(
        "The league declined to comment further. "
        "Fox News AI Newsletter: get the latest on the AI industry"))

    hits = explain_text_match({"queries": [QUERY]}, art)

    assert [h["term"] for h in hits] == ["AI industry"]
    assert hits[0]["where"] == "body", "the reader would have found it themselves"
    assert "AI industry" in hits[0]["snippet"]


def test_a_term_in_the_headline_is_reported_as_such(db, wire):
    """The search working. Same shape of answer, different conclusion."""
    art = _article(db, wire, title="New data center approved outside Columbus")

    hits = explain_text_match({"queries": [QUERY]}, art)

    assert hits[0]["where"] == "headline"


def test_the_earliest_field_a_term_appears_in_is_the_one_reported(db, wire):
    art = _article(db, wire, title="A data center opens",
                   summary="The data center is large.",
                   content="Another mention of the data center.")
    assert explain_text_match({"queries": [QUERY]}, art)[0]["where"] == "headline"


def test_an_article_with_none_of_the_words_says_so(db, wire):
    """The third case the original report did not cover.

    "The search is broken" and "the words are somewhere I am not looking" were
    the two this file was written for. There is a third — the words are nowhere
    at all, and never had to be, because the query admitted the article on an
    exclusion or the feed's non-text filters. Returning nothing left the reader
    with the mystery this whole feature exists to end."""
    art = _article(db, wire, content="The league declined to comment.")
    got = explain_text_match({"queries": [QUERY]}, art)
    assert len(got) == 1
    assert got[0]["where"] == "elsewhere"
    assert "None of the query's words" in got[0]["snippet"]


def test_a_query_that_matches_everything_says_that_instead(db, wire):
    """A query with an alternative that is only an exclusion matches every
    article that is not excluded, so the reader's own terms play no part. That
    is a different answer and a fixable one, so it gets its own wording."""
    art = _article(db, wire, content="The league declined to comment.")
    got = explain_text_match({"queries": ['"data center" OR NOT basketball']}, art)
    assert got[0]["where"] == "query"
    assert "only an exclusion" in got[0]["snippet"]


def test_plain_keywords_are_explained_too(db, wire):
    """Not every feed uses a boolean string."""
    art = _article(db, wire, content="Talk of a server farm in the county.")
    hits = explain_text_match({"keywords": ["server farm"]}, art)
    assert hits[0]["term"] == "server farm" and hits[0]["where"] == "body"


def test_every_matching_term_is_listed_not_just_the_first(db, wire):
    art = _article(db, wire, content="A data center and a server farm nearby.")
    assert {h["term"] for h in explain_text_match({"queries": [QUERY]}, art)} \
        == {"data center", "server farm"}


def test_a_broken_query_explains_nothing_rather_than_raising(db, wire):
    art = _article(db, wire, content="A data center opened.")
    assert explain_text_match({"queries": ["(data center"]}, art) == []


def test_criteria_with_no_words_at_all_are_fine(db, wire):
    art = _article(db, wire)
    assert explain_text_match({}, art) == []
    assert explain_text_match({"countries": ["US"]}, art) == []


# ---------- through the endpoint ----------

def test_the_endpoint_answers_for_one_of_the_readers_feeds(client, register, db, wire):
    headers = register("reader")
    feed_id = client.post("/api/feeds", headers=headers, json={
        "name": "Data centres", "criteria": {"queries": [QUERY]}}).json()["id"]
    art = _article(db, wire, content="…get the latest on the AI industry")

    r = client.get(f"/api/feeds/{feed_id}/why/{art.id}", headers=headers)

    assert r.status_code == 200
    body = r.json()
    assert body["hits"][0]["term"] == "AI industry"
    assert body["body_only"] is True, "this is the case worth flagging"


def test_body_only_is_false_when_the_headline_carries_it(client, register, db, wire):
    headers = register("reader")
    feed_id = client.post("/api/feeds", headers=headers, json={
        "name": "Data centres", "criteria": {"queries": [QUERY]}}).json()["id"]
    art = _article(db, wire, title="New data center approved")

    body = client.get(f"/api/feeds/{feed_id}/why/{art.id}", headers=headers).json()

    assert body["body_only"] is False


def test_a_missing_article_is_a_404(client, register):
    headers = register("reader")
    feed_id = client.post("/api/feeds", headers=headers, json={
        "name": "f", "criteria": {"keywords": ["x"]}}).json()["id"]
    assert client.get(f"/api/feeds/{feed_id}/why/999999",
                      headers=headers).status_code == 404


def test_it_needs_a_session(client, db, wire):
    art = _article(db, wire)
    assert client.get(f"/api/feeds/1/why/{art.id}").status_code == 401


def test_somebody_elses_feed_is_not_readable(client, register, db, wire):
    mine = register("alice")
    feed_id = client.post("/api/feeds", headers=mine, json={
        "name": "f", "criteria": {"keywords": ["x"]}}).json()["id"]
    art = _article(db, wire)

    theirs = register("mallory")
    assert client.get(f"/api/feeds/{feed_id}/why/{art.id}",
                      headers=theirs).status_code == 404
