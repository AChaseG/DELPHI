"""In-process API tests (FastAPI TestClient): auth, account settings, and the
Pantheon share/permission flow across accounts."""
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
