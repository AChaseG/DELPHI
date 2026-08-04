"""How many devices an account is *in use on*, what they are, and a cap on it.

The distinction the whole feature turns on is "in use" against "signed in". A
session token here lasts thirty days, so counting valid tokens would report a
phone used once last month as a device the account is on — which is not what
anybody means by "how many devices is this account being used on at the same
time". What is counted is recent traffic, per device rather than per session or
per connection, because one laptop with four tabs open is one laptop.
"""
import json
import time
from datetime import timedelta

import pytest

from backend.app import devices, main
from backend.app.models import Device, User, utcnow


@pytest.fixture(autouse=True)
def _clean():
    devices.reset_throttle()
    yield
    devices.reset_throttle()


UA_IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
UA_WINDOWS = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
UA_IPAD = ("Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
           "(KHTML, like Gecko) Version/17.0 Safari/604.1")
UA_ANDROID = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36")


# ---------- what kind of device is this ----------

@pytest.mark.parametrize("ua,kind,platform,browser", [
    (UA_IPHONE, "mobile", "iOS", "Safari"),
    (UA_WINDOWS, "desktop", "Windows", "Chrome"),
    (UA_IPAD, "tablet", "iPadOS", "Safari"),
    (UA_ANDROID, "mobile", "Android", "Chrome"),
])
def test_it_recognises_the_common_devices(ua, kind, platform, browser):
    assert devices.classify(ua) == (kind, platform, browser)


def test_an_android_tablet_is_not_a_phone():
    """The only thing separating them in the string is the word Mobile."""
    tablet = UA_ANDROID.replace(" Mobile", "")
    assert devices.classify(tablet)[0] == "tablet"
    assert devices.classify(UA_ANDROID)[0] == "mobile"


def test_edge_is_not_reported_as_chrome():
    """Every browser claims to be several others; the order of the checks is
    the whole of what makes this right."""
    edge = UA_WINDOWS + " Edg/124.0.0.0"
    assert devices.classify(edge)[2] == "Edge"


def test_something_unrecognised_says_so_rather_than_guessing():
    assert devices.classify("curl/8.4.0") == ("unknown", "", "")
    assert devices.classify("") == ("unknown", "", "")


def test_a_device_reads_as_a_sentence(db):
    d = Device(user_id=1, device_key="k" * 12, kind="mobile",
               platform="iOS", browser="Safari")
    assert devices.describe(d) == "Safari on iOS (mobile)"


# ---------- in use, not signed in ----------

def _seen(db, user_id, key, ago_s=0, ua=UA_WINDOWS):
    kind, platform, browser = devices.classify(ua)
    d = Device(user_id=user_id, device_key=key, kind=kind, platform=platform,
               browser=browser, user_agent=ua,
               last_seen_at=utcnow() - timedelta(seconds=ago_s))
    db.add(d)
    db.commit()
    return d


def test_a_device_quiet_for_a_while_stops_counting(db):
    _seen(db, 1, "recent000001", ago_s=5)
    _seen(db, 1, "stale0000001", ago_s=devices.ACTIVE_WINDOW_S + 60)
    assert devices.active_count(db, 1) == 1
    assert [d.device_key for d in devices.active(db, 1)] == ["recent000001"]


def test_a_stale_device_is_still_listed_as_known(db):
    """The count answers "how many now"; the list answers "what are they",
    and a laptop that was used yesterday is worth showing as a laptop."""
    _seen(db, 1, "stale0000001", ago_s=devices.ACTIVE_WINDOW_S + 60)
    assert devices.active_count(db, 1) == 0
    rows = devices.all_devices(db, 1)
    assert len(rows) == 1 and not devices.is_active(rows[0])


def test_devices_are_counted_per_account(db):
    _seen(db, 1, "aaaaaaaaaaaa")
    _seen(db, 2, "bbbbbbbbbbbb")
    _seen(db, 2, "cccccccccccc")
    assert devices.active_count(db, 1) == 1
    assert devices.active_count(db, 2) == 2


def test_the_same_browser_asking_twice_is_one_device(db):
    devices.touch(db, 1, "samekey00001", UA_WINDOWS)
    devices.reset_throttle()          # pretend enough time passed
    devices.touch(db, 1, "samekey00001", UA_WINDOWS)
    assert devices.active_count(db, 1) == 1


# ---------- the write throttle ----------

def test_a_busy_tab_does_not_write_on_every_request(db):
    """It runs in the middleware on every API call. Writing each time is the
    per-request database cost that has taken this machine down before."""
    devices.touch(db, 1, "chatty000001", UA_WINDOWS)
    first = devices.active(db, 1)[0].last_seen_at
    for _ in range(50):
        devices.touch(db, 1, "chatty000001", UA_WINDOWS)
    assert devices.active(db, 1)[0].last_seen_at == first


def test_the_throttle_is_well_inside_the_activity_window(db):
    """If it were not, a device could be written less often than it is judged
    on and drop out of the count while someone was using it."""
    assert devices.TOUCH_EVERY_S < devices.ACTIVE_WINDOW_S / 2


# ---------- the limit ----------

def _user(db, name, limit=None):
    u = User(username=name, email=f"{name}@x.test", password_hash="x",
             device_limit=limit)
    db.add(u)
    db.commit()
    return u


def test_no_limit_by_default(db, monkeypatch):
    """Switching a cap on for everybody during an upgrade would lock people
    out of their own accounts without anyone asking for it."""
    monkeypatch.setattr(devices, "DEFAULT_LIMIT", 0)
    u = _user(db, "amy")
    assert devices.limit_for(u) == 0
    for i in range(5):
        _seen(db, u.id, f"key{i:09d}")
    assert devices.would_exceed(db, u, "brandnew0001") == 0


def test_an_account_follows_the_server_default(db, monkeypatch):
    monkeypatch.setattr(devices, "DEFAULT_LIMIT", 2)
    u = _user(db, "ben", limit=None)
    assert devices.limit_for(u) == 2


def test_an_account_can_override_the_default(db, monkeypatch):
    monkeypatch.setattr(devices, "DEFAULT_LIMIT", 2)
    assert devices.limit_for(_user(db, "cal", limit=5)) == 5
    assert devices.limit_for(_user(db, "dan", limit=0)) == 0, "0 means unlimited"


def test_a_further_device_is_refused_at_the_limit(db, monkeypatch):
    monkeypatch.setattr(devices, "DEFAULT_LIMIT", 2)
    u = _user(db, "eve")
    _seen(db, u.id, "first0000001")
    _seen(db, u.id, "second000001")
    assert devices.would_exceed(db, u, "third0000001") == 2


def test_a_device_already_in_use_is_never_refused(db, monkeypatch):
    """The cap is on how many places the account is open in, not on how many
    requests each may make. Re-checking an established device would throw out
    whichever one happened to ask last."""
    monkeypatch.setattr(devices, "DEFAULT_LIMIT", 2)
    u = _user(db, "fay")
    _seen(db, u.id, "first0000001")
    _seen(db, u.id, "second000001")
    assert devices.would_exceed(db, u, "second000001") == 0


def test_a_slot_frees_when_a_device_goes_quiet(db, monkeypatch):
    monkeypatch.setattr(devices, "DEFAULT_LIMIT", 1)
    u = _user(db, "gus")
    _seen(db, u.id, "old000000001", ago_s=devices.ACTIVE_WINDOW_S + 60)
    assert devices.would_exceed(db, u, "new000000001") == 0


# ---------- the way back in ----------

def test_releasing_clears_the_devices_and_ends_the_sessions(db, monkeypatch):
    """Both halves. Clearing the rows alone frees the slots while the old
    tokens still work, so a device that was pushed out could come straight
    back and take the slot again."""
    monkeypatch.setattr(devices, "DEFAULT_LIMIT", 2)
    u = _user(db, "hal")
    before = u.token_version or 0
    _seen(db, u.id, "first0000001")
    _seen(db, u.id, "second000001")

    released = devices.release_all(db, u)

    assert released == 2
    assert devices.active_count(db, u.id) == 0
    assert u.token_version == before + 1
    assert devices.would_exceed(db, u, "third0000001") == 0


def test_releasing_an_account_with_nothing_to_release(db):
    u = _user(db, "ivy")
    assert devices.release_all(db, u) == 0


# ---------- what may be presented as a device key ----------

@pytest.mark.parametrize("bad", [
    "", "short", "has spaces here", "../../etc/passwd", "<script>alert(1)</script>",
    "a" * 65, "semi;colon;;;;", "key\nwith\nnewlines",
])
def test_a_key_that_is_not_an_opaque_identifier_is_refused(bad):
    assert not devices.valid_key(bad)


@pytest.mark.parametrize("good", ["abcd1234", "A-Za-z0-9_" + "x" * 4, "f" * 64])
def test_a_plausible_key_is_accepted(good):
    assert devices.valid_key(good)
