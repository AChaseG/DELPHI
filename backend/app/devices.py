"""Which devices an account is being used on right now, and how many it may be.

Three things an operator asked for and the system could not answer: how many
devices an account is *actively* in use on, what kinds of devices those are,
and a cap on the first.

The distinction that shapes all of it is "in use" versus "signed in". A session
token here lives for thirty days, so counting valid tokens would report a phone
somebody used once last month as a device the account is on. What is counted
instead is recent traffic: a device is active if something arrived from it
inside ACTIVE_WINDOW_S. Close the laptop and it stops counting a few minutes
later, which is what someone means when they say an account is being used in
two places at once.

The count is per device, not per session or per connection, because one laptop
with four tabs open is one device — and a limit that counted tabs would be
unusable. Tabs share the browser's local storage, so they share a device key.
"""
from __future__ import annotations

import os
import re
import time
from datetime import timedelta

from sqlalchemy import delete as sa_delete, func, select

from .models import Device, User, utcnow

# How long after its last request a device stops counting as in use. Long
# enough to span a reader's pause between page loads, short enough that a
# closed laptop frees its slot before it is worth complaining about. The
# dashboard polls every 15s while it is open, so an open tab stays well inside
# this without any keepalive of its own.
ACTIVE_WINDOW_S = float(os.environ.get("NEWS_DEVICE_ACTIVE_WINDOW_S", "300"))

# The cap for accounts with no cap of their own. 0 means no limit, which is the
# default: switching this on for everybody without being asked would lock
# people out of their own accounts on an upgrade.
DEFAULT_LIMIT = int(os.environ.get("NEWS_DEVICE_LIMIT", "0"))

# A device row is touched on every request, and writing to the database that
# often is exactly the sort of per-request cost that has already taken this
# machine down once. `last_seen_at` only needs to be accurate to well inside
# the active window, so a device already seen this recently is left alone.
TOUCH_EVERY_S = float(os.environ.get("NEWS_DEVICE_TOUCH_EVERY_S", "30"))

# (user_id, device_key) -> monotonic time of the last write.
_touched: dict[tuple[int, str], float] = {}

_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def valid_key(key: str) -> bool:
    """A device key is stored and shown to an operator, so it has to be an
    opaque identifier and nothing else — not a name, not markup, not a path."""
    return bool(key and _KEY_RE.match(key))


def classify(user_agent: str) -> tuple[str, str, str]:
    """(kind, platform, browser) from a user-agent string.

    Deliberately coarse. The question is "is this the phone or the desktop",
    which needs three buckets and a recognisable name — not a fingerprint, and
    not a dependency on a browser-database that goes stale. Anything
    unrecognised says so rather than guessing.
    """
    ua = user_agent or ""
    low = ua.lower()

    # Android tablets say "Android" without "Mobile"; Android phones say both.
    # That convention is the only thing distinguishing them in the string.
    if "ipad" in low or ("android" in low and "mobile" not in low):
        kind = "tablet"
    elif ("mobi" in low or "iphone" in low or "ipod" in low
          or "windows phone" in low):
        kind = "mobile"
    elif ("windows" in low or "macintosh" in low or "mac os x" in low
          or "x11" in low or "linux" in low or "cros" in low):
        kind = "desktop"
    else:
        kind = "unknown"

    platform = ""
    for needle, name in (("windows phone", "Windows Phone"), ("windows", "Windows"),
                         ("iphone", "iOS"), ("ipad", "iPadOS"), ("ipod", "iOS"),
                         ("cros", "ChromeOS"), ("android", "Android"),
                         ("mac os x", "macOS"), ("macintosh", "macOS"),
                         ("linux", "Linux")):
        if needle in low:
            platform = name
            break

    browser = ""
    # Order matters: every one of these also claims to be several of the
    # others. Edge says Chrome and Safari, Chrome says Safari.
    for needle, name in (("edg/", "Edge"), ("opr/", "Opera"), ("brave", "Brave"),
                         ("firefox/", "Firefox"), ("chrome/", "Chrome"),
                         ("crios/", "Chrome"), ("fxios/", "Firefox"),
                         ("safari/", "Safari")):
        if needle in low:
            browser = name
            break

    return kind, platform, browser


def describe(device: Device) -> str:
    """A device in one line, for an operator reading a list of them."""
    bits = [b for b in (device.browser, device.platform) if b]
    label = " on ".join(bits) if bits else "Unrecognised browser"
    return f"{label} ({device.kind})" if device.kind != "unknown" else label


def limit_for(user: User) -> int:
    """This account's cap, falling back to the server default. 0 = no limit."""
    if user.device_limit is None:
        return max(0, DEFAULT_LIMIT)
    return max(0, user.device_limit)


def _cutoff():
    return utcnow() - timedelta(seconds=ACTIVE_WINDOW_S)


def active(db, user_id: int) -> list[Device]:
    """Devices this account is in use on right now, most recent first."""
    return list(db.scalars(
        select(Device)
        .where(Device.user_id == user_id, Device.last_seen_at >= _cutoff())
        .order_by(Device.last_seen_at.desc())))


def active_count(db, user_id: int) -> int:
    """How many devices the account is in use on. Counted in the database
    rather than by loading the rows, because the admin list asks this once per
    account and the rows are never looked at."""
    return db.scalar(
        select(func.count(Device.id)).where(Device.user_id == user_id,
                                            Device.last_seen_at >= _cutoff())) or 0


def all_devices(db, user_id: int) -> list[Device]:
    """Every device ever seen for the account, in-use ones first."""
    return list(db.scalars(
        select(Device).where(Device.user_id == user_id)
        .order_by(Device.last_seen_at.desc())))


def is_active(device: Device) -> bool:
    return bool(device.last_seen_at and device.last_seen_at >= _cutoff())


def touch(db, user_id: int, device_key: str, user_agent: str) -> None:
    """Record that this device is in use, cheaply.

    Skipped entirely when the row was written inside TOUCH_EVERY_S, so the
    common case of a dashboard polling every fifteen seconds costs a dictionary
    lookup rather than a write.
    """
    now = time.monotonic()
    seen_at = _touched.get((user_id, device_key))
    if seen_at is not None and now - seen_at < TOUCH_EVERY_S:
        return
    _touched[(user_id, device_key)] = now

    device = db.scalar(select(Device).where(Device.user_id == user_id,
                                            Device.device_key == device_key))
    if device is None:
        kind, platform, browser = classify(user_agent)
        device = Device(user_id=user_id, device_key=device_key, kind=kind,
                        platform=platform, browser=browser,
                        user_agent=(user_agent or "")[:300])
        db.add(device)
    device.last_seen_at = utcnow()
    db.commit()


def would_exceed(db, user: User, device_key: str) -> int:
    """0 if this device may proceed, otherwise the limit it would break.

    A device already counted among the active ones is always allowed through:
    the cap is on how many places an account is open in, not on how many
    requests each may make, and re-checking an established device against the
    cap would drop whichever one happened to ask last.
    """
    limit = limit_for(user)
    if not limit:
        return 0
    in_use = active(db, user.id)
    if any(d.device_key == device_key for d in in_use):
        return 0
    return limit if len(in_use) >= limit else 0


def release_all(db, user: User) -> int:
    """Forget every device for this account and end its sessions.

    Both halves matter. Deleting the rows frees the slots, and bumping
    token_version signs the old devices out for real — otherwise a device that
    had been pushed out could simply come back and take the slot again, and the
    person who asked to be signed out everywhere would not have been.
    """
    keys = list(db.scalars(select(Device.device_key).where(Device.user_id == user.id)))
    db.execute(sa_delete(Device).where(Device.user_id == user.id))
    user.token_version = (user.token_version or 0) + 1
    db.commit()
    for key in keys:
        _touched.pop((user.id, key), None)
    return len(keys)


def forget(user_id: int, device_key: str) -> None:
    """Drop the write-throttle memo for one device (used after deletions)."""
    _touched.pop((user_id, device_key), None)


def reset_throttle() -> None:
    """Tests share a process; the memo must not leak between them."""
    _touched.clear()
