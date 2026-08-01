"""Favourite locations: multiple geofences, flagging, the shared feed, sharing."""

import pytest
from sqlalchemy import select

from backend.app.matching import CriteriaMatcher, query_articles
from backend.app.models import Article, FavoriteLocation, Feed, Source, User, utcnow

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


def test_locations_share_one_feed(client, register):
    """Every location the account owns is covered by a single column, not one
    each — a handful of watched places used to bury the rest of the board."""
    hdr = register("loc1")
    made = []
    for name, point, radius in (("Home", TOKYO, 40), ("Office", LONDON, 25),
                                ("Site", LIMA, 80)):
        r = client.post("/api/locations", json={
            "name": name, "lat": point[0], "lon": point[1], "radius_km": radius},
            headers=hdr)
        assert r.status_code == 201, r.text
        made.append(r.json())

    assert len({loc["feed_id"] for loc in made}) == 1, "all three share one feed"
    feeds = client.get("/api/feeds", headers=hdr).json()
    assert len(feeds) == 1, [f["name"] for f in feeds]
    assert feeds[0]["name"] == "📍 Favourite Locations"
    # One area per location, matched as OR.
    radii = sorted(g["radius_km"] for g in feeds[0]["criteria"]["geos"])
    assert radii == [25, 40, 80]


def test_articles_carry_what_the_browser_needs_to_flag_them(client, register, db, corpus):
    """Deciding which favourite locations an article is near moved to the
    browser — it is geometry over data the browser already holds. The server's
    remaining job is to hand over that data: the article's tagged places and its
    country, and the locations themselves."""
    hdr = register("loc2")
    client.post("/api/locations", json={
        "name": "Tokyo watch", "lat": TOKYO[0], "lon": TOKYO[1], "radius_km": 60}, headers=hdr)

    found = client.post("/api/articles/search?limit=50", json={"criteria": {}}, headers=hdr)
    assert found.status_code == 200
    by_title = {a["title"]: a for a in found.json()}
    tokyo = by_title["Tokyo story"]
    assert tokyo["places"] and tokyo["places"][0]["name"] == "Tokyo"
    assert "lat" in tokyo["places"][0] and "lon" in tokyo["places"][0]
    # The work is not done twice: the server no longer ships a verdict.
    assert "near" not in tokyo

    loc = client.get("/api/locations", headers=hdr).json()[0]
    assert (loc["lat"], loc["lon"], loc["radius_km"]) == (TOKYO[0], TOKYO[1], 60)


def test_country_centroids_are_published_for_the_fallback(client, register):
    """An article naming no place is placed at its country's centre, so the
    browser needs those coordinates to reach the same answer the server used to."""
    hdr = register("loc2b")
    countries = client.get("/api/meta", headers=hdr).json()["countries"]
    assert countries
    assert all("lat" in c and "lon" in c for c in countries)
    jp = next(c for c in countries if c["iso2"] == "JP")
    assert 30 < jp["lat"] < 40 and 130 < jp["lon"] < 145


def test_locations_still_match_server_side_for_the_feed(db, corpus):
    """The locations feed is an ordinary geofenced feed, so the server keeps
    matching those areas in SQL — moving the badges to the browser must not have
    moved the feed's own filtering with them."""
    from backend.app.main import _location_circle

    src, arts = corpus
    loc = FavoriteLocation(user_id="acct:1", name="Tokyo watch",
                           lat=TOKYO[0], lon=TOKYO[1], radius_km=60, color="gold")
    db.add(loc)
    db.commit()
    got = query_articles(db, {"geos": [_location_circle(loc)]}, limit=50)
    assert [a.title for a in got] == ["Tokyo story"]


def test_editing_a_location_moves_the_shared_feed(client, register):
    hdr = register("loc3")
    a = client.post("/api/locations", json={
        "name": "Desk", "lat": TOKYO[0], "lon": TOKYO[1], "radius_km": 30}, headers=hdr).json()
    client.post("/api/locations", json={
        "name": "Other", "lat": LIMA[0], "lon": LIMA[1], "radius_km": 15}, headers=hdr)
    client.patch(f"/api/locations/{a['id']}",
                 json={"name": "Desk 2", "radius_km": 90}, headers=hdr)
    feeds = client.get("/api/feeds", headers=hdr).json()
    assert len(feeds) == 1
    # The feed keeps its own name — it is not any one location's — and the
    # edited area moves while the other is left alone.
    assert feeds[0]["name"] == "📍 Favourite Locations"
    assert sorted(g["radius_km"] for g in feeds[0]["criteria"]["geos"]) == [15, 90]


def test_deleting_one_location_keeps_the_feed_for_the_rest(client, register):
    hdr = register("loc4")
    a = client.post("/api/locations", json={
        "name": "Temp", "lat": 0, "lon": 0, "radius_km": 10}, headers=hdr).json()
    client.post("/api/locations", json={
        "name": "Stays", "lat": TOKYO[0], "lon": TOKYO[1], "radius_km": 50}, headers=hdr)

    assert client.delete(f"/api/locations/{a['id']}", headers=hdr).status_code == 204
    feeds = client.get("/api/feeds", headers=hdr).json()
    assert len(feeds) == 1, "the feed survives while another location needs it"
    assert [g["radius_km"] for g in feeds[0]["criteria"]["geos"]] == [50]


def test_deleting_the_last_location_removes_the_feed(client, register):
    hdr = register("loc4b")
    loc = client.post("/api/locations", json={
        "name": "Only", "lat": 0, "lon": 0, "radius_km": 10}, headers=hdr).json()
    assert client.delete(f"/api/locations/{loc['id']}", headers=hdr).status_code == 204
    assert client.get("/api/feeds", headers=hdr).json() == []


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
            "username": name, "email": f"{name}@example.com", "password": "correct-horse-staple"})
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

    # The member's own /api/locations includes it, which is what their browser
    # flags articles against — sharing a location is what puts it in that list.
    shared = [x for x in client.get("/api/locations", headers=member).json()
              if x["name"] == "Tokyo desk"]
    assert shared, "the shared location reaches the other member"
    assert shared[0]["shared_by"] == "owner"
    assert (shared[0]["lat"], shared[0]["radius_km"]) == (TOKYO[0], 60)
    assert shared[0]["mine"] is False


def test_place_search_answers_from_the_gazetteer(client, register):
    """A place Delphi ships is answered here, without asking anyone else."""
    hdr = register("loc7")
    r = client.get("/api/geo/search?q=tokyo", headers=hdr)
    assert r.status_code == 200
    hits = r.json()["results"]
    assert hits and hits[0]["name"] == "Tokyo" and hits[0]["country"] == "JP"
    assert hits[0]["source"] == "local"
    # Native-script names resolve too.
    native = client.get("/api/geo/search?q=東京", headers=hdr).json()["results"]
    assert native[0]["name"] == "Tokyo"


def test_old_per_location_feeds_are_folded_into_one(client, register, db):
    """Accounts created before locations shared a feed carry one column each.
    Startup consolidation has to merge them without losing an area, and has to
    be safe to run again."""
    from backend.app.main import LOCATIONS_FEED_NAME, _consolidate_location_feeds

    hdr = register("legacy")
    uid = "acct:%d" % db.scalar(select(User.id).where(User.username == "legacy"))

    # Rebuild the old shape by hand: three locations, three feeds.
    made = []
    for i, (name, point, radius) in enumerate((("A", TOKYO, 10), ("B", LONDON, 20),
                                               ("C", LIMA, 30))):
        loc = FavoriteLocation(user_id=uid, name=name, lat=point[0], lon=point[1],
                               radius_km=radius, color="gold")
        db.add(loc)
        db.flush()
        feed = Feed(user_id=uid, name=f"📍 {name}", sort="newest", position=i,
                    criteria={"geos": [{"type": "Circle", "center": list(point),
                                        "radius_km": radius}]})
        db.add(feed)
        db.flush()
        loc.feed_id = feed.id
        made.append(feed.id)
    db.commit()
    assert len(client.get("/api/feeds", headers=hdr).json()) == 3

    assert _consolidate_location_feeds(db) == 1
    feeds = client.get("/api/feeds", headers=hdr).json()
    assert len(feeds) == 1, [f["name"] for f in feeds]
    assert feeds[0]["name"] == LOCATIONS_FEED_NAME
    assert sorted(g["radius_km"] for g in feeds[0]["criteria"]["geos"]) == [10, 20, 30]
    assert feeds[0]["id"] in made, "an existing column is reused, not replaced"

    # Running it again changes nothing.
    assert _consolidate_location_feeds(db) == 0
    assert len(client.get("/api/feeds", headers=hdr).json()) == 1
