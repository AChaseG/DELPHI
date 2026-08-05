"""A record of the moments this process could not answer.

Every "can't reach the server" so far has been diagnosed after the fact, from
whatever happened to still be in a log buffer that holds about a minute. By
the time anyone looks the machine is healthy again and the evidence has
scrolled away, so each round of this has been reconstruction rather than
reading — and twice I fixed a real cause that turned out not to be the whole
one, because the evidence for the rest was already gone.

This measures the thing that actually fails. A coroutine that asks to be woken
every second, and notices how late it was. If it wanted the loop at 12:00:01
and got it at 12:00:14, then for thirteen seconds nothing else ran either: not
a page, not an API call, not the health check Fly gives up on after ten. That
is precisely the condition the site is unreachable in, measured directly
rather than inferred from a gap between log lines.

The record is kept in memory and served from /api/ingest/status, so the
question "what was it doing at 03:40?" has an answer that outlives the log.
Bounded to the worst few dozen, newest first: an unbounded list of a recurring
fault is its own leak.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque

from .models import utcnow

log = logging.getLogger("watchdog")

# How often the watchdog asks for the loop back.
TICK_S = float(os.environ.get("NEWS_WATCHDOG_TICK_S", "1.0"))

# How late is worth recording. Small delays are ordinary — the loop shares one
# core with everything else — so this sits well above the noise and well below
# the 10s the health check allows, to catch a stall on its way to becoming an
# outage rather than only after it is one.
STALL_S = float(os.environ.get("NEWS_WATCHDOG_STALL_S", "3.0"))

MAX_RECORDS = 40

_stalls: deque[dict] = deque(maxlen=MAX_RECORDS)

# What the poller says it is doing, set as it moves through a cycle. A stall
# is only actionable if it says which phase was running, and the phase timings
# in the log are printed when the cycle *ends* — no use if it never does.
_activity: str = "idle"


def doing(what: str) -> None:
    global _activity
    _activity = what


def activity() -> str:
    return _activity


def stalls() -> list[dict]:
    """Worst-first is wrong here; newest-first is what a question asks for."""
    return list(reversed(_stalls))


def worst() -> float:
    return max((s["late_s"] for s in _stalls), default=0.0)


def clear() -> None:
    _stalls.clear()


def _record(late: float, during: str) -> None:
    _stalls.append({
        "at": utcnow().isoformat() + "Z",
        "late_s": round(late, 1),
        "during": during,
    })
    log.warning("event loop unavailable for %.1fs during %s", late, during)


async def watch() -> None:
    """Ask for the loop every TICK_S and record every time it is late.

    Deliberately the cheapest possible thing to be starved: it does no I/O,
    touches no database and holds nothing, so lateness here can only mean the
    loop itself was unavailable — never that this task was waiting on
    something of its own.
    """
    while True:
        due = time.monotonic() + TICK_S
        try:
            await asyncio.sleep(TICK_S)
        except asyncio.CancelledError:
            raise
        late = time.monotonic() - due
        if late >= STALL_S:
            _record(late, _activity)
