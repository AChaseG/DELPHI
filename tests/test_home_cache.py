"""The warm Home board: same answers as matching live, and no drift.

Home's columns are matched by the poller and served from the ids it kept, so
two things have to hold. The warmed answer must be the answer a live match
would have given, and the server's idea of what Home's columns *are* must stay
identical to the client's — a column the server doesn't recognize is matched
live, which is correct but slow, and silent.
"""
import json
import re
import time
from datetime import timedelta
from pathlib import Path

from backend.app import home
from backend.app.models import Article, Source, utcnow

APP_JS = Path(__file__).resolve().parent.parent / "frontend" / "js" / "app.js"


def _client_columns() -> list[dict]:
    """The DELPHI_FEEDS array out of app.js, as data."""
    src = APP_JS.read_text(encoding="utf-8")
    block = src[src.index("const DELPHI_FEEDS = ["):]
    block = block[:block.index("\n];") + 2]
    out = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("{ home:"):
            continue
        hid = re.search(r'home: "([^"]+)"', line).group(1)
        criteria = re.search(r"criteria: (\{.*?\}), sort:", line).group(1)
        sort = re.search(r'sort: "([^"]+)"', line).group(1)
        # JS object literal -> JSON: quote the keys, which are bare identifiers.
        criteria = re.sub(r"(\w+):", r'"\1":', criteria).replace("'", '"')
        out.append({
            "id": hid,
            "criteria": json.loads(criteria),
            "sort": sort,
            "grouped": "group_events: true" in line,
        })
    return out


def test_server_and_client_agree_on_what_home_is():
    theirs = _client_columns()
    assert theirs, "could not read DELPHI_FEEDS out of app.js"
    ours = {c["id"]: c for c in home.HOME_COLUMNS}
    assert sorted(ours) == sorted(c["id"] for c in theirs)
    for col in theirs:
        mine = ours[col["id"]]
        assert home._normalize(mine["criteria"]) == home._normalize(col["criteria"]), col["id"]
        assert mine["sort"] == col["sort"], col["id"]
        assert mine["grouped"] == col["grouped"], col["id"]


def _seed(db, n=60):
    src = Source(name="Wire", rss_url="http://example.com/rss", country="us",
                 language="en", scope="international", platform="news")
    db.add(src)
    db.flush()
    now = utcnow()
    for i in range(n):
        db.add(Article(
            source_id=src.id, title=f"Story {i}", summary="body", url=f"http://e.com/{i}",
            language="en", country="us", categories=["politics"] if i % 2 else ["business"],
            importance=90 if i % 3 == 0 else 20,
            published_at=now - timedelta(hours=i % 12),
            fetched_at=now,
        ))
    db.commit()
    return src


def test_warmed_column_matches_a_live_match(db):
    from backend.app.matching import query_articles
    _seed(db)
    home.refresh(db)
    for col in home.HOME_COLUMNS:
        limit = home.GROUPED_QUERY_LIMIT if col["grouped"] else home.PLAIN_LIMIT
        live = query_articles(db, col["criteria"], sort=col["sort"], limit=limit)
        warm = home.take(db, col["criteria"], col["sort"],
                         home.GROUPED_REQUEST_LIMIT if col["grouped"] else home.PLAIN_LIMIT,
                         grouped=col["grouped"])
        assert warm is not None, f"{col['id']} was not warmed"
        assert [a.id for a in warm] == [a.id for a in live], col["id"]


def test_a_column_nobody_warmed_is_not_served_from_the_cache(db):
    _seed(db)
    home.clear()
    col = home.HOME_COLUMNS[3]
    assert home.take(db, col["criteria"], col["sort"], home.PLAIN_LIMIT, grouped=False) is None


def test_someone_elses_search_is_never_served_from_the_cache(db):
    _seed(db)
    home.refresh(db)
    # Same shape, different criteria: must not be mistaken for a Home column.
    assert home.take(db, {"categories": ["politics", "sport"]}, "newest",
                     home.PLAIN_LIMIT, grouped=False) is None
    # Home's criteria, but a limit the client never sends for them.
    col = home.HOME_COLUMNS[3]
    assert home.take(db, col["criteria"], col["sort"], 7, grouped=False) is None
    # Home's criteria, sorted the other way.
    assert home.take(db, col["criteria"], "importance", home.PLAIN_LIMIT, grouped=False) is None
    # A plain column asked for as a grouped one.
    assert home.take(db, col["criteria"], col["sort"], home.PLAIN_LIMIT, grouped=True) is None


def test_criteria_are_recognized_whatever_order_they_arrive_in(db):
    _seed(db)
    home.refresh(db)
    got = home.take(db, {"categories": ["economy", "business"]}, "newest",
                    home.PLAIN_LIMIT, grouped=False)
    assert got is not None    # the client may serialize a list in any order


def test_two_watched_areas_do_not_break_the_way_in(db, register, client):
    """Recognizing a column means canonicalizing the criteria, which meant
    sorting their lists — and two map areas are dicts, which raise TypeError
    when compared. Every search goes through here, so a feed with two areas on
    the map answered 500 instead of returning news."""
    area = {"type": "Circle", "name": "Reading", "lat": 51.45, "lon": -0.97,
            "radius_km": 25}
    criteria = {"geos": [area, dict(area, name="Slough", lon=-0.6)]}
    assert home.take(db, criteria, "newest", home.PLAIN_LIMIT, grouped=False) is None
    res = client.post("/api/articles/search?limit=5", json={"criteria": criteria},
                      headers=register("twoareas"))
    assert res.status_code == 200, res.text


def test_a_stale_warm_list_is_refused(db, monkeypatch):
    _seed(db)
    home.refresh(db)
    col = home.HOME_COLUMNS[3]
    assert home.take(db, col["criteria"], col["sort"], home.PLAIN_LIMIT, grouped=False)
    later = time.monotonic() + home.MAX_AGE_S + 1     # read the real clock first
    monkeypatch.setattr(home.time, "monotonic", lambda: later)
    assert home.take(db, col["criteria"], col["sort"], home.PLAIN_LIMIT, grouped=False) is None


def test_articles_deleted_since_the_warm_up_just_drop_out(db):
    _seed(db)
    home.refresh(db)
    col = home.HOME_COLUMNS[3]
    before = home.take(db, col["criteria"], col["sort"], home.PLAIN_LIMIT, grouped=False)
    assert len(before) > 2
    db.delete(db.get(Article, before[0].id))
    db.commit()
    after = home.take(db, col["criteria"], col["sort"], home.PLAIN_LIMIT, grouped=False)
    assert [a.id for a in after] == [a.id for a in before[1:]]


def test_the_endpoint_serves_the_same_articles_warm_or_cold(client, register, db):
    _seed(db)
    auth = register("homer")
    col = next(c for c in home.HOME_COLUMNS if not c["grouped"] and c["id"] == "politics")
    body = {"criteria": col["criteria"]}
    qs = f"?sort={col['sort']}&limit={home.PLAIN_LIMIT}"

    home.clear()
    cold = client.post("/api/articles/search" + qs, json=body, headers=auth).json()
    home.refresh(db)
    warm = client.post("/api/articles/search" + qs, json=body, headers=auth).json()
    assert [a["id"] for a in warm] == [a["id"] for a in cold]
    assert warm == cold          # including every per-reader field


def test_grouped_endpoint_serves_the_same_events_warm_or_cold(client, register, db):
    _seed(db)
    auth = register("grouper")
    col = next(c for c in home.HOME_COLUMNS if c["grouped"] and c["id"] == "top")
    body = {"criteria": col["criteria"]}
    qs = f"?sort={col['sort']}&limit={home.GROUPED_REQUEST_LIMIT}"

    home.clear()
    cold = client.post("/api/articles/search-grouped" + qs, json=body, headers=auth).json()
    home.refresh(db)
    warm = client.post("/api/articles/search-grouped" + qs, json=body, headers=auth).json()
    assert warm == cold


def test_one_readers_history_never_reaches_another(client, register, db):
    """The warm list is shared; "already opened" must not be."""
    _seed(db)
    alice, bob = register("alicehome"), register("bobhome")
    col = next(c for c in home.HOME_COLUMNS if c["id"] == "politics")
    qs = f"?sort={col['sort']}&limit={home.PLAIN_LIMIT}"
    body = {"criteria": col["criteria"]}
    home.refresh(db)

    first = client.post("/api/articles/search" + qs, json=body, headers=alice).json()
    event_id = next((a["event_id"] for a in first if a["event_id"]), None)
    if event_id is None:            # no clustering in this fixture; make one
        art = db.get(Article, first[0]["id"])
        from backend.app.models import Event
        ev = Event(title="E", first_seen=utcnow(), updated_at=utcnow())
        db.add(ev)
        db.flush()
        art.event_id = ev.id
        db.commit()
        home.refresh(db)
        event_id = ev.id
    client.post(f"/api/events/{event_id}/viewed", headers=alice)

    hers = client.post("/api/articles/search" + qs, json=body, headers=alice).json()
    his = client.post("/api/articles/search" + qs, json=body, headers=bob).json()
    assert any(a["viewed"] for a in hers if a["event_id"] == event_id)
    assert not any(a["viewed"] for a in his)
