"""In-process API tests (FastAPI TestClient): auth, account settings, and the
Pantheon share/permission flow across accounts."""
from pathlib import Path


def test_register_login_and_gate(client, register):
    hdr = register("alice")
    # protected route needs the token
    assert client.get("/api/feeds").status_code == 401
    assert client.get("/api/feeds", headers=hdr).status_code == 200
    r = client.post("/api/auth/login", json={"username": "alice", "password": "password123"})
    assert r.status_code == 200 and r.json()["token"]
    assert client.post("/api/auth/login",
                       json={"username": "alice", "password": "wrong"}).status_code == 401


def test_account_settings_persist(client, register):
    hdr = register("bob")
    assert client.get("/api/session/settings", headers=hdr).json()["settings"] == {}
    client.put("/api/session/settings", headers=hdr,
               json={"settings": {"theme": "light", "lang": "fr", "volume": 10}})
    got = client.get("/api/session/settings", headers=hdr).json()["settings"]
    assert got == {"theme": "light", "lang": "fr", "volume": 10}


def test_pantheon_share_and_permissions(client, register):
    alice = register("alice")
    bob = register("bob")
    carol = register("carol")

    pid = client.post("/api/pantheons", headers=alice,
                      json={"name": "Watch Desk", "visibility": "private"}).json()["id"]
    fid = client.post("/api/feeds", headers=alice, json={
        "name": "Quake", "criteria": {"keywords": ["earthquake"], "auto_coverage": False}}).json()["id"]

    # outsider cannot see a private pantheon
    assert client.get(f"/api/pantheons/{pid}", headers=carol).status_code == 403

    # invite + accept
    assert client.post(f"/api/pantheons/{pid}/invite", headers=alice,
                       json={"user": "bob"}).status_code == 200
    inv = client.get("/api/pantheons", headers=bob).json()["invites"]
    assert len(inv) == 1
    client.post(f"/api/pantheons/invites/{inv[0]['id']}/accept", headers=bob)

    # share the feed; member sees it (read-only), outsider is blocked
    sid = client.post(f"/api/feeds/{fid}/share", headers=alice,
                      json={"pantheon_id": pid}).json()["id"]
    shared = client.get(f"/api/pantheons/{pid}/feeds", headers=bob).json()
    assert len(shared) == 1 and shared[0]["shared_by"] == "alice" and shared[0]["can_edit"] is False
    assert client.get(f"/api/feeds/{sid}/articles", headers=bob).status_code == 200
    assert client.get(f"/api/feeds/{sid}/articles", headers=carol).status_code == 403

    # a plain member cannot edit someone else's shared feed
    body = {"name": "Quake", "criteria": {"keywords": ["quake"], "auto_coverage": False}}
    assert client.put(f"/api/feeds/{sid}", headers=bob, json=body).status_code == 403
    assert client.put(f"/api/feeds/{sid}", headers=alice, json=body).status_code == 200


def test_registration_requires_valid_email_and_password(client):
    assert client.post("/api/auth/register",
                       json={"username": "x", "email": "a@b.com", "password": "password123"}
                       ).status_code == 422  # username too short
    assert client.post("/api/auth/register",
                       json={"username": "gooduser", "email": "nope", "password": "password123"}
                       ).status_code == 422  # bad email
    assert client.post("/api/auth/register",
                       json={"username": "gooduser", "email": "a@b.com", "password": "short"}
                       ).status_code == 422  # weak password


def test_action_links_use_a_path_segment(client, monkeypatch):
    """Reset/verify tokens ride in the path. Query strings are dropped by
    registrar domain-forwarding, and fragments are percent-encoded to %23 by
    mail link-rewriters — both were observed turning a live reset link into a
    dead end."""
    from backend.app import mailer
    sent = []
    monkeypatch.setattr(mailer, "enabled", lambda: True)
    monkeypatch.setattr(mailer, "send_password_reset",
                        lambda to, user, link: sent.append(link) or True)
    monkeypatch.setattr(mailer, "send_verification",
                        lambda to, user, link: sent.append(link) or True)
    monkeypatch.setenv("NEWS_PUBLIC_URL", "https://delphi-news.com")

    client.post("/api/auth/register", json={
        "username": "linkuser", "email": "linkuser@example.com", "password": "password123"})
    client.post("/api/auth/forgot", json={"email": "linkuser@example.com"})

    assert sent, "no action link was generated"
    for link in sent:
        assert "/reset/" in link or "/verify/" in link, \
            f"token should be a path segment: {link}"
        assert "?" not in link and "#" not in link, \
            f"query strings get stripped and fragments get %23-encoded by mail rewriters: {link}"


def test_action_link_paths_serve_the_app(client):
    """/reset/<token> and /verify/<token> must return the app, not a 404 —
    the static mount alone 404s on any path that isn't a file on disk."""
    for path in ("/reset/sometoken", "/verify/sometoken"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        assert "<html" in r.text.lower()


def test_index_assets_are_absolute():
    """Relative asset URLs break when the page is served from a nested path:
    /reset/<token> would request /reset/js/app.js and get HTML back, so no
    script runs and the reset form never appears."""
    import pathlib
    html = pathlib.Path("frontend/index.html").read_text()
    for asset in ('js/app.js', 'js/api.js', 'css/styles.css'):
        assert f'"/{asset}"' in html, f"{asset} should be referenced absolutely"


def test_blank_names_are_rejected_with_a_reason(client, register):
    """A nameless feed/alert used to be accepted, so the board grew items the
    user couldn't tell apart. The rejection must name the field."""
    hdr = register("namer")
    for path in ("/api/alerts", "/api/feeds"):
        r = client.post(path, json={"name": "   ", "criteria": {}}, headers=hdr)
        assert r.status_code == 422, f"{path} -> {r.status_code}"
        detail = r.json()["detail"]
        # FastAPI returns validation errors as a list of {loc, msg}; the client
        # formats these for display, so the field must be identifiable.
        assert isinstance(detail, list) and detail
        assert "name" in detail[0]["loc"]
        assert "name" in detail[0]["msg"].lower()


def test_names_are_trimmed(client, register):
    hdr = register("trimmer")
    r = client.post("/api/alerts", json={"name": "  Tokyo watch  ", "criteria": {}}, headers=hdr)
    assert r.status_code == 201
    alerts = client.get("/api/alerts", headers=hdr).json()
    assert alerts[0]["name"] == "Tokyo watch"


def test_startup_removes_leftover_demo_data(db):
    """Demo generation is gone, but instances seeded by an older build still
    carry sample rows and the button that cleared them no longer exists — so
    startup cleans them, and must not touch real reporting."""
    from backend.app.main import _purge_demo_data
    from backend.app.models import Article, Source, utcnow

    demo = Source(name="Sample", rss_url="https://example.org/demo.xml",
                  homepage="https://example.org", country="US", language="en",
                  scope="national", categories=[])
    real = Source(name="Real wire", rss_url="https://news.example.com/rss",
                  homepage="https://news.example.com", country="GB", language="en",
                  scope="international", categories=[])
    db.add_all([demo, real])
    db.flush()
    db.add(Article(source_id=demo.id, url="https://example.org/demo/1/0", guid="d0",
                   title="Sample story", summary="", content="",
                   published_at=utcnow(), fetched_at=utcnow(), language="en",
                   country="US", categories=[], places=[], importance=50))
    db.add(Article(source_id=real.id, url="https://news.example.com/a/1", guid="r1",
                   title="Real story", summary="", content="",
                   published_at=utcnow(), fetched_at=utcnow(), language="en",
                   country="GB", categories=[], places=[], importance=50))
    db.commit()

    removed = _purge_demo_data(db)

    assert removed == 1
    assert db.query(Article).filter(Article.url.like("https://example.org/demo/%")).count() == 0
    assert db.query(Source).filter(Source.rss_url.like("https://example.org%")).count() == 0
    # Real reporting survives untouched.
    assert db.query(Article).filter_by(guid="r1").count() == 1
    assert db.query(Source).filter_by(name="Real wire").count() == 1
    # Idempotent: a second run on a clean database does nothing.
    assert _purge_demo_data(db) == 0


def test_demo_endpoints_are_gone(client, register):
    hdr = register("nodemo")
    for path in ("/api/demo/seed", "/api/demo/purge"):
        assert client.post(path, headers=hdr).status_code == 404


def test_text_responses_are_compressed_but_live_updates_are_not(client, register):
    """Compression is most of what a phone away from wi-fi is waiting for, and
    it must not reach the alert stream: an event held back until a compression
    buffer fills is an alert that arrives late."""
    from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES

    hdr = register("zipper")

    page = client.get("/js/app.js", headers={"Accept-Encoding": "gzip"})
    assert page.headers.get("content-encoding") == "gzip"
    assert "Accept-Encoding" in page.headers.get("vary", "")
    # A client that says it cannot take gzip is still served plain text.
    plain = client.get("/js/app.js", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers

    listing = client.get("/api/sources?slim=1", headers={**hdr, "Accept-Encoding": "gzip"})
    assert listing.headers.get("content-encoding") == "gzip"

    # The live stream is exempt by its content type. Reading the stream itself
    # here would never return — it stays open for the life of the session — so
    # what is checked is the exemption the middleware applies to it.
    assert "text/event-stream" in DEFAULT_EXCLUDED_CONTENT_TYPES
    assert 'media_type="text/event-stream"' in \
        (Path(__file__).resolve().parent.parent
         / "backend" / "app" / "main.py").read_text()
