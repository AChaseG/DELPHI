"""Tiny in-process rate limiter for abuse-prone auth endpoints.

Delphi runs as a single uvicorn process over SQLite, so a per-process
sliding-window counter is sufficient — no external store needed. Keys are
(bucket, client-ip); each bucket has its own limit and window. When the
limit is exceeded the caller raises HTTP 429.

Disable entirely with NEWS_RATE_LIMIT=0 (useful for tests).
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

ENABLED = os.environ.get("NEWS_RATE_LIMIT", "1") != "0"

# bucket -> (max_events, window_seconds)
_LIMITS: dict[str, tuple[int, int]] = {
    "login": (10, 300),      # 10 sign-in attempts / 5 min / IP
    "register": (5, 3600),   # 5 new accounts / hour / IP
    "forgot": (5, 900),      # 5 reset emails / 15 min / IP
    "reset": (10, 900),      # 10 reset submissions / 15 min / IP
    "resend": (5, 900),      # 5 verification resends / 15 min / IP
    # Address lookups spend somebody else's quota (OpenStreetMap's), so one
    # reader typing quickly must not be able to spend all of it.
    "geocode": (60, 60),     # 60 address lookups / min / IP
    # A manual poll asks the server to go and talk to every due source. One
    # cycle runs at a time, so this cannot pile up — but it can be held
    # permanently busy, which starves the scheduled polling behind it.
    "ingest": (6, 300),      # 6 manual polls / 5 min / IP
}

_hits: dict[tuple[str, str], deque] = defaultdict(deque)
_lock = threading.Lock()


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()  # first hop = original client
    return request.client.host if request.client else "unknown"


def check(bucket: str, request: Request) -> None:
    """Record one event for (bucket, caller IP); raise 429 past the limit."""
    if not ENABLED:
        return
    limit, window = _LIMITS[bucket]
    key = (bucket, client_ip(request))
    now = time.monotonic()
    with _lock:
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
