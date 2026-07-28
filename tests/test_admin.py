"""Operator console: NEWS_ADMIN_USERS designation, access control, and the
account-management endpoints (list / verify / promote / suspend / reset /
delete) with their self-lockout and last-operator guards."""
import pytest

from backend.app import main
from backend.app.database import SessionLocal
from backend.app.models import Alert, Feed, User


def _register(client, username, password="password123"):
    r = client.post("/api/auth/register", json={
        "username": username, "email": f"{username}@example.com", "password": password})
    assert r.status_code == 201, r.text
    body = r.json()
    return {"Authorization": "Bearer " + body["token"]}, int(body["user_key"].split(":")[1])


def _hdr_for(client, uid_username):
    r = client.post("/api/auth/login", json={"username": uid_username, "password": "password123"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


@pytest.fixture
def admin_env(monkeypatch):
    """Designate 'boss' as the built-in operator via NEWS_ADMIN_USERS."""
    monkeypatch.setattr(main, "_ADMIN_HANDLES", frozenset({"boss"}))


def test_designated_operator_bypasses_broken_email(client, monkeypatch):
    """Misconfigured SMTP must not lock the owner out of their own instance:
    with mail 'enabled' but undeliverable, sign-up demands a link that never
    arrives. The NEWS_ADMIN_USERS operator is verified on sight so the console
    that fixes it stays reachable."""
    monkeypatch.setattr(main, "_ADMIN_HANDLES", frozenset({"boss@example.com"}))
    monkeypatch.setattr(main.mailer, "enabled", lambda: True)

    # An ordinary account is held at the verification gate, as designed.
    r = client.post("/api/auth/register", json={
        "username": "alice", "email": "alice@example.com", "password": "password123"})
    assert r.status_code == 201 and r.json().get("verification_sent") is True
    assert client.post("/api/auth/login",
                       json={"username": "alice", "password": "password123"}).status_code == 403

    # The designated operator gets straight in and can reach the console.
    r = client.post("/api/auth/register", json={
        "username": "boss", "email": "boss@example.com", "password": "password123"})
    assert r.status_code == 201, r.text
    login = client.post("/api/auth/login", json={"username": "boss", "password": "password123"})
    assert login.status_code == 200, login.text
    hdr = {"Authorization": "Bearer " + login.json()["token"]}
    assert client.get("/api/admin/users", headers=hdr).status_code == 200

    # ...and can force-verify the account that email left stranded.
    alice_id = [u["id"] for u in client.get("/api/admin/users", headers=hdr).json()["users"]
                if u["username"] == "alice"][0]
    assert client.post(f"/api/admin/users/{alice_id}/verify", headers=hdr).status_code == 200
    assert client.post("/api/auth/login",
                       json={"username": "alice", "password": "password123"}).status_code == 200


def test_non_admin_is_denied(client):
    hdr, _ = _register(client, "alice")
    assert client.get("/api/admin/users", headers=hdr).status_code == 403


def test_meta_reports_admin_flag(client, admin_env):
    boss_hdr, _ = _register(client, "boss")
    alice_hdr, _ = _register(client, "alice")
    assert client.get("/api/meta", headers=boss_hdr).json()["is_admin"] is True
    assert client.get("/api/meta", headers=alice_hdr).json()["is_admin"] is False


def test_configured_admin_lists_users(client, admin_env):
    boss_hdr, boss_id = _register(client, "boss")
    _register(client, "alice")
    data = client.get("/api/admin/users", headers=boss_hdr).json()
    assert data["me"] == boss_id
    assert data["admin_count"] == 1
    names = {u["username"]: u for u in data["users"]}
    assert names["boss"]["is_admin"] and names["boss"]["config_admin"]
    assert not names["alice"]["is_admin"]


def test_promote_and_demote(client, admin_env):
    boss_hdr, _ = _register(client, "boss")
    _, alice_id = _register(client, "alice")
    # Promote alice → she can now reach the console.
    assert client.post(f"/api/admin/users/{alice_id}/admin", json={"is_admin": True},
                       headers=boss_hdr).status_code == 200
    alice_hdr = _hdr_for(client, "alice")
    assert client.get("/api/admin/users", headers=alice_hdr).status_code == 200
    # Demote alice again.
    assert client.post(f"/api/admin/users/{alice_id}/admin", json={"is_admin": False},
                       headers=boss_hdr).status_code == 200
    assert client.get("/api/admin/users", headers=_hdr_for(client, "alice")).status_code == 403


def test_cannot_demote_configured_admin(client, admin_env):
    boss_hdr, boss_id = _register(client, "boss")
    r = client.post(f"/api/admin/users/{boss_id}/admin", json={"is_admin": False}, headers=boss_hdr)
    assert r.status_code == 400


def test_cannot_remove_last_operator(client, monkeypatch):
    # boss registers as the built-in operator (is_admin persisted). Drop the
    # NEWS_ADMIN_USERS designation so boss is now the ONLY operator, held only
    # by the persisted flag — self-demote must hit the last-operator guard.
    monkeypatch.setattr(main, "_ADMIN_HANDLES", frozenset({"boss"}))
    boss_hdr, boss_id = _register(client, "boss")
    monkeypatch.setattr(main, "_ADMIN_HANDLES", frozenset())  # no more built-in designation
    assert client.get("/api/admin/users", headers=boss_hdr).json()["admin_count"] == 1
    r = client.post(f"/api/admin/users/{boss_id}/admin", json={"is_admin": False},
                    headers=boss_hdr)
    assert r.status_code == 400  # boss is the last operator


def test_suspend_blocks_login_and_reinstate_restores(client, admin_env):
    boss_hdr, _ = _register(client, "boss")
    _, alice_id = _register(client, "alice")
    assert client.post(f"/api/admin/users/{alice_id}/disable", json={"disabled": True},
                       headers=boss_hdr).status_code == 200
    r = client.post("/api/auth/login", json={"username": "alice", "password": "password123"})
    assert r.status_code == 403
    assert client.post(f"/api/admin/users/{alice_id}/disable", json={"disabled": False},
                       headers=boss_hdr).status_code == 200
    assert client.post("/api/auth/login",
                       json={"username": "alice", "password": "password123"}).status_code == 200


def test_suspension_revokes_an_existing_session(client, admin_env):
    """A 30-day token carries no revocation, so suspending an account must cut
    off the session it already holds — not just block the next sign-in."""
    boss_hdr, _ = _register(client, "boss")
    alice_hdr, alice_id = _register(client, "alice")
    assert client.get("/api/feeds", headers=alice_hdr).status_code == 200
    client.post(f"/api/admin/users/{alice_id}/disable", json={"disabled": True}, headers=boss_hdr)
    # Same token, now suspended.
    assert client.get("/api/feeds", headers=alice_hdr).status_code == 403
    assert client.get("/api/meta", headers=alice_hdr).status_code == 403
    # Reinstating restores the same session.
    client.post(f"/api/admin/users/{alice_id}/disable", json={"disabled": False}, headers=boss_hdr)
    assert client.get("/api/feeds", headers=alice_hdr).status_code == 200


def test_deleted_account_token_stops_working(client, admin_env):
    boss_hdr, _ = _register(client, "boss")
    alice_hdr, alice_id = _register(client, "alice")
    assert client.get("/api/feeds", headers=alice_hdr).status_code == 200
    client.delete(f"/api/admin/users/{alice_id}", headers=boss_hdr)
    assert client.get("/api/feeds", headers=alice_hdr).status_code == 401


def test_cannot_suspend_self_or_configured_admin(client, admin_env):
    boss_hdr, boss_id = _register(client, "boss")
    assert client.post(f"/api/admin/users/{boss_id}/disable", json={"disabled": True},
                       headers=boss_hdr).status_code == 400


def test_reset_password(client, admin_env):
    boss_hdr, _ = _register(client, "boss")
    _, alice_id = _register(client, "alice")
    assert client.post(f"/api/admin/users/{alice_id}/reset-password",
                       json={"password": "brandnew99"}, headers=boss_hdr).status_code == 200
    assert client.post("/api/auth/login",
                       json={"username": "alice", "password": "password123"}).status_code == 401
    assert client.post("/api/auth/login",
                       json={"username": "alice", "password": "brandnew99"}).status_code == 200


def test_reset_password_too_short(client, admin_env):
    boss_hdr, _ = _register(client, "boss")
    _, alice_id = _register(client, "alice")
    assert client.post(f"/api/admin/users/{alice_id}/reset-password",
                       json={"password": "short"}, headers=boss_hdr).status_code == 422


def test_delete_user_cascades(client, admin_env):
    boss_hdr, _ = _register(client, "boss")
    alice_hdr, alice_id = _register(client, "alice")
    client.post("/api/feeds", json={"name": "Alice feed"}, headers=alice_hdr)
    client.post("/api/alerts", json={"name": "Alice alert"}, headers=alice_hdr)
    acct = f"acct:{alice_id}"
    with SessionLocal() as s:
        assert s.query(Feed).filter_by(user_id=acct).count() == 1
        assert s.query(Alert).filter_by(user_id=acct).count() == 1
    assert client.delete(f"/api/admin/users/{alice_id}", headers=boss_hdr).status_code == 200
    with SessionLocal() as s:
        assert s.get(User, alice_id) is None
        assert s.query(Feed).filter_by(user_id=acct).count() == 0
        assert s.query(Alert).filter_by(user_id=acct).count() == 0


def test_cannot_delete_self_or_configured_admin(client, admin_env):
    boss_hdr, boss_id = _register(client, "boss")
    assert client.delete(f"/api/admin/users/{boss_id}", headers=boss_hdr).status_code == 400
