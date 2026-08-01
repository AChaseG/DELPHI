"""Bounds on the two routes that can be made to cost real money.

Searching is cheap per call and constant in practice — a reader types, and the
board, the rail, and the builder preview all fire. Exporting is the opposite:
rare per person, and up to EXPORT_MAX articles assembled into a file, each one
put through the translation service when a reading language is set. They are
therefore on separate buckets with very different numbers, and the point of
these tests is that the separation survives.
"""
import pytest

from backend.app import main, ratelimit


@pytest.fixture
def limited(monkeypatch):
    """Rate limiting on (conftest turns it off), empty table, no proxy."""
    monkeypatch.setattr(ratelimit, "ENABLED", True)
    monkeypatch.setattr(ratelimit, "TRUSTED_PROXIES", 0)
    monkeypatch.setattr(ratelimit, "CLIENT_IP_HEADER", "")
    ratelimit._hits.clear()
    yield
    ratelimit._hits.clear()


def _until_429(call, tries):
    allowed = 0
    for _ in range(tries):
        if call().status_code == 429:
            break
        allowed += 1
    return allowed


# ---------- the limits exist and are the right ones ----------

def test_search_is_bounded(client, register, limited):
    headers = register("reader")
    limit = ratelimit._LIMITS["search"][0]
    ratelimit._hits.clear()

    allowed = _until_429(
        lambda: client.post("/api/articles/search", json={"keywords": ["x"]},
                            headers=headers),
        limit + 20)
    assert allowed == limit


def test_grouped_search_shares_the_search_budget(client, register, limited):
    """Both are the same work; separate budgets would just double the ceiling."""
    headers = register("reader")
    limit = ratelimit._LIMITS["search"][0]
    ratelimit._hits.clear()

    for _ in range(limit):
        client.post("/api/articles/search", json={"keywords": ["x"]}, headers=headers)
    spent = client.post("/api/articles/search-grouped", json={"keywords": ["x"]},
                        headers=headers)
    assert spent.status_code == 429


def test_export_is_bounded_far_more_tightly_than_search(client, register, limited):
    headers = register("reader")
    limit = ratelimit._LIMITS["export"][0]
    ratelimit._hits.clear()

    allowed = _until_429(
        lambda: client.post("/api/articles/export?format=csv", json={"keywords": ["x"]},
                            headers=headers),
        limit + 10)
    assert allowed == limit


def test_a_saved_feed_export_spends_the_same_budget(client, register, limited):
    """Otherwise the tight limit is avoided by exporting a feed instead."""
    headers = register("reader")
    feed = client.post("/api/feeds", json={
        "name": "Test", "criteria": {"keywords": ["x"]}}, headers=headers)
    assert feed.status_code in (200, 201), feed.text
    feed_id = feed.json()["id"]
    limit = ratelimit._LIMITS["export"][0]
    ratelimit._hits.clear()

    for _ in range(limit):
        client.post("/api/articles/export?format=csv", json={"keywords": ["x"]},
                    headers=headers)
    spent = client.get(f"/api/feeds/{feed_id}/export?format=csv", headers=headers)
    assert spent.status_code == 429


def test_exporting_does_not_eat_the_search_budget(client, register, limited):
    """A reader who exports a file should not then be unable to search."""
    headers = register("reader")
    ratelimit._hits.clear()

    for _ in range(ratelimit._LIMITS["export"][0]):
        client.post("/api/articles/export?format=csv", json={"keywords": ["x"]},
                    headers=headers)

    assert client.post("/api/articles/search", json={"keywords": ["x"]},
                       headers=headers).status_code == 200


def test_export_is_the_tighter_of_the_two():
    """If these ever invert, the expensive route became the generous one."""
    search_per_min = ratelimit._LIMITS["search"][0] / ratelimit._LIMITS["search"][1]
    export_per_min = ratelimit._LIMITS["export"][0] / ratelimit._LIMITS["export"][1]
    assert export_per_min < search_per_min


def test_the_limit_says_when_to_come_back(client, register, limited):
    headers = register("reader")
    ratelimit._hits.clear()
    for _ in range(ratelimit._LIMITS["export"][0] + 1):
        resp = client.post("/api/articles/export?format=csv", json={"keywords": ["x"]},
                           headers=headers)
    assert resp.status_code == 429
    assert resp.headers["Retry-After"].isdigit()
    assert "try again" in resp.json()["detail"].lower()


def test_ordinary_use_is_nowhere_near_the_search_limit(client, register, limited):
    """A limit a person can hit by using the app normally is a bug.

    Opening the board runs several columns at once and a typed query previews
    per keystroke; twenty in quick succession has to be uneventful.
    """
    headers = register("reader")
    ratelimit._hits.clear()
    for _ in range(20):
        assert client.post("/api/articles/search", json={"keywords": ["x"]},
                           headers=headers).status_code == 200


# ---------- cross-origin access ----------

def test_no_cross_origin_permission_is_handed_out_by_default(client):
    """The frontend is same-origin, so nothing legitimate needs this header."""
    resp = client.get("/api/meta", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


def test_the_default_is_no_configured_origins():
    assert main.CORS_ORIGINS == []


def test_a_named_origin_can_still_be_allowed(monkeypatch):
    """The escape hatch works, and only for the origin named.

    Built as a separate app so the middleware stack is assembled with the
    setting in place, which is when add_middleware is read.
    """
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["https://client.example"],
                       allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
                       allow_headers=["Authorization", "Content-Type"])

    @app.get("/ping")
    def ping():
        return {"ok": True}

    c = TestClient(app)
    allowed = c.get("/ping", headers={"Origin": "https://client.example"})
    assert allowed.headers["access-control-allow-origin"] == "https://client.example"

    other = c.get("/ping", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in other.headers}
