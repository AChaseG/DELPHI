"""Typhon — live hazards on the map, from the agencies that declare them.

The news tells you a fire happened. This tells you a fire *is happening*, with
a coordinate, and it keeps telling you as it grows. Those are different kinds
of fact and they are stored differently: see the `Hazard` model for why a thing
that gets rewritten every fifteen minutes cannot be an article.

Shape of a poll, and each step is here for a reason:

    fetch     one request per provider, through safefetch, off the loop
    upsert    match on (provider, external_id); work out what actually changed
    prune     drop what the provider has stopped mentioning
    (later)   evaluate watched places against what changed, and deliver

The single most important rule in this file is that **a fetch returning
nothing is a failure, not an empty world**. Providers rate-limit, go down, and
rename fields. If a zero-row response were taken at face value the prune would
empty the table, and the next good poll would re-insert everything as new — and
once proximity alerting exists, notify every watcher about every fire they had
already been told about. Every provider therefore returns None for "I could not
tell you" and a list for "this is what there is", and None never prunes.

Off by default (`NEWS_HAZARDS`). Providers needing a key no-op without one
rather than failing, so an instance that never sets one simply has no layer.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta

import httpx
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import safefetch
from .models import Hazard, utcnow

log = logging.getLogger("hazards")

# How often the loop runs a poll. WFIGS moves on the order of minutes to hours;
# against a 15-second ingest tick this is one HTTP request every sixty ticks,
# which is what "does not compete with the news poll" looks like in practice.
POLL_EVERY_SECONDS = float(os.environ.get("NEWS_HAZARD_EVERY_S", "900"))
RETENTION_DAYS = float(os.environ.get("NEWS_HAZARD_RETENTION_DAYS", "7"))
# A backstop against a provider bug, not a disk-space policy. The real risk is
# subtler than the volume filling: storage.over_ceiling() measures the whole
# database file while prune_to_fit() only ever deletes *articles*, so a hazard
# table left to grow would quietly push news out of the archive instead.
MAX_HAZARDS = int(os.environ.get("NEWS_MAX_HAZARDS", "5000"))
FETCH_TIMEOUT = 30.0

KIND_WILDFIRE = "wildfire"


def enabled() -> bool:
    return os.environ.get("NEWS_HAZARDS", "0") not in ("0", "", "off", "false")


# ---------- WFIGS: active US wildfire incidents ----------
#
# The federal interagency feed, via the NIFC open-data ArcGIS service. No key,
# no quota to manage, and the only source here that produces a thing a person
# would call "a wildfire" — everything else is a detection or a reading.
#
# United States only, and there is no fixing that from this end. The layer says
# so in its own label; GDACS in the source catalog is what makes hazard data
# non-empty for a reader anywhere else.

WFIGS_URL = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/"
             "services/WFIGS_Incident_Locations_Current/FeatureServer/0/query")
WFIGS_FIELDS = (
    "IrwinID,IncidentName,FireDiscoveryDateTime,PercentContained,"
    "IncidentSize,DiscoveryAcres,POOState,POOCounty,FireCause,"
    "IncidentShortDescription"
)
MAX_INCIDENTS = 300

# Acres to a 0-100 severity. A quarter-acre roadside fire and a campaign fire
# burning a county are both "a wildfire" to the feed and must not be to a
# reader. Bands rather than a curve because the number that matters is which
# band it is in — see bucket_of.
_ACRE_BANDS = ((100_000, 95), (50_000, 85), (10_000, 70),
               (1_000, 50), (100, 30), (10, 15))


def _wildfire_severity(acres: float, containment: float) -> int:
    """How much of a problem this fire is, on the shared 0-100 scale.

    Size sets it and containment takes it down: a 50,000-acre fire at 95% is
    genuinely less of an emergency than a 5,000-acre fire nobody has a line
    around yet. Never to zero — a fire that is still being reported is still
    burning.
    """
    base = 5
    for floor, score in _ACRE_BANDS:
        if acres >= floor:
            base = score
            break
    contained = max(0.0, min(100.0, containment)) / 100.0
    return max(5, int(round(base * (1.0 - 0.6 * contained))))


def _epoch_ms(value) -> datetime | None:
    """ArcGIS hands back epoch milliseconds, and sometimes nothing."""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.utcfromtimestamp(value / 1000.0)
    except (OverflowError, OSError, ValueError):
        return None


def _num(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def normalize_wfigs(payload: dict) -> list[dict] | None:
    """Incidents out of an ArcGIS feature collection.

    Returns None when the payload is not one — an ArcGIS error is a JSON body
    with an `error` key and HTTP 200, so status alone does not tell you.
    """
    if not isinstance(payload, dict) or "error" in payload:
        return None
    features = payload.get("features")
    if not isinstance(features, list):
        return None

    rows = []
    for feature in features:
        attrs = (feature or {}).get("attributes") or {}
        geom = (feature or {}).get("geometry") or {}
        lon, lat = geom.get("x"), geom.get("y")
        external_id = str(attrs.get("IrwinID") or "").strip()
        if external_id == "" or not isinstance(lat, (int, float)) \
                or not isinstance(lon, (int, float)):
            continue                       # no id or no point: nothing to draw
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue

        acres = max(0.0, _num(attrs.get("IncidentSize"))
                    or _num(attrs.get("DiscoveryAcres")))
        containment = max(0.0, min(100.0, _num(attrs.get("PercentContained"))))
        state = str(attrs.get("POOState") or "").replace("US-", "").strip()
        county = str(attrs.get("POOCounty") or "").strip()
        rows.append({
            "kind": KIND_WILDFIRE,
            "provider": "wfigs",
            "external_id": external_id[:120],
            "name": (str(attrs.get("IncidentName") or "").strip()
                     or "Unnamed incident")[:200],
            "lat": float(lat),
            "lon": float(lon),
            "country": "US",
            "severity": _wildfire_severity(acres, containment),
            "started_at": _epoch_ms(attrs.get("FireDiscoveryDateTime")),
            "raw": {
                "acres": round(acres, 1),
                "containment": round(containment),
                "state": state,
                "county": county,
                "cause": str(attrs.get("FireCause") or "").strip(),
                "note": str(attrs.get("IncidentShortDescription") or "").strip()[:500],
            },
        })
    return rows


async def fetch_wfigs(client: httpx.AsyncClient) -> list[dict] | None:
    params = {
        "where": "IncidentTypeCategory = 'WF'",
        "outFields": WFIGS_FIELDS,
        "returnGeometry": "true",
        "outSR": "4326",
        "orderByFields": "IncidentSize DESC",
        "resultRecordCount": str(MAX_INCIDENTS),
        "f": "json",
    }
    try:
        resp = await client.get(WFIGS_URL, params=params)
    except (httpx.HTTPError, safefetch.BlockedURL) as exc:
        log.warning("wfigs: could not reach the incident feed: %s", exc)
        return None
    if resp.status_code != 200:
        log.warning("wfigs: HTTP %s", resp.status_code)
        return None
    try:
        rows = normalize_wfigs(resp.json())
    except ValueError as exc:
        log.warning("wfigs: response was not JSON: %s", exc)
        return None
    if rows is None:
        log.warning("wfigs: response was not a feature collection")
    return rows


PROVIDERS = {"wfigs": fetch_wfigs}


# ---------- the poll ----------

async def _fetch_all() -> tuple[list[dict], set[str]]:
    """Every configured provider, concurrently. Returns the rows and the set of
    providers that actually answered — pruning is scoped to those, so one
    provider being down never deletes another's rows."""
    rows: list[dict] = []
    answered: set[str] = set()
    async with safefetch.client(timeout=FETCH_TIMEOUT) as client:
        results = await asyncio.gather(
            *(fetch(client) for fetch in PROVIDERS.values()),
            return_exceptions=True)
    for name, result in zip(PROVIDERS, results):
        if isinstance(result, BaseException):
            log.warning("%s: poll raised %s", name, result)
            continue
        if result is None:
            continue                       # said nothing; must not prune it
        if not result:
            # An empty list from a provider that answered cleanly is a real
            # answer in principle, but in practice it is far more often a
            # renamed field or a silent rate-limit. Treated as "said nothing",
            # because the cost of being wrong the other way is deleting the
            # table and re-alerting everyone when it comes back.
            log.warning("%s: answered with no rows; treating as a failed poll", name)
            continue
        answered.add(name)
        rows.extend(result)
    return rows, answered


def _upsert(db: Session, rows: list[dict]) -> dict:
    """Write the poll's rows, and say what genuinely moved.

    "Changed" is narrow on purpose. Every poll re-reports every hazard, so
    "appeared in this response" describes all of them and is useless — what a
    reader needs to hear about is a hazard that is new, or one that got worse.

    Every existing row is loaded in one query rather than looked up per
    incident. Three hundred incidents is three hundred round trips otherwise,
    every fifteen minutes, for a set small enough to hold in a dict.
    """
    now = utcnow()
    providers = {row["provider"] for row in rows}
    known = {(h.provider, h.external_id): h for h in db.scalars(
        select(Hazard).where(Hazard.provider.in_(providers)))} if providers else {}

    added, updated, escalated = 0, 0, 0
    for row in rows:
        existing = known.get((row["provider"], row["external_id"]))
        if existing is None:
            db.add(Hazard(**row, first_seen_at=now, updated_at=now, last_seen_at=now))
            added += 1
            continue
        was = bucket_of_score(existing.severity, existing.kind)
        for field, value in row.items():
            setattr(existing, field, value)
        existing.last_seen_at = now
        now_bucket = bucket_of_score(existing.severity, existing.kind)
        if now_bucket != was:
            existing.updated_at = now
            updated += 1
            if now_bucket > was:
                escalated += 1
    db.commit()
    return {"seen": len(rows), "added": added, "updated": updated,
            "escalated": escalated}


# Alerting steps on buckets, never on the raw score, so a fire creeping from
# 62 to 63 is not news and one crossing out of "large" is. Air quality will
# bring the EPA's own six categories in behind the same call.
_WILDFIRE_BUCKETS = (80, 60, 40, 20)


def bucket_of_score(severity: int, kind: str) -> int:
    if kind == KIND_WILDFIRE:
        for step, floor in enumerate(_WILDFIRE_BUCKETS):
            if severity >= floor:
                return len(_WILDFIRE_BUCKETS) - step
        return 0
    return severity // 20


def bucket_of(hazard: Hazard) -> int:
    return bucket_of_score(hazard.severity, hazard.kind)


def _prune(db: Session, answered: set[str]) -> int:
    """Drop what the provider has stopped listing, and cap the table.

    Scoped to providers that answered this poll: one being down must never
    delete another's rows, and a provider that said nothing keeps everything it
    ever told us until it speaks again.
    """
    if not answered:
        return 0
    removed = 0
    if RETENTION_DAYS > 0:
        cutoff = utcnow() - timedelta(days=RETENTION_DAYS)
        removed += db.execute(sa_delete(Hazard).where(
            Hazard.provider.in_(answered),
            Hazard.last_seen_at < cutoff)).rowcount or 0

    # The backstop. Only reachable if a provider returns far more than it is
    # supposed to; keep the worst, since a table this size is read by a map and
    # the severe ones are what anybody is looking for.
    total = db.scalar(select(func.count(Hazard.id))) or 0
    if total > MAX_HAZARDS:
        doomed = db.scalars(
            select(Hazard.id).order_by(Hazard.severity.asc(),
                                       Hazard.last_seen_at.asc())
            .limit(total - MAX_HAZARDS)).all()
        if doomed:
            removed += db.execute(sa_delete(Hazard).where(
                Hazard.id.in_(doomed))).rowcount or 0
            log.warning("hazards: capped the table at %s, dropped %s",
                        MAX_HAZARDS, len(doomed))
    if removed:
        db.commit()
    return removed


async def poll(db: Session) -> dict:
    """One hazard cycle. Never raises into the ingest loop."""
    started = time.monotonic()
    try:
        rows, answered = await _fetch_all()
        if not answered:
            return {"ok": False, "at": utcnow().isoformat(),
                    "error": "no provider answered"}
        counts = await asyncio.to_thread(_upsert, db, rows)
        pruned = await asyncio.to_thread(_prune, db, answered)
        return {"ok": True, "at": utcnow().isoformat(),
                "providers": sorted(answered), "pruned": pruned,
                "seconds": round(time.monotonic() - started, 2), **counts}
    except Exception as exc:                      # never break the news poll
        log.exception("hazard poll failed")
        return {"ok": False, "at": utcnow().isoformat(), "error": str(exc)}
