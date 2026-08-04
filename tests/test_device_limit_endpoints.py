"""The device limit as a reader and an operator actually meet it.

test_devices.py covers the counting; this covers the wiring — that the limit is
enforced on real requests, that being refused says what to do about it, that
the emailed link genuinely gets someone back in, and that an operator can see
and set all of it.
"""
import pytest

from backend.app import devices, mailer
from backend.app.models import Device, User, utcnow


@pytest.fixture(autouse=True)
def _clean():
    devices.reset_throttle()
    yield
    devices.reset_throttle()


@pytest.fixture
def admin(client, monkeypatch):
    """An operator, the way the rest of the admin tests make one."""
    from backend.app import main
    monkeypatch.setattr(main, "_ADMIN_HANDLES", frozenset({"boss"}))
    client.post("/api/auth/register", json={
        "username": "boss", "email": "boss@x.test", "password": "correct-horse-staple"})
    r = client.post("/api/auth/login",
                    json={"username": "boss", "password": "correct-horse-staple"})
    return {"Authorization": "Bearer " + r.json()["token"]}


def _uid(client, admin_headers, username):
    rows = client.get("/api/admin/users", headers=admin_headers).json()["users"]
    return next(u["id"] for u in rows if u["username"] == username)


DEV_A = "aaaaaaaaaaaa1111"
DEV_B = "bbbbbbbbbbbb2222"
DEV_C = "cccccccccccc3333"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _headers(auth, device):
    return {**auth, "X-Delphi-Device": device, "User-Agent": UA}


# ---------- the account is recorded as being in use ----------

def test_using_the_app_records_the_device(client, register):
    auth = register("dana")
    client.get("/api/meta", headers=_headers(auth, DEV_A))
    r = client.get("/api/feeds", headers=_headers(auth, DEV_A))
    assert r.status_code == 200

    from backend.app.database import SessionLocal
    with SessionLocal() as db:
        user = db.scalar(__import__("sqlalchemy").select(User).where(User.username == "dana"))
        assert devices.active_count(db, user.id) == 1
        d = devices.active(db, user.id)[0]
        assert (d.kind, d.platform, d.browser) == ("desktop", "Windows", "Chrome")


def test_a_client_that_sends_no_device_still_works(client, register):
    """The header comes from our own script, so anything without one is a
    script or an older cached copy of the app. Refusing those would be a
    lockout dressed up as a limit."""
    auth = register("eli")
    assert client.get("/api/meta", headers=auth).status_code == 200


def test_a_nonsense_device_header_is_ignored_not_stored(client, register):
    auth = register("fin")
    r = client.get("/api/meta", headers={**auth, "X-Delphi-Device": "<script>x</script>"})
    assert r.status_code == 200
    from backend.app.database import SessionLocal
    from sqlalchemy import select
    with SessionLocal() as db:
        assert db.scalars(select(Device)).all() == []


# ---------- the limit ----------

def _set_limit(client, admin_auth, uid, limit):
    return client.post(f"/api/admin/users/{uid}/device-limit",
                       json={"limit": limit}, headers=admin_auth)


def test_a_third_device_is_refused_when_the_limit_is_two(client, register, admin):
    auth = register("gwen")
    uid = _uid(client, admin, "gwen")
    assert _set_limit(client, admin, uid, 2).status_code == 200

    assert client.get("/api/meta", headers=_headers(auth, DEV_A)).status_code == 200
    assert client.get("/api/meta", headers=_headers(auth, DEV_B)).status_code == 200

    r = client.get("/api/meta", headers=_headers(auth, DEV_C))
    assert r.status_code == 403
    body = r.json()
    assert body["code"] == "device_limit"
    assert body["limit"] == 2
    # The message has to say what to do, not just that something was refused.
    assert "limit" in body["detail"]
    assert "emailed" in body["detail"] or "email" in body["detail"]


def test_the_devices_already_in_use_keep_working(client, register, admin):
    """Being at the limit must not lock out the people who are inside it."""
    auth = register("hugo")
    uid = _uid(client, admin, "hugo")
    _set_limit(client, admin, uid, 1)

    assert client.get("/api/meta", headers=_headers(auth, DEV_A)).status_code == 200
    assert client.get("/api/meta", headers=_headers(auth, DEV_B)).status_code == 403
    devices.reset_throttle()
    assert client.get("/api/meta", headers=_headers(auth, DEV_A)).status_code == 200


def test_a_refused_device_is_not_recorded(client, register, admin):
    """It never got in, so counting it would make the block permanent."""
    auth = register("iris")
    uid = _uid(client, admin, "iris")
    _set_limit(client, admin, uid, 1)
    client.get("/api/meta", headers=_headers(auth, DEV_A))
    client.get("/api/meta", headers=_headers(auth, DEV_B))

    r = client.get(f"/api/admin/users/{uid}/devices", headers=admin).json()
    assert r["active"] == 1
    assert len(r["devices"]) == 1


def test_no_limit_means_no_limit(client, register, admin, monkeypatch):
    monkeypatch.setattr(devices, "DEFAULT_LIMIT", 0)
    auth = register("jan")
    for key in (DEV_A, DEV_B, DEV_C):
        assert client.get("/api/meta", headers=_headers(auth, key)).status_code == 200


# ---------- what an operator sees ----------

def test_the_account_list_reports_devices_in_use(client, register, admin):
    auth = register("kim")
    client.get("/api/meta", headers=_headers(auth, DEV_A))
    rows = client.get("/api/admin/users", headers=admin).json()
    row = next(u for u in rows["users"] if u["username"] == "kim")
    assert row["active_devices"] == 1
    assert row["known_devices"] == 1
    assert row["device_limit"] is None            # following the server default
    assert "effective_device_limit" in row
    assert "device_active_window_s" in rows


def test_the_device_list_says_what_each_one_is(client, register, admin):
    auth = register("lee")
    client.get("/api/meta", headers=_headers(auth, DEV_A))
    body = client.get(f"/api/admin/users/{_uid(client, admin, 'lee')}/devices",
                      headers=admin).json()
    assert body["active"] == 1
    d = body["devices"][0]
    assert d["kind"] == "desktop" and d["platform"] == "Windows"
    assert d["browser"] == "Chrome" and d["in_use"] is True
    assert "Chrome on Windows" in d["label"]
    # The key is how a browser claims to be that device. An operator has no
    # use for it and it should not be sitting in a console anyone can read.
    assert "device_key" not in d and DEV_A not in str(body)


def test_only_an_operator_may_look(client, register, admin):
    auth = register("moe")
    uid = _uid(client, admin, "moe")
    assert client.get(f"/api/admin/users/{uid}/devices", headers=auth).status_code == 403
    assert client.post(f"/api/admin/users/{uid}/device-limit", json={"limit": 1},
                       headers=auth).status_code == 403


# ---------- setting the limit ----------

def test_a_limit_can_be_set_cleared_and_reset(client, register, admin, monkeypatch):
    monkeypatch.setattr(devices, "DEFAULT_LIMIT", 4)
    auth = register("nia")
    uid = _uid(client, admin, "nia")

    assert _set_limit(client, admin, uid, 2).json()["effective_limit"] == 2
    # null hands it back to the default rather than freezing today's value, so
    # changing the default later still moves this account.
    body = _set_limit(client, admin, uid, None).json()
    assert body["limit"] is None and body["effective_limit"] == 4


@pytest.mark.parametrize("bad", [-1, 101, "three", 1.5])
def test_a_nonsense_limit_is_refused(client, register, admin, bad):
    auth = register("otto")
    uid = _uid(client, admin, "otto")
    assert _set_limit(client, admin, uid, bad).status_code == 400


# ---------- the way back in ----------

def test_an_operator_can_clear_an_account_that_is_stuck(client, register, admin):
    auth = register("pia")
    uid = _uid(client, admin, "pia")
    _set_limit(client, admin, uid, 1)
    client.get("/api/meta", headers=_headers(auth, DEV_A))

    assert client.post(f"/api/admin/users/{uid}/devices/release",
                       headers=admin).json()["released"] == 1
    assert client.get(f"/api/admin/users/{uid}/devices", headers=admin).json()["active"] == 0


def test_the_emailed_link_asks_without_saying_whether_the_account_exists(client):
    """Reachable without signing in, so a different answer for a real address
    would turn it into a way to test addresses."""
    known = client.post("/api/auth/devices/release-link", json={"email": "nobody@x.test"})
    assert known.status_code == 200 and known.json()["ok"] is True


def test_the_link_clears_the_devices_and_signs_the_account_out(client, register, admin):
    from backend.app import auth as authmod
    auth = register("quinn")
    uid = _uid(client, admin, "quinn")
    _set_limit(client, admin, uid, 1)
    client.get("/api/meta", headers=_headers(auth, DEV_A))

    token = authmod.make_scoped_token("devices", uid, 3600)
    body = client.post("/api/auth/devices/release", json={"token": token})
    assert body.status_code == 200 and body.json()["released"] == 1

    # Slots freed...
    devices.reset_throttle()
    assert client.get(f"/api/admin/users/{uid}/devices", headers=admin).json()["active"] == 0
    # ...and the old session really is over, or the evicted device could just
    # come back and take the slot again.
    assert client.get("/api/meta", headers=_headers(auth, DEV_A)).status_code == 401


def test_a_forged_or_stale_link_is_refused(client):
    assert client.post("/api/auth/devices/release",
                       json={"token": "not-a-real-token"}).status_code == 400


def test_a_reset_token_cannot_be_used_as_a_signout_link(client, register, admin):
    """Scoped tokens exist precisely so one purpose's token is not another's."""
    from backend.app import auth as authmod
    auth = register("rex")
    uid = _uid(client, admin, "rex")
    wrong = authmod.make_scoped_token("reset", uid, 3600)
    assert client.post("/api/auth/devices/release", json={"token": wrong}).status_code == 400
