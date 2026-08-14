"""Tiny in-process rate limiter for abuse-prone auth endpoints.

Delphi runs as a single uvicorn process over SQLite, so a per-process
sliding-window counter is sufficient — no external store needed. Keys are
(bucket, client-ip); each bucket has its own limit and window. When the
limit is exceeded the caller raises HTTP 429.

Disable entirely with NEWS_RATE_LIMIT=0 (useful for tests).

The whole thing rests on working out *who* is calling, and that is harder than
it looks behind a proxy. See `client_ip` — getting it wrong in the obvious way
does not weaken these limits, it removes them.
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

ENABLED = os.environ.get("NEWS_RATE_LIMIT", "1") != "0"

def _int_env(name: str, default: str) -> int:
    """A setting that decides who gets limited should not fail quietly.

    A typo here silently changes who Delphi believes is calling, so say so at
    startup rather than running on the default and looking fine.
    """
    raw = os.environ.get(name, default)
    try:
        return max(0, int(raw))
    except ValueError:
        raise ValueError(f"{name} must be a whole number, not “{raw}”") from None


# How many proxies of our own sit in front of the app. Each one appends the
# address it heard from to X-Forwarded-For, so this is exactly how many
# trailing entries of that chain are ours to believe.
#
# The default is deployment-aware because both possible defaults are wrong
# somewhere: 0 behind a proxy puts the whole internet in one bucket (a
# self-inflicted outage on sign-in), and 1 with nothing in front makes every
# limit forgeable. Fly sets FLY_APP_NAME in the machine environment, so we can
# tell which situation we are in rather than guessing.
_default_proxies = "1" if os.environ.get("FLY_APP_NAME") else "0"
TRUSTED_PROXIES = _int_env("NEWS_TRUSTED_PROXIES", _default_proxies)

# A header a trusted proxy *overwrites* (rather than appends to), naming the
# real client. Fly sets Fly-Client-IP; other proxies have their own. Off by
# default: it is only safe when the proxy is known to overwrite it, since
# anything a client can set and we believe is a bypass.
CLIENT_IP_HEADER = os.environ.get("NEWS_CLIENT_IP_HEADER", "").strip().lower()

# Ceiling on distinct callers tracked at once, so a flood of addresses cannot
# grow the table without bound. Past it, the least recently active are dropped.
MAX_TRACKED = _int_env("NEWS_RATE_LIMIT_MAX_KEYS", "50000")
_SWEEP_EVERY = 60.0

# bucket -> (max_events, window_seconds)
_LIMITS: dict[str, tuple[int, int]] = {
    "login": (10, 300),      # 10 sign-in attempts / 5 min / IP
    "register": (5, 3600),   # 5 new accounts / hour / IP
    "forgot": (5, 900),      # 5 reset emails / 15 min / IP
    "reset": (10, 900),      # 10 reset submissions / 15 min / IP
    "resend": (5, 900),      # 5 verification resends / 15 min / IP
    # Changing a password asks for the current one, so this endpoint is a place
    # to guess it. Kept off the "login" bucket deliberately: sharing one would
    # mean a few mistyped attempts here also locked the account out of signing
    # in, and the two are reached from different places for different reasons.
    "change_password": (10, 300),   # 10 attempts / 5 min / IP
    # Address lookups spend somebody else's quota (OpenStreetMap's), so one
    # reader typing quickly must not be able to spend all of it.
    "geocode": (60, 60),     # 60 address lookups / min / IP
    # A manual poll asks the server to go and talk to every due source. One
    # cycle runs at a time, so this cannot pile up — but it can be held
    # permanently busy, which starves the scheduled polling behind it.
    "ingest": (6, 300),      # 6 manual polls / 5 min / IP
    # Searching is what the app is for, and a reader types quickly: a board
    # refresh, a query in the rail, and a preview in the builder can all fire
    # within a second of each other. This is set where a person cannot reach it
    # and a script running flat out can — it bounds FTS work, nothing more.
    "search": (150, 60),     # 150 searches / min / IP
    # Export is a different animal, which is why it is not on the search
    # bucket: up to EXPORT_MAX (2000) articles assembled into a file, and with
    # a reading language set, every one of those is put through the translation
    # service — somebody else's quota, and real CPU on a two-vCPU machine.
    # Nobody exports the same column forty times a minute.
    "export": (12, 300),     # 12 exports / 5 min / IP
    # Anything that reaches Stripe, plus redeeming an invitation. Tight because
    # each one is a call to somebody else's API on a worker thread, and because
    # guessing invitation codes is the one thing here worth brute-forcing —
    # 20 tries in five minutes against a 32-character alphabet is nowhere.
    "billing": (20, 300),    # 20 / 5 min / IP
}

_hits: dict[tuple[str, str], deque] = defaultdict(deque)
_lock = threading.Lock()
_last_sweep = time.monotonic()


def client_ip(request: Request) -> str:
    """Who is calling, from the part of the request they cannot write.

    X-Forwarded-For is *appended* to: every proxy adds the address it heard
    from, so the header a request arrives with reads

        <whatever the client made up>, <client's real address>, <inner proxies>

    Reading the first entry — the obvious thing, and what this used to do —
    reads the one field entirely under the caller's control. Sending a
    different value each time then gives every request a fresh bucket, which
    does not weaken these limits so much as delete them.

    So work from the other end. Appending is what makes the chain trustworthy
    in reverse: the last entry was written by the proxy nearest us, the one
    before that by the proxy before it, and so on for as many proxies as we
    actually have. Treat the connection's own peer address as the final link
    (nothing can forge that — it is who opened the socket) and count back
    TRUSTED_PROXIES hops. Everything earlier is the caller's fiction and is
    never read, however much of it they send.
    """
    if CLIENT_IP_HEADER:
        stated = request.headers.get(CLIENT_IP_HEADER, "").strip()
        if stated:
            return stated

    peer = request.client.host if request.client else "unknown"
    if not TRUSTED_PROXIES:
        return peer

    forwarded = [h.strip() for h in
                 request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    chain = forwarded + [peer]
    # chain[-1] is our own proxy, chain[-2] the one in front of it, …; the
    # client is the hop just before the first one we trust. Clamp at 0 for a
    # chain shorter than the configured depth (a direct request, or a
    # misconfigured count) — the earliest real entry is the safest guess.
    return chain[max(0, len(chain) - 1 - TRUSTED_PROXIES)]


def _sweep(now: float) -> None:
    """Drop callers whose window has fully passed. Caller holds the lock.

    Without this the table only ever grows: entries are trimmed inside a key
    but the key itself is never removed, so every address ever seen keeps a
    deque forever.
    """
    global _last_sweep
    _last_sweep = now
    for key in [k for k, ev in _hits.items()
                if not ev or ev[-1] < now - _LIMITS[k[0]][1]]:
        del _hits[key]
    if len(_hits) <= MAX_TRACKED:
        return
    # Still over after sweeping: shed the least recently active. This does
    # forgive whoever is dropped, but filling the table needs MAX_TRACKED
    # distinct real addresses — at which point the attacker has a botnet and
    # per-address limiting was never the defence anyway. Bounded memory is
    # worth more than a limit that holds only until the process is starved.
    for key in sorted(_hits, key=lambda k: _hits[k][-1])[:len(_hits) - MAX_TRACKED]:
        del _hits[key]


def check(bucket: str, request: Request) -> None:
    """Record one event for (bucket, caller IP); raise 429 past the limit."""
    if not ENABLED:
        return
    limit, window = _LIMITS[bucket]
    key = (bucket, client_ip(request))
    now = time.monotonic()
    with _lock:
        if now - _last_sweep > _SWEEP_EVERY or len(_hits) > MAX_TRACKED:
            _sweep(now)
        events = _hits[key]
        cutoff = now - window
        while events and events[0] < cutoff:
            events.popleft()
        if len(events) >= limit:
            retry = max(1, int(events[0] + window - now))
            raise HTTPException(
                429, f"Too many attempts. Try again in about {retry} seconds.",
                headers={"Retry-After": str(retry)})
        events.append(now)
