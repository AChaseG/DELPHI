"""Address lookup, for places the bundled gazetteer does not know.

Delphi ships a gazetteer of roughly 480 cities and 154 countries. It is
instant, free and private, and it is enough for "watch Tokyo" — but it cannot
find a street address, a town, a district or a lake, and a reader who types one
gets nothing back. This asks OpenStreetMap's Nominatim for those.

Three things follow from calling somebody else's service:

  * It is the *server* that calls it, never the browser, so the reader's own
    address is never handed to a third party — Nominatim sees this server.
  * Nominatim's usage policy allows roughly one request a second from an
    identified client. Requests are paced to that and answers are cached, so a
    reader typing a street name letter by letter costs a handful of lookups.
  * It is optional. NEWS_GEOCODER=off falls back to the gazetteer alone, and
    NEWS_NOMINATIM_URL points at a self-hosted instance for anyone who would
    rather not involve a third party at all.

A failure here is never an error the reader sees: the gazetteer results stand
on their own and the address results simply do not appear.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

log = logging.getLogger("geocode")

PROVIDER = os.environ.get("NEWS_GEOCODER", "nominatim").strip().lower()
BASE_URL = os.environ.get("NEWS_NOMINATIM_URL", "https://nominatim.openstreetmap.org").rstrip("/")
# Nominatim asks for a real contact in the agent string so it can reach the
# operator of a misbehaving client rather than simply blocking it.
CONTACT = os.environ.get("NEWS_GEOCODER_CONTACT", "https://github.com/AChaseG/DELPHI")
USER_AGENT = f"Delphi/1.0 (news dashboard; +{CONTACT})"
TIMEOUT_S = float(os.environ.get("NEWS_GEOCODER_TIMEOUT", "4"))
MIN_GAP_S = float(os.environ.get("NEWS_GEOCODER_GAP", "1.0"))   # their policy
CACHE_MAX = 512
CACHE_TTL_S = 24 * 3600      # an address does not move

_cache: dict[str, tuple[float, list[dict]]] = {}
_last_call = 0.0
# What happened last time, so an operator can tell "nobody typed an address"
# from "the lookup is being refused" without reading the log.
status: dict = {"lookups": 0, "failures": 0, "last_error": None}
# One lock per event loop rather than one for the module: a lock belongs to the
# loop that first awaited it, and the app's loop is not the only one that runs
# this (tests drive it directly).
_locks: dict[object, asyncio.Lock] = {}


def _pacing_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        for dead in [k for k in _locks if getattr(k, "is_closed", lambda: False)()]:
            _locks.pop(dead, None)
        lock = _locks[loop] = asyncio.Lock()
    return lock


ATTRIBUTION = "© OpenStreetMap contributors"


def enabled() -> bool:
    return PROVIDER not in ("", "off", "none", "0")


def _shorten(item: dict) -> tuple[str, str]:
    """A short label and the full address, from one Nominatim result.

    `display_name` is the whole postal address, which is too long for a
    dropdown row; the first component names the thing itself."""
    display = (item.get("display_name") or "").strip()
    name = (item.get("name") or "").strip() or display.split(",")[0].strip()
    return name, display


async def search(query: str, limit: int = 5) -> list[dict]:
    """Addresses matching `query`. Never raises; an outage returns nothing."""
    q = (query or "").strip()
    if not enabled() or len(q) < 3:
        return []

    key = q.lower()
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_S:
        return hit[1]

    try:
        async with _pacing_lock():             # one request at a time, paced
            global _last_call
            wait = MIN_GAP_S - (time.monotonic() - _last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            _last_call = time.monotonic()
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                resp = await client.get(
                    f"{BASE_URL}/search",
                    params={"q": q, "format": "jsonv2", "limit": str(limit),
                            "addressdetails": "1"},
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                )
            resp.raise_for_status()
            rows = resp.json()
    except Exception as exc:
        status["failures"] += 1
        status["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
        log.info("address lookup failed for %r: %s", q[:60], exc)
        return []

    status["lookups"] += 1
    status["last_error"] = None

    out = []
    for item in rows if isinstance(rows, list) else []:
        try:
            lat, lon = float(item["lat"]), float(item["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        name, display = _shorten(item)
        if not name:
            continue
        out.append({
            "name": name,
            "address": display,
            "lat": lat,
            "lon": lon,
            "country": (item.get("address", {}) or {}).get("country_code", "").upper()[:2].lower(),
            "kind": item.get("addresstype") or item.get("type") or "place",
            "source": "osm",
        })

    _cache[key] = (time.time(), out)
    if len(_cache) > CACHE_MAX:               # bounded: drop the oldest entry
        _cache.pop(next(iter(_cache)))
    return out


def clear_cache() -> None:
    """For tests, and for an operator who has changed provider."""
    _cache.clear()
