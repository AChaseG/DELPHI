"""Watching a place, and whether anything ever turns up.

A favourite location produced an empty column for most places anyone would
choose, and not because of a glitch. Two things had to be true for an article
to match, and for most locations neither could be:

*It had to name one of the 577 cities in the bundled gazetteer.* That is the
only way an article is given a position, so a circle around anywhere else could
not contain anything — permanently, not rarely. Meanwhile the picker offers any
address OpenStreetMap knows, which is mostly places that could never work.

*Something had to already be gathering news about it.* Saving a location made a
filter over the existing catalog and no source of its own, so a place with no
local outlet already in the catalog had nothing to filter.

So a watched place now carries the name it was picked under — matched in
headlines, and asked of a news search so coverage actually arrives.
"""
import itertools
from datetime import timedelta

import pytest

from backend.app.matching import query_articles
from backend.app.models import Article, FavoriteLocation, Source, utcnow

SEQ = itertools.count(1)

# Somewhere real, and deliberately not one of the gazetteer's 577 cities.
READING = {"type": "Circle", "center": [51.4543, -0.9781], "radius_km": 25,
           "name": "Reading", "country": "GB"}


@pytest.fixture
def wire(db):
    s = Source(name="wire", rss_url="http://wire.example/feed")
    db.add(s)
    db.commit()
    return s


def _story(db, wire, title, *, country="", places=None, minutes_ago=1):
    a = Article(source_id=wire.id, title=title, url=f"http://wire.example/{next(SEQ)}",
                summary=title, content="",
                published_at=utcnow() - timedelta(minutes=minutes_ago),
                fetched_at=utcnow(), importance=30, country=country)
    a.places = places or []
    db.add(a)
    db.commit()
    return a


# ---------- the case that could never work ----------

def test_a_place_the_gazetteer_does_not_know_now_matches(db, wire):
    """Reading is not one of the 577, so this returned nothing at all before.

    No coordinates are involved: the article has no tagged places, because
    geotagging has never heard of Reading either.
    """
    _story(db, wire, "Council in Reading approves housing plan", country="GB")

    got = query_articles(db, {"geos": [READING]}, limit=40)

    assert len(got) == 1


def test_a_map_click_with_no_name_still_works_geographically(db, wire):
    """A point with no name is a real choice, and must keep what it had."""
    athens_circle = {"type": "Circle", "center": [37.9838, 23.7275],
                     "radius_km": 50, "name": "", "country": ""}
    _story(db, wire, "Protest in Athens over pensions", country="GR",
           places=[{"name": "Athens", "lat": 37.9838, "lon": 23.7275, "country": "GR"}])

    got = query_articles(db, {"geos": [athens_circle]}, limit=40)

    assert len(got) == 1


def test_coordinates_still_match_when_the_name_is_absent_from_the_headline(db, wire):
    """The geographic route has to survive; the name is an addition to it."""
    wide = {"type": "Circle", "center": [37.9838, 23.7275], "radius_km": 60,
            "name": "Some Other Label", "country": "GR"}
    _story(db, wire, "Ferries halted after weather warning", country="GR",
           places=[{"name": "Athens", "lat": 37.9838, "lon": 23.7275, "country": "GR"}])

    assert len(query_articles(db, {"geos": [wide]}, limit=40)) == 1


# ---------- the name must not match everything ----------

def test_an_unrelated_story_does_not_match(db, wire):
    _story(db, wire, "Markets steady as investors weigh policy", country="GB")
    assert query_articles(db, {"geos": [READING]}, limit=40) == []


def test_the_country_tells_two_places_of_the_same_name_apart(db, wire):
    """Reading in England is not Reading in Pennsylvania."""
    _story(db, wire, "Reading city council approves budget", country="US")

    got = query_articles(db, {"geos": [READING]}, limit=40)

    assert got == [], "a US story matched a location watched in GB"


def test_a_story_with_no_country_is_not_discarded(db, wire):
    """Most articles state no country; requiring one would empty the column."""
    _story(db, wire, "Flooding closes roads around Reading", country="")
    assert len(query_articles(db, {"geos": [READING]}, limit=40)) == 1


def test_the_name_matches_whole_words_only(db, wire):
    _story(db, wire, "Proofreadings of the manuscript continue", country="GB")
    assert query_articles(db, {"geos": [READING]}, limit=40) == []


def test_a_very_short_name_is_ignored(db, wire):
    """Two letters appear inside half of all words; that is not a signal."""
    tiny = {"type": "Circle", "center": [0, 0], "radius_km": 5,
            "name": "Ur", "country": ""}
    _story(db, wire, "Further disruption expected during the week")
    assert query_articles(db, {"geos": [tiny]}, limit=40) == []


def test_a_passing_mention_in_the_body_does_not_count(db, wire):
    """A watched place is matched on headline and summary, not the whole body.

    A story *about* somewhere names it up front; deep in the body it is usually
    a dateline or a list. Keeping the body out is also what lets this feed scan
    deeply enough to find anything.
    """
    a = _story(db, wire, "Transport update for the south east", country="GB")
    a.content = "…services also called at Reading before continuing…"
    db.commit()

    assert query_articles(db, {"geos": [READING]}, limit=40) == []


# ---------- reach ----------

def test_the_feed_looks_further_back_than_an_ordinary_one(db, wire):
    """A watched place has no text for the index, so depth is all it has.

    At the old default of 2,000 rows, a place appearing once in a few hundred
    articles showed a handful of stories while the archive held far more.
    """
    for i in range(3000):
        _story(db, wire, f"Routine coverage of nothing much ({i})", minutes_ago=i + 10)
    _story(db, wire, "Fire crews called to Reading industrial estate",
           country="GB", minutes_ago=2500)

    got = query_articles(db, {"geos": [READING]}, limit=40)

    assert len(got) == 1, "the match was older than the default scan window"


# ---------- a place gets a source of its own ----------

def test_saving_a_location_starts_gathering_news_about_it(client, register, db):
    headers = register("reader")
    resp = client.post("/api/locations", json={
        "name": "Home", "place_name": "Reading", "country": "GB",
        "lat": 51.4543, "lon": -0.9781, "radius_km": 25}, headers=headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["has_source"] is True

    source = db.query(Source).filter(Source.added_by == "location").one()
    assert "Reading" in source.rss_url
    assert "gl=GB" in source.rss_url, "asked the wrong country's edition"
    assert source.scope == "local"


def test_the_readers_label_is_not_what_gets_searched(client, register, db):
    """"Dad's house" is a fine name for a place and a useless search query."""
    headers = register("reader")
    client.post("/api/locations", json={
        "name": "Dad's house", "place_name": "Reading", "country": "GB",
        "lat": 51.4543, "lon": -0.9781, "radius_km": 25}, headers=headers)

    source = db.query(Source).filter(Source.added_by == "location").one()
    assert "Reading" in source.rss_url
    assert "house" not in source.rss_url.lower()


def test_a_point_with_no_name_gathers_nothing(client, register, db):
    """Nothing to search for. The circle still flags what arrives anyway."""
    headers = register("reader")
    resp = client.post("/api/locations", json={
        "name": "That spot", "lat": 12.5, "lon": 44.2, "radius_km": 30}, headers=headers)

    assert resp.json()["has_source"] is False
    assert db.query(Source).filter(Source.added_by == "location").count() == 0


def test_renaming_the_place_moves_its_source_rather_than_adding_one(client, register, db):
    """The leak to avoid: an orphaned source is polled forever for nobody."""
    headers = register("reader")
    loc = client.post("/api/locations", json={
        "name": "Home", "place_name": "Reading", "country": "GB",
        "lat": 51.4543, "lon": -0.9781, "radius_km": 25}, headers=headers).json()

    client.patch(f"/api/locations/{loc['id']}",
                 json={"place_name": "Oxford", "lat": 51.752, "lon": -1.2577},
                 headers=headers)

    sources = db.query(Source).filter(Source.added_by == "location").all()
    assert len(sources) == 1, "renaming left the old source behind"
    assert "Oxford" in sources[0].rss_url


def test_deleting_a_location_takes_its_source_away(client, register, db):
    headers = register("reader")
    loc = client.post("/api/locations", json={
        "name": "Home", "place_name": "Reading", "country": "GB",
        "lat": 51.4543, "lon": -0.9781, "radius_km": 25}, headers=headers).json()
    assert db.query(Source).filter(Source.added_by == "location").count() == 1

    client.delete(f"/api/locations/{loc['id']}", headers=headers)

    assert db.query(Source).filter(Source.added_by == "location").count() == 0


def test_two_people_watching_one_place_share_its_source(client, register, db):
    """And the second one leaving must not take it from the first."""
    a = register("ann")
    b = register("ben")
    payload = {"name": "Home", "place_name": "Reading", "country": "GB",
               "lat": 51.4543, "lon": -0.9781, "radius_km": 25}
    client.post("/api/locations", json=payload, headers=a)
    second = client.post("/api/locations", json=payload, headers=b).json()

    assert db.query(Source).filter(Source.added_by == "location").count() == 1

    client.delete(f"/api/locations/{second['id']}", headers=b)

    assert db.query(Source).filter(Source.added_by == "location").count() == 1, (
        "one reader leaving removed the source the other still relies on")


def test_the_saved_place_survives_a_round_trip(client, register):
    """Editing must not quietly drop the searchable name."""
    headers = register("reader")
    client.post("/api/locations", json={
        "name": "Home", "place_name": "Reading", "country": "GB",
        "lat": 51.4543, "lon": -0.9781, "radius_km": 25}, headers=headers)

    listed = client.get("/api/locations", headers=headers).json()[0]

    assert listed["place_name"] == "Reading"
    assert listed["country"] == "GB"


def test_the_feed_criteria_carry_the_name(client, register, db):
    """Which is what makes the column match at all."""
    headers = register("reader")
    client.post("/api/locations", json={
        "name": "Home", "place_name": "Reading", "country": "GB",
        "lat": 51.4543, "lon": -0.9781, "radius_km": 25}, headers=headers)

    from backend.app.models import Feed
    feed = db.query(Feed).filter(Feed.user_id.like("acct:%")).first()
    area = feed.criteria["geos"][0]
    assert area["name"] == "Reading"
    assert area["country"] == "GB"
