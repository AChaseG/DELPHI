"""Favourite locations: multiple geofences, flagging, auto-feed, sharing."""

import pytest

from backend.app.matching import CriteriaMatcher, query_articles
from backend.app.models import Article, Source, utcnow

TOKYO = (35.68, 139.69)
LONDON = (51.51, -0.13)
LIMA = (-12.05, -77.04)


def _article(db, src, title, lat, lon, place="Somewhere", country="XX"):
    a = Article(source_id=src.id, url=f"http://x/{title}", guid=title, title=title,
                summary="", content="", published_at=utcnow(), fetched_at=utcnow(),
                language="en", country=country, categories=[],
                places=[{"name": place, "country": country, "lat": lat, "lon": lon}],
                importance=50)
    db.add(a)
    return a


@pytest.fixture
def corpus(db):
    src = Source(name="S", rss_url="http://s/f", scope="national")
    db.add(src)
    db.flush()
    arts = {
        "tokyo": _article(db, src, "Tokyo story", *TOKYO, "Tokyo", "JP"),
        "london": _article(db, src, "London story", *LONDON, "London", "GB"),
        "lima": _article(db, src, "Lima story", *LIMA, "Lima", "PE"),
    }
    db.commit()
    return src, arts


def circle(point, km):
    return {"type": "Circle", "center": list(point), "radius_km": km}


def test_multiple_geofences_match_any(db, corpus):
    """Two areas in one feed must return articles from either — the point of
    multiple fences is OR, not AND."""
    _, arts = corpus
    got = query_articles(db, {"geos": [circle(TOKYO, 60), circle(LONDON, 60)]}, limit=50)
    titles = {a.title for a in got}
    assert titles == {"Tokyo story", "London story"}


def test_legacy_single_geo_still_works(db, corpus):
    got = query_articles(db, {"geo": circle(TOKYO, 60)}, limit=50)
    assert {a.title for a in got} == {"Tokyo story"}


def test_legacy_geo_and_geos_combine(db, corpus):
    """A feed saved before multi-area support, then edited, carries both keys."""
    m = CriteriaMatcher({"geo": circle(TOKYO, 60), "geos": [circle(LIMA, 60)]})
    assert len(m.geos) == 2
    got = query_articles(db, {"geo": circle(TOKYO, 60), "geos": [circle(LIMA, 60)]}, limit=50)
    assert {a.title for a in got} == {"Tokyo story", "Lima story"}


def test_create_location_makes_its_own_feed(client, register):
    hdr = register("loc1")
    r = client.post("/api/locations", json={
        "name": "Home", "lat": TOKYO[0], "lon": TOKYO[1], "radius_km": 40}, headers=hdr)
    assert r.status_code == 201, r.text
    loc = r.json()
    assert loc["feed_id"], "a location should come with a feed"

    feeds = client.get("/api/feeds", headers=hdr).json()
    mine = [f for f in feeds if f["id"] == loc["feed_id"]][0]
    assert mine["name"] == "📍 Home"
    assert mine["criteria"]["geos"][0]["radius_km"] == 40


def test_articles_inside_a_location_are_flagged(client, register, db, corpus):
    hdr = register("loc2")
    client.post("/api/locations", json={
        "name": "Tokyo watch", "lat": TOKYO[0], "lon": TOKYO[1], "radius_km": 60}, headers=hdr)
    found = client.post("/api/articles/search?limit=50", json={"criteria": {}}, headers=hdr)
    assert found.status_code == 200
    by_title = {a["title"]: a for a in found.json()}
    assert [n["name"] for n in by_title["Tokyo story"]["near"]] == ["Tokyo watch"]
    assert by_title["London story"]["near"] == []


def test_editing_a_location_moves_its_feed(client, register):
    hdr = register("loc3")
    loc = client.post("/api/locations", json={
        "name": "Desk", "lat": TOKYO[0], "lon": TOKYO[1], "radius_km": 30}, headers=hdr).json()
    client.patch(f"/api/locations/{loc['id']}",
                 json={"name": "Desk 2", "radius_km": 90}, headers=hdr)
    feed = [f for f in client.get("/api/feeds", headers=hdr).json()
            if f["id"] == loc["feed_id"]][0]
    assert feed["name"] == "📍 Desk 2"
    assert feed["criteria"]["geos"][0]["radius_km"] == 90


def test_deleting_a_location_removes_its_feed(client, register):
    hdr = register("loc4")
    loc = client.post("/api/locations", json={
        "name": "Temp", "lat": 0, "lon": 0, "radius_km": 10}, headers=hdr).json()
    assert client.delete(f"/api/locations/{loc['id']}", headers=hdr).status_code == 204
    assert loc["feed_id"] not in [f["id"] for f in client.get("/api/feeds", headers=hdr).json()]


def test_deleting_can_keep_the_feed(client, register):
    hdr = register("loc5")
    loc = client.post("/api/locations", json={
        "name": "Keep", "lat": 0, "lon": 0, "radius_km": 10}, headers=hdr).json()
    client.delete(f"/api/locations/{loc['id']}?keep_feed=true", headers=hdr)
    assert loc["feed_id"] in [f["id"] for f in client.get("/api/feeds", headers=hdr).json()]


def test_rejects_impossible_coordinates(client, register):
    hdr = register("loc6")
    bad = client.post("/api/locations",
                      json={"name": "Nowhere", "lat": 999, "lon": 0}, headers=hdr)
    assert bad.status_code == 422
    assert "map" in bad.json()["detail"].lower()


def test_shared_location_flags_articles_for_other_members(client, db, corpus):
    def reg(name):
        r = client.post("/api/auth/register", json={
            "username": name, "email": f"{name}@example.com", "password": "password123"})
        return {"Authorization": "Bearer " + r.json()["token"]}

    owner, member = reg("owner"), reg("member")
    pid = client.post("/api/pantheons", json={"name": "Desk"}, headers=owner).json()["id"]
    client.post(f"/api/pantheons/{pid}/invite", json={"user": "member"}, headers=owner)
    inv = client.get("/api/pantheons", headers=member).json()["invites"][0]
    client.post(f"/api/pantheons/invites/{inv['id']}/accept", headers=member)

    loc = client.post("/api/locations", json={
        "name": "Tokyo desk", "lat": TOKYO[0], "lon": TOKYO[1], "radius_km": 60},
        headers=owner).json()
    assert client.post(f"/api/locations/{loc['id']}/share",
                       json={"pantheon_id": pid}, headers=owner).status_code == 201

    # The member sees it, and their articles are flagged by it.
    names = [x["name"] for x in client.get("/api/locations", headers=member).json()]
    assert "Tokyo desk" in names
    found = client.post("/api/articles/search?limit=50", json={"criteria": {}}, headers=member)
    by_title = {a["title"]: a for a in found.json()}
    assert [n["name"] for n in by_title["Tokyo story"]["near"]] == ["Tokyo desk"]


def test_place_search_is_local(client, register):
    hdr = register("loc7")
    r = client.get("/api/geo/search?q=tokyo", headers=hdr)
    assert r.status_code == 200
    hits = r.json()
    assert hits and hits[0]["name"] == "Tokyo" and hits[0]["country"] == "JP"
    # Native-script names resolve too.
    assert client.get("/api/geo/search?q=東京", headers=hdr).json()[0]["name"] == "Tokyo"
