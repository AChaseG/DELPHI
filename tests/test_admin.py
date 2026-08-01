"""Operator console: NEWS_ADMIN_USERS designation, access control, and the
account-management endpoints (list / verify / promote / suspend / reset /
delete) with their self-lockout and last-operator guards."""
import pytest

from backend.app import main
from backend.app.database import SessionLocal
from backend.app.models import Alert, Feed, User


def _register(client, username, password="correct-horse-staple"):
    r = client.post("/api/auth/register", json={
        "username": username, "email": f"{username}@example.com", "password": password})
    assert r.status_code == 201, r.text
    body = r.json()
    return {"Authorization": "Bearer " + body["token"]}, int(body["user_key"].split(":")[1])


def _hdr_for(client, uid_username):
    r = client.post("/api/auth/login", json={"username": uid_username, "password": "correct-horse-staple"})
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
        "username": "alice", "email": "alice@example.com", "password": "correct-horse-staple"})
    assert r.status_code == 201 and r.json().get("verification_sent") is True
    assert client.post("/api/auth/login",
                       json={"username": "alice", "password": "correct-horse-staple"}).status_code == 403

    # The designated operator gets straight in and can reach the console.
    r = client.post("/api/auth/register", json={
        "username": "boss", "email": "boss@example.com", "password": "correct-horse-staple"})
    assert r.status_code == 201, r.text
    login = client.post("/api/auth/login", json={"username": "boss", "password": "correct-horse-staple"})
    assert login.status_code == 200, login.text
    hdr = {"Authorization": "Bearer " + login.json()["token"]}
    assert client.get("/api/admin/users", headers=hdr).status_code == 200

    # ...and can force-verify the account that email left stranded.
    alice_id = [u["id"] for u in client.get("/api/admin/users", headers=hdr).json()["users"]
                if u["username"] == "alice"][0]
    assert client.post(f"/api/admin/users/{alice_id}/verify", headers=hdr).status_code == 200
    assert client.post("/api/auth/login",
                       json={"username": "alice", "password": "correct-horse-staple"}).status_code == 200


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
    r = client.post("/api/auth/login", json={"username": "alice", "password": "correct-horse-staple"})
    assert r.status_code == 403
    assert client.post(f"/api/admin/users/{alice_id}/disable", json={"disabled": False},
                       headers=boss_hdr).status_code == 200
    assert client.post("/api/auth/login",
                       json={"username": "alice", "password": "correct-horse-staple"}).status_code == 200


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
                       json={"username": "alice", "password": "correct-horse-staple"}).status_code == 401
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


# ---------- Pantheon succession when an owner's account is deleted ----------

def _make_pantheon(client, hdr, name="Watch Desk"):
    r = client.post("/api/pantheons", json={"name": name}, headers=hdr)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _join(client, owner_hdr, pantheon_id, member_hdr, member_name):
    """Invite member_name and have them accept."""
    r = client.post(f"/api/pantheons/{pantheon_id}/invite",
                    json={"user": member_name}, headers=owner_hdr)
    assert r.status_code in (200, 201), r.text
    invites = client.get("/api/pantheons", headers=member_hdr).json()["invites"]
    inv = next(i for i in invites if i["pantheon_id"] == pantheon_id)
    assert client.post(f"/api/pantheons/invites/{inv['id']}/accept",
                       headers=member_hdr).status_code == 200


def test_deleting_owner_transfers_pantheon_to_admin(client, admin_env):
    """An existing admin inherits ahead of plain members."""
    boss_hdr, _ = _register(client, "boss")
    owner_hdr, owner_id = _register(client, "owner")
    mem_hdr, mem_id = _register(client, "member")
    adm_hdr, adm_id = _register(client, "deputy")
    pid = _make_pantheon(client, owner_hdr)
    _join(client, owner_hdr, pid, mem_hdr, "member")     # joins first
    _join(client, owner_hdr, pid, adm_hdr, "deputy")     # joins later, promoted
    assert client.post(f"/api/pantheons/{pid}/members/{adm_id}/role",
                       json={"role": "admin"}, headers=owner_hdr).status_code == 200

    assert client.delete(f"/api/admin/users/{owner_id}", headers=boss_hdr).status_code == 200

    detail = client.get(f"/api/pantheons/{pid}", headers=adm_hdr)
    assert detail.status_code == 200, "the Pantheon must survive its owner's deletion"
    body = detail.json()
    assert body["owner_name"] == "deputy"          # admin inherited, not the earlier joiner
    assert body["role"] == "owner"
    assert {m["username"] for m in body["members"]} == {"member", "deputy"}


def test_deleting_owner_falls_back_to_longest_standing_member(client, admin_env):
    boss_hdr, _ = _register(client, "boss")
    owner_hdr, owner_id = _register(client, "owner")
    first_hdr, _ = _register(client, "first")
    second_hdr, _ = _register(client, "second")
    pid = _make_pantheon(client, owner_hdr)
    _join(client, owner_hdr, pid, first_hdr, "first")
    _join(client, owner_hdr, pid, second_hdr, "second")

    client.delete(f"/api/admin/users/{owner_id}", headers=boss_hdr)

    body = client.get(f"/api/pantheons/{pid}", headers=first_hdr).json()
    assert body["owner_name"] == "first"


def test_shared_content_transfers_with_the_pantheon(client, admin_env):
    """Feeds and alerts the departing owner shared stay on the group's board,
    reassigned to the heir — and an inherited alert stops emailing them."""
    boss_hdr, _ = _register(client, "boss")
    owner_hdr, owner_id = _register(client, "owner")
    heir_hdr, heir_id = _register(client, "heir")
    pid = _make_pantheon(client, owner_hdr)
    _join(client, owner_hdr, pid, heir_hdr, "heir")

    feed_id = client.post("/api/feeds", json={"name": "Shared feed"},
                          headers=owner_hdr).json()["id"]
    alert_id = client.post("/api/alerts", json={"name": "Shared alert", "notify_email": True},
                           headers=owner_hdr).json()["id"]
    assert client.post(f"/api/feeds/{feed_id}/share", json={"pantheon_id": pid},
                       headers=owner_hdr).status_code in (200, 201)
    assert client.post(f"/api/alerts/{alert_id}/share", json={"pantheon_id": pid},
                       headers=owner_hdr).status_code in (200, 201)

    client.delete(f"/api/admin/users/{owner_id}", headers=boss_hdr)

    shared = client.get(f"/api/pantheons/{pid}/feeds", headers=heir_hdr)
    assert shared.status_code == 200
    assert any(f["name"] == "Shared feed" for f in shared.json()), \
        "shared feed should survive and stay on the Pantheon board"
    with SessionLocal() as s:
        acct = f"acct:{heir_id}"
        feeds = s.query(Feed).filter_by(pantheon_id=pid).all()
        alerts = s.query(Alert).filter_by(pantheon_id=pid).all()
        assert feeds and all(f.user_id == acct for f in feeds)
        assert alerts and all(a.user_id == acct for a in alerts)
        # inherited delivery settings must not follow the alert to a new owner
        assert all(a.notify_email is False and a.webhook_url == "" for a in alerts)


def test_solo_owner_deletion_closes_the_pantheon(client, admin_env):
    boss_hdr, _ = _register(client, "boss")
    owner_hdr, owner_id = _register(client, "owner")
    pid = _make_pantheon(client, owner_hdr, "Solo Desk")
    feed_id = client.post("/api/feeds", json={"name": "Solo feed"},
                          headers=owner_hdr).json()["id"]
    client.post(f"/api/feeds/{feed_id}/share", json={"pantheon_id": pid}, headers=owner_hdr)

    client.delete(f"/api/admin/users/{owner_id}", headers=boss_hdr)

    from backend.app.models import Pantheon, PantheonMember
    with SessionLocal() as s:
        assert s.get(Pantheon, pid) is None
        assert s.query(PantheonMember).filter_by(pantheon_id=pid).count() == 0
        assert s.query(Feed).filter_by(pantheon_id=pid).count() == 0


def test_personal_content_is_still_deleted(client, admin_env):
    """Only shared content is preserved; private feeds/alerts go with the user."""
    boss_hdr, _ = _register(client, "boss")
    owner_hdr, owner_id = _register(client, "owner")
    heir_hdr, _ = _register(client, "heir")
    pid = _make_pantheon(client, owner_hdr)
    _join(client, owner_hdr, pid, heir_hdr, "heir")
    client.post("/api/feeds", json={"name": "Private feed"}, headers=owner_hdr)
    client.post("/api/alerts", json={"name": "Private alert"}, headers=owner_hdr)

    client.delete(f"/api/admin/users/{owner_id}", headers=boss_hdr)

    acct = f"acct:{owner_id}"
    with SessionLocal() as s:
        assert s.query(Feed).filter_by(user_id=acct).count() == 0
        assert s.query(Alert).filter_by(user_id=acct).count() == 0
        assert s.get(User, owner_id) is None


# ---------- shared feeds must survive normal Pantheon activity ----------

def test_shared_feeds_survive_membership_changes(client, admin_env):
    """Reported as 'Pantheons wipe feeds shared to them'. Sharing must copy,
    not move, and nothing short of deleting the Pantheon may remove the copy."""
    boss_hdr, _ = _register(client, "boss")
    owner_hdr, _ = _register(client, "owner")
    mem_hdr, mem_id = _register(client, "member")
    pid = _make_pantheon(client, owner_hdr, "Shared Desk")
    _join(client, owner_hdr, pid, mem_hdr, "member")

    own_feed = client.post("/api/feeds", json={"name": "Wires"}, headers=owner_hdr).json()["id"]
    assert client.post(f"/api/feeds/{own_feed}/share", json={"pantheon_id": pid},
                       headers=owner_hdr).status_code in (200, 201)

    def shared_names(hdr):
        r = client.get(f"/api/pantheons/{pid}/feeds", headers=hdr)
        assert r.status_code == 200, r.text
        return [f["name"] for f in r.json()]

    # Visible to both, and the sharer keeps their private original.
    assert shared_names(owner_hdr) == ["Wires"]
    assert shared_names(mem_hdr) == ["Wires"]
    assert [f["name"] for f in client.get("/api/feeds", headers=owner_hdr).json()] == ["Wires"]

    # A member can actually read the shared feed's articles (not just see it).
    shared_id = client.get(f"/api/pantheons/{pid}/feeds", headers=mem_hdr).json()[0]["id"]
    assert client.get(f"/api/feeds/{shared_id}/articles", headers=mem_hdr).status_code == 200

    # Routine activity must not remove it.
    client.post(f"/api/pantheons/{pid}/members/{mem_id}/role",
                json={"role": "admin"}, headers=owner_hdr)
    assert shared_names(owner_hdr) == ["Wires"]
    client.post(f"/api/pantheons/{pid}/members/{mem_id}/role",
                json={"role": "member"}, headers=owner_hdr)
    client.patch(f"/api/pantheons/{pid}", json={"name": "Renamed Desk"}, headers=owner_hdr)
    assert shared_names(owner_hdr) == ["Wires"]

    # A member leaving keeps everyone else's shared content.
    client.post(f"/api/pantheons/{pid}/leave", headers=mem_hdr)
    assert shared_names(owner_hdr) == ["Wires"]


def test_sharing_the_same_feed_twice_is_refused_not_duplicated(client, admin_env):
    _register(client, "boss")
    owner_hdr, _ = _register(client, "owner")
    pid = _make_pantheon(client, owner_hdr, "Desk")
    fid = client.post("/api/feeds", json={"name": "Wires"}, headers=owner_hdr).json()["id"]
    client.post(f"/api/feeds/{fid}/share", json={"pantheon_id": pid}, headers=owner_hdr)
    again = client.post(f"/api/feeds/{fid}/share", json={"pantheon_id": pid}, headers=owner_hdr)
    assert again.status_code == 409
    assert len(client.get(f"/api/pantheons/{pid}/feeds", headers=owner_hdr).json()) == 1
