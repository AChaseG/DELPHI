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

On unless `NEWS_HAZARDS=0` — see enabled() for why that default was reversed.
Providers needing a key no-op without one rather than failing, but they say so
in the status: a layer that is empty because nobody set a key is otherwise
indistinguishable from one that is empty because nothing is burning, and only
one of those is worth an operator's evening.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta

import httpx
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import mailer, safefetch
from .events import broadcaster
from .geo import haversine_km, nearest_edge_km
from .models import FavoriteLocation, Hazard, HazardHit, User, utcnow

log = logging.getLogger("hazards")

# How often the loop runs a poll. WFIGS moves on the order of minutes to hours;
# against a 15-second ingest tick this is one HTTP request every sixty ticks,
# which is what "does not compete with the news poll" looks like in practice.
POLL_EVERY_SECONDS = float(os.environ.get("NEWS_HAZARD_EVERY_S", "900"))
RETENTION_DAYS = float(os.environ.get("NEWS_HAZARD_RETENTION_DAYS", "7"))
# Air goes stale in a way a fire does not. A wildfire the feed stopped listing
# a day ago is still worth showing while the picture settles; an air-quality
# reading six hours old is a different city's afternoon, and a dot presented as
# current when it is not is worse than no dot. Six hours also covers a station
# that reports hourly and skips one.
AIR_RETENTION_HOURS = float(os.environ.get("NEWS_AIR_RETENTION_H", "6"))
# A backstop against a provider bug, not a disk-space policy. The real risk is
# subtler than the volume filling: storage.over_ceiling() measures the whole
# database file while prune_to_fit() only ever deletes *articles*, so a hazard
# table left to grow would quietly push news out of the archive instead.
#
# **Per provider, and that is a correction rather than a refinement.** This was
# one global cap that deleted the lowest-severity rows in the whole table until
# the total fitted, and it was wrong in two ways that only became visible once
# there were enough providers to reach it:
#
#   · It let one talkative provider evict another's rows. The module already
#     refuses to let a provider that is *down* delete anybody else's rows (see
#     _prune); a provider that is merely chatty must not either. With OpenAQ
#     able to return eight thousand stations and PurpleAir three thousand, the
#     table crossed a five-thousand-row cap on the first poll and everything
#     after it was eviction.
#   · It compared severities across kinds, and those are not one scale. They
#     share a range and nothing else. A fire in the smallest band scores 5 and
#     a station reading Good scores 10 — so the cap that exists to protect the
#     news archive was sorting *wildfires* to the front of the queue and
#     deleting them to make room for clean-air readings.
#
# Within one provider the severity ordering is meaningful, so a provider over
# its own cap still sheds its least important rows first. Across providers
# nothing is compared at all. The total is bounded by construction: this many
# rows times however many providers are configured.
MAX_PER_PROVIDER = int(os.environ.get("NEWS_MAX_HAZARDS_PER_PROVIDER", "4000"))
# The whole-table figure is now a tripwire rather than a deleter. If per-provider
# capping is working this is unreachable, so reaching it means something is
# wrong in a way that deleting rows would hide rather than fix.
MAX_HAZARDS = int(os.environ.get("NEWS_MAX_HAZARDS", "40000"))
FETCH_TIMEOUT = 30.0

KIND_WILDFIRE = "wildfire"


def enabled() -> bool:
    """On unless an operator turns it off, which is a reversal worth explaining.

    This shipped defaulting to *off* — "ship it dark and turn it on
    deliberately" — and that turned out to be a bad trade. The layers appear in
    the map's control whether or not the poller is running, so an instance with
    the flag unset looked exactly like an instance with no fires nearby: a
    ticked box and a blank map, with nothing anywhere to tell the two apart.
    Somebody spent an evening wondering why Seattle had no air quality.

    The caution it was protecting against has expired. WFIGS needs no key and
    costs one request every fifteen minutes; AirNow no-ops without one; the
    table is bounded well under a megabyte; and every failure mode now has a
    test. The real protection was always elsewhere anyway — both map layers are
    off per browser until a reader ticks them, so nothing shows up for anybody
    who has not asked for it.

    So the variable stays as an operator's *off* switch. A self-hosted copy that
    wants no US hazard polling sets NEWS_HAZARDS=0.
    """
    return os.environ.get("NEWS_HAZARDS", "1") not in ("0", "", "off", "false")


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


# ---------- WFIGS perimeters: the shape of the fire, not just its dot ----------
#
# The same ArcGIS organisation as the incident points above, so there is no new
# host, no key and no quota — and, crucially, the same identity: an IRWIN id,
# which is already what a wfigs Hazard's external_id is. A perimeter therefore
# attaches to a fire we already hold by primary key and never inserts one of
# its own. The point service stays the authority on what fires exist; this only
# ever says what shape one of them is.
#
# Two things about this feed shape everything below.
#
# **Most fires do not have one.** Perimeters get mapped for incidents big
# enough to be worth flying, and the service's own fall-off rules drop small
# ones that have gone quiet. That is not a gap to paper over — it is why
# `Hazard.geometry` is nullable and why the map falls back to an *explicitly
# labelled* approximate circle rather than pretending.
#
# **A perimeter is not as live as its fire.** Acreage moves with every
# situation report; the outline moves when somebody flies it, which for a
# campaign fire is roughly nightly. Hence `geometry_at` as its own timestamp:
# showing a day-old shape is fine, showing it as though it were current is not.

PERIMETER_URL = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/"
                 "services/WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query")
PERIMETER_FIELDS = "attr_IrwinID,poly_GISAcres,poly_DateCurrent"
# Degrees, because outSR is 4326 — roughly 200 m. ArcGIS generalises server-side
# with this, so the detail never crosses the wire at all. This single parameter
# is what turns a megabyte-per-fire feed into a kilobyte-per-fire one, and at
# Atlas's zoom levels 200 m is under a pixel until the reader is very close in.
PERIMETER_OFFSET = os.environ.get("NEWS_PERIMETER_OFFSET", "0.002")
# About a metre. Federal geometry arrives with far more decimal places than any
# map uses, and half the payload is digits nobody can see.
PERIMETER_PRECISION = "5"
# Below this, a perimeter is smaller than the marker that would sit on top of
# it. Asking for thousands of them to draw nothing is the definition of cost
# without benefit.
MIN_PERIMETER_ACRES = float(os.environ.get("NEWS_PERIMETER_MIN_ACRES", "100"))
MAX_PERIMETERS = 400
# A hard ceiling per shape, after generalisation. A fire whose outline still
# will not fit in this has an outline nobody can read anyway, and it falls back
# to the labelled circle — which is the honest outcome, and much better than a
# payload that punishes every reader panning the map.
MAX_GEOMETRY_BYTES = int(os.environ.get("NEWS_MAX_GEOMETRY_BYTES", "32768"))


def _iso_ms(value) -> datetime | None:
    """A perimeter's date, which arrives as epoch milliseconds like everything
    else out of ArcGIS but occasionally as an ISO string instead."""
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")) \
                .replace(tzinfo=None)
        except ValueError:
            return None
    return _epoch_ms(value)


def normalize_perimeters(payload) -> dict[str, tuple[dict, datetime | None]] | None:
    """GeoJSON features keyed by the IRWIN id of the fire they belong to.

    Returns None when the body is not a feature collection — an ArcGIS error is
    a JSON body with an `error` key served with HTTP 200, so the status line
    alone never tells you.

    Asked for as GeoJSON rather than Esri JSON deliberately: `geo.point_in_geo`
    and Leaflet both speak GeoJSON natively, and hand-converting Esri rings is
    a well-known way to get winding order wrong in a way that only shows up on
    polygons with holes.
    """
    if not isinstance(payload, dict) or "error" in payload:
        return None
    features = payload.get("features")
    if not isinstance(features, list):
        return None

    out: dict[str, tuple[dict, datetime | None]] = {}
    for feature in features:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") or {}
        geom = feature.get("geometry")
        irwin = str(props.get("attr_IrwinID") or "").strip()
        if not irwin or not isinstance(geom, dict):
            continue
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        if not isinstance(geom.get("coordinates"), list) or not geom["coordinates"]:
            continue
        # Measured after generalisation, on what would actually be stored and
        # shipped. A cheap check on the one number that matters.
        if len(json.dumps(geom, separators=(",", ":"))) > MAX_GEOMETRY_BYTES:
            log.info("perimeters: %s is too large to draw; leaving it a circle", irwin)
            continue
        out[irwin] = (geom, _iso_ms(props.get("poly_DateCurrent")))
    return out


async def fetch_perimeters(client: httpx.AsyncClient) -> dict | None:
    params = {
        "where": f"poly_GISAcres >= {MIN_PERIMETER_ACRES}",
        "outFields": PERIMETER_FIELDS,
        "returnGeometry": "true",
        "outSR": "4326",
        "maxAllowableOffset": PERIMETER_OFFSET,
        "geometryPrecision": PERIMETER_PRECISION,
        "orderByFields": "poly_GISAcres DESC",
        "resultRecordCount": str(MAX_PERIMETERS),
        "f": "geojson",
    }
    try:
        resp = await client.get(PERIMETER_URL, params=params)
    except (httpx.HTTPError, safefetch.BlockedURL) as exc:
        log.warning("perimeters: could not reach the perimeter feed: %s", exc)
        return None
    if resp.status_code != 200:
        log.warning("perimeters: HTTP %s", resp.status_code)
        return None
    try:
        shapes = normalize_perimeters(resp.json())
    except ValueError as exc:
        log.warning("perimeters: response was not JSON: %s", exc)
        return None
    if shapes is None:
        log.warning("perimeters: response was not a feature collection")
    return shapes


def attach_perimeters(db: Session, shapes: dict) -> int:
    """Hang each shape on the fire it belongs to. Never creates a hazard.

    Strictly optional work, and the failure semantics are the opposite of every
    other fetch here: a perimeter feed that goes down leaves the previous
    shapes exactly where they are rather than clearing them. The fires are the
    payload and the outlines are the garnish, and a fire that briefly loses its
    outline is a smaller lie than a fire that vanishes.
    """
    if not shapes:
        return 0
    fires = db.scalars(select(Hazard).where(
        Hazard.provider == "wfigs",
        Hazard.external_id.in_(list(shapes)))).all()
    attached = 0
    for fire in fires:
        geom, drawn_at = shapes[fire.external_id]
        if fire.geometry != geom:
            fire.geometry = geom
            attached += 1
        fire.geometry_at = drawn_at or fire.geometry_at or utcnow()
    if attached:
        db.commit()
    return attached


# ---------- AirNow: what the air is actually like ----------
#
# The EPA's own service: regulatory monitors run by the agencies whose numbers
# get quoted when a county tells people to stay indoors. One dot per station,
# at the station's real coordinate.
#
# Needs a key. Without one the provider is simply absent — no error, no empty
# layer, nothing to explain to somebody who never asked for air quality.
#
# **Why this layer is thinner than the map on fire.airnow.gov**, since the
# question will be asked again and the answer is not in any parameter here:
#
# That is the EPA's *Fire and Smoke Map*, which is a different product from
# the AirNow API. It draws three networks on one canvas — AirNow's own
# regulatory monitors, temporary monitors, and tens of thousands of
# **PurpleAir** low-cost sensors, the last put through a correction equation
# EPA publishes for exactly that purpose. Only the first two come out of
# airnowapi.org. PurpleAir sensors are a separate company's network with a
# separate (paid) API, and no combination of arguments to this endpoint will
# ever return one. Matching that map's density means integrating PurpleAir
# directly — a decision about money and about trusting consumer hardware, not
# something to quietly bolt on here.
#
# What *was* ours to fix, and now is: this asked for permanent monitors only
# and a two-hour window over the lower 48, which dropped the mobile units
# agencies deploy into smoke, anything reporting late, and Alaska and Hawaii
# entirely.

AIRNOW_URL = "https://www.airnowapi.org/aq/data/"
# Wide enough for Alaska and Hawaii as well as the lower 48. This was CONUS
# only, which quietly meant an Anchorage reader had no air layer at all and no
# way to find out why.
AIRNOW_BBOX = os.environ.get("NEWS_AIRNOW_BBOX", "-180,15,-65,72")
AIRNOW_PARAMETERS = "PM25,OZONE,PM10"
# Permanent *and* mobile monitors ("2"). This asked for permanent only, which
# is the single biggest reason our map was thinner than AirNow's own: the
# temporary units are exactly what agencies wheel out during a fire, so the
# monitors missing from our layer were the ones nearest the smoke.
AIRNOW_MONITOR_TYPE = os.environ.get("NEWS_AIRNOW_MONITORS", "2")
# Three hours, not two. A monitor that reports hourly but lands late was being
# dropped for the sake of an hour's freshness it does not have anyway.
AIRNOW_WINDOW_HOURS = float(os.environ.get("NEWS_AIRNOW_WINDOW_H", "3"))
KIND_AIR = "air_quality"

# The EPA's six categories, as (floor, label). These are not Delphi's opinion —
# they are the published breakpoints the whole country's guidance is written
# against, which is why alerting steps on them rather than on the AQI number.
AQI_CATEGORIES = ((301, "Hazardous"), (201, "Very Unhealthy"),
                  (151, "Unhealthy"), (101, "Unhealthy for Sensitive Groups"),
                  (51, "Moderate"), (0, "Good"))
# Category to the shared 0-100 severity, so one colour ramp means roughly the
# same thing whether it is drawn over a fire or a monitor.
_AQI_SEVERITY = {5: 100, 4: 85, 3: 65, 2: 45, 1: 25, 0: 10}


def aqi_category(aqi: float) -> int:
    """0 (Good) through 5 (Hazardous)."""
    for step, (floor, _label) in enumerate(AQI_CATEGORIES):
        if aqi >= floor:
            return len(AQI_CATEGORIES) - 1 - step
    return 0


def aqi_label(aqi: float) -> str:
    for floor, label in AQI_CATEGORIES:
        if aqi >= floor:
            return label
    return "Good"


def normalize_airnow(payload) -> list[dict] | None:
    """One row per monitoring station, carrying its worst pollutant.

    AirNow answers per station *per parameter*, so a site measuring both
    particulates and ozone arrives twice. Drawing both would put two dots on
    one place and make the worse of them the one that happens to be underneath.
    The station's reading is its highest AQI, which is also how the EPA reports
    it: the index is a maximum across pollutants, not an average.
    """
    if not isinstance(payload, list):
        return None                        # an error body is a dict or a string

    worst: dict[str, dict] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        lat, lon = entry.get("Latitude"), entry.get("Longitude")
        aqi = entry.get("AQI")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        if not isinstance(aqi, (int, float)) or isinstance(aqi, bool) or aqi < 0:
            continue                       # -999 is AirNow for "no reading"
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        # The station's own identifier where there is one; a station without a
        # code is still a real point, so fall back to where it stands.
        code = str(entry.get("FullAQSCode") or entry.get("IntlAQSCode") or "").strip()
        key = code or f"{round(float(lat), 4)},{round(float(lon), 4)}"
        best = worst.get(key)
        if best is not None and best["_aqi"] >= aqi:
            continue
        site = str(entry.get("SiteName") or "").strip()
        worst[key] = {
            "kind": KIND_AIR,
            "provider": "airnow",
            "external_id": key[:120],
            "name": site or "Air quality station",
            "lat": float(lat),
            "lon": float(lon),
            "country": "US",
            "severity": _AQI_SEVERITY[aqi_category(aqi)],
            "started_at": None,
            "_aqi": float(aqi),
            "raw": {
                "aqi": int(aqi),
                "category": aqi_label(aqi),
                "parameter": str(entry.get("Parameter") or "").strip(),
                "site": site,
                "agency": str(entry.get("AgencyName") or "").strip(),
                "observed_utc": str(entry.get("UTC") or "").strip(),
            },
        }
    for row in worst.values():
        row.pop("_aqi", None)
    return list(worst.values())


async def fetch_airnow(client: httpx.AsyncClient) -> list[dict] | None:
    key = os.environ.get("AIRNOW_API_KEY", "").strip()
    if not key:
        return None                        # not configured: no layer, no noise
    # A window rather than "now". Stations report on their own clock and the
    # service lags them; asking for the current hour alone routinely comes back
    # empty, which this module is required to treat as a failure.
    end = datetime.utcnow()
    start = end - timedelta(hours=AIRNOW_WINDOW_HOURS)
    params = {
        "startDate": start.strftime("%Y-%m-%dT%H"),
        "endDate": end.strftime("%Y-%m-%dT%H"),
        "parameters": AIRNOW_PARAMETERS,
        "BBOX": AIRNOW_BBOX,
        "dataType": "A",                   # the index itself, not raw concentrations
        "format": "application/json",
        "verbose": "1",                    # station name and agency
        "monitorType": AIRNOW_MONITOR_TYPE,
        "API_KEY": key,
    }
    try:
        resp = await client.get(AIRNOW_URL, params=params)
    except (httpx.HTTPError, safefetch.BlockedURL) as exc:
        log.warning("airnow: could not reach the service: %s", exc)
        return None
    if resp.status_code != 200:
        # 401/403 is a bad or expired key; say which, since the fix differs.
        log.warning("airnow: HTTP %s%s", resp.status_code,
                    " — check AIRNOW_API_KEY" if resp.status_code in (401, 403) else "")
        return None
    try:
        rows = normalize_airnow(resp.json())
    except ValueError as exc:
        log.warning("airnow: response was not JSON: %s", exc)
        return None
    if rows is None:
        log.warning("airnow: response was not a list of observations")
    return rows



# ---------- OpenAQ: the air everywhere the EPA is not ----------
#
# AirNow is the EPA, and the EPA stops at the border. Delphi is a news product
# in twenty-three languages, so an air layer that is blank for most of its
# readers is a layer most of its readers should not be shown. OpenAQ is the
# aggregator that fixes it: government and research monitors worldwide, a free
# key, and — the thing that decided it — licence terms that permit storing the
# data and serving it on, which is what this module does by construction.
#
# **What it does not fix.** OpenAQ carried PurpleAir until 11 March 2024, when
# access ended for reasons OpenAQ describes as outside its control. So this
# closes the *coverage* gap and not the *density* one; nothing here brings us
# closer to the EPA's Fire and Smoke Map. Two separate problems, and it is
# worth being precise about which one is solved.
#
# **The genuine complication: OpenAQ reports concentrations, not an index.**
# AirNow hands back an AQI that is already computed, already on the EPA's
# scale, and already NowCast-smoothed for real-time use. OpenAQ hands back
# 18.4 µg/m³ with a unit attached, and units travel with the value and are
# never converted at the source — the same pollutant has several parameter
# entries for different units. Everything downstream of normalize_airnow
# assumes an index, so something has to bridge that, and there are three ways
# to do it of which only one is honest:
#
#   · Compute NowCast properly, as AirNow does — a weighted average over the
#     previous twelve hours, so a spike does not swing the number and a lull
#     does not hide one. It needs twelve hours of history per sensor, which
#     means a measurements table an order of magnitude larger than everything
#     else in this schema, and a backfill for every new station. No.
#   · Push a single hourly reading through the EPA's breakpoint table and call
#     the answer "AQI". Cheap, and wrong: the PM2.5 breakpoints are defined
#     against a 24-hour average and the ozone ones against an 8-hour average,
#     so one hour of smoke would read as a day of it. No.
#   · Apply the table and say exactly what was done. The *category* is the
#     useful part — a reader wants to know whether the air is bad, not an
#     integer — and the breakpoints are the published thresholds that question
#     is answered against. So compute it, store it, and label it as derived
#     from a single recent reading rather than as an AQI. Yes.
#
# Which is why every OpenAQ row carries `estimated: True` in `raw`, and why
# the wording that rides on it is not to be trimmed for looks.

OPENAQ_BASE = "https://api.openaq.org/v3"
# The pollutants the EPA publishes breakpoints for. Anything else OpenAQ
# measures is real data we have no scale for, and inventing one would be worse
# than not drawing it.
OPENAQ_PARAMETERS = ("pm25", "pm10", "o3", "no2", "so2", "co")
# Global coverage means the rectangle is the world, so pagination matters here
# in a way it never has for a single-request provider. A cap rather than a
# loop-until-done: a provider bug that paginated forever would otherwise hold
# the cycle lock for as long as it liked.
OPENAQ_MAX_PAGES = int(os.environ.get("NEWS_OPENAQ_MAX_PAGES", "8"))
OPENAQ_PAGE_SIZE = 1000

# EPA breakpoints: (concentration low, concentration high, AQI low, AQI high).
# Not Delphi's opinion — the published thresholds the whole of US air-quality
# guidance is written against, and the same ones AirNow's own numbers come
# from, so a category means the same thing whichever provider produced it.
# PM2.5 is the 2024 revision.
#
# `unit` is what the concentration must be in before the table is read, and
# `decimals` is where EPA truncates it first — that truncation is part of the
# standard, not a rounding preference, and leaving it out puts values near a
# boundary in the wrong band.
_EPA_BREAKPOINTS = {
    "pm25": {"unit": "ug/m3", "decimals": 1, "bands": (
        (0.0, 9.0, 0, 50), (9.1, 35.4, 51, 100), (35.5, 55.4, 101, 150),
        (55.5, 125.4, 151, 200), (125.5, 225.4, 201, 300),
        (225.5, 325.4, 301, 500))},
    "pm10": {"unit": "ug/m3", "decimals": 0, "bands": (
        (0, 54, 0, 50), (55, 154, 51, 100), (155, 254, 101, 150),
        (255, 354, 151, 200), (355, 424, 201, 300), (425, 604, 301, 500))},
    "o3": {"unit": "ppm", "decimals": 3, "bands": (
        (0.0, 0.054, 0, 50), (0.055, 0.070, 51, 100), (0.071, 0.085, 101, 150),
        (0.086, 0.105, 151, 200), (0.106, 0.200, 201, 300))},
    "no2": {"unit": "ppb", "decimals": 0, "bands": (
        (0, 53, 0, 50), (54, 100, 51, 100), (101, 360, 101, 150),
        (361, 649, 151, 200), (650, 1249, 201, 300), (1250, 2049, 301, 500))},
    "so2": {"unit": "ppb", "decimals": 0, "bands": (
        (0, 35, 0, 50), (36, 75, 51, 100), (76, 185, 101, 150),
        (186, 304, 151, 200), (305, 604, 201, 300), (605, 1004, 301, 500))},
    "co": {"unit": "ppm", "decimals": 1, "bands": (
        (0.0, 4.4, 0, 50), (4.5, 9.4, 51, 100), (9.5, 12.4, 101, 150),
        (12.5, 15.4, 151, 200), (15.5, 30.4, 201, 300),
        (30.5, 50.4, 301, 500))},
}

# Molar masses, for the one conversion that is not a factor of ten. A gas
# reported as a mass per volume becomes a volume fraction at EPA's stated
# reference conditions (25 °C, 1 atm), where a mole occupies 24.45 litres.
_MOLAR_MASS = {"o3": 48.00, "no2": 46.0055, "so2": 64.066, "co": 28.010}
_MOLAR_VOLUME = 24.45


def convert_unit(parameter: str, value: float, unit: str) -> float | None:
    """A measurement in whatever OpenAQ reported, in the unit the table wants.

    Returns None for a unit this cannot honestly convert, because guessing at
    one produces a number that is wrong by a factor of a thousand and looks
    entirely plausible.
    """
    spec = _EPA_BREAKPOINTS.get(parameter)
    if spec is None:
        return None
    want = spec["unit"]
    unit = (unit or "").strip().lower().replace("µ", "u").replace("³", "3")
    unit = {"ug/m³": "ug/m3", "μg/m3": "ug/m3", "ugm3": "ug/m3",
            "particles/cm³": "", "c": "", "f": ""}.get(unit, unit)
    if unit == want:
        return float(value)
    if want == "ug/m3":
        # Particulates only, and only a decimal step is defensible: turning a
        # volume fraction into a mass needs a density this does not have.
        if unit == "mg/m3":
            return float(value) * 1000.0
        return None
    mass = _MOLAR_MASS.get(parameter)
    ppm = None
    if unit == "ppm":
        ppm = float(value)
    elif unit == "ppb":
        ppm = float(value) / 1000.0
    elif unit == "ug/m3" and mass:
        ppm = float(value) * _MOLAR_VOLUME / mass / 1000.0
    if ppm is None:
        return None
    return ppm if want == "ppm" else ppm * 1000.0


def concentration_to_aqi(parameter: str, value, unit: str) -> int | None:
    """One reading, on the EPA's index. None when it cannot be placed there.

    Linear interpolation inside the band the truncated concentration falls in,
    which is the formula the standard specifies rather than a curve fitted to
    it. A value above the top band has no defined index — the table simply
    stops — so it is pinned to the ceiling rather than extrapolated into a
    number nobody publishes.
    """
    spec = _EPA_BREAKPOINTS.get(parameter)
    if spec is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    conc = convert_unit(parameter, float(value), unit)
    if conc is None or conc < 0:
        return None
    # EPA truncates rather than rounds, and it is part of the method: 9.09
    # µg/m³ is Good, not Moderate.
    factor = 10 ** spec["decimals"]
    conc = int(conc * factor) / factor
    bands = spec["bands"]
    for c_low, c_high, i_low, i_high in bands:
        if conc <= c_high:
            if conc < c_low:                   # inside a gap between bands
                return i_low
            return int(round((i_high - i_low) / (c_high - c_low)
                             * (conc - c_low) + i_low))
    return bands[-1][3]


def _openaq_us_excluded() -> bool:
    """Whether AirNow is answering for the United States.

    The two scales must never meet. AirNow publishes a real, NowCast-smoothed
    AQI; an OpenAQ row carries a category estimated from one hourly reading.
    Averaging them — which `air_readings` would happily do for any two stations
    within five kilometres of a place — produces a number that is neither.

    So: AirNow owns the US, OpenAQ owns everywhere else, and the boundary is a
    border rather than a radius. Proximity dedupe sounds tidier and is worse —
    a station 1.1 km from an AirNow site would survive while one 0.9 km away
    did not, making the rule that decides which scale a reader sees depend on
    geometry they cannot inspect. With no AirNow key, OpenAQ covers the US too,
    which is strictly better than a blank map.
    """
    return bool(os.environ.get("AIRNOW_API_KEY", "").strip())


def normalize_openaq(locations: list, latest_by_parameter: dict) -> list[dict] | None:
    """One row per station, carrying its worst pollutant.

    Two payloads because that is what two endpoints give: `/v3/locations`
    describes the stations and their sensors (which pollutant, in which unit),
    and `/v3/parameters/{id}/latest` carries the readings keyed by sensor. The
    join is on the sensor id, and a reading whose sensor is not in the location
    set is dropped rather than guessed at.

    Same shape as normalize_airnow's answer, and for the same reason: a station
    measuring three pollutants arrives three times, and drawing all three would
    stack dots on one point with the worst of them underneath. The EPA's index
    is a maximum across pollutants, not an average, so the station's reading is
    its highest.
    """
    if not isinstance(locations, list) or not isinstance(latest_by_parameter, dict):
        return None

    # sensor id -> (station, parameter name, unit)
    sensors: dict[int, tuple[dict, str, str]] = {}
    stations: dict[int, dict] = {}
    skip_us = _openaq_us_excluded()
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        coords = loc.get("coordinates") or {}
        lat, lon = coords.get("latitude"), coords.get("longitude")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        country = str(((loc.get("country") or {}) if isinstance(loc.get("country"), dict)
                       else {}).get("code") or loc.get("country") or "").strip().upper()[:2]
        if skip_us and country == "US":
            continue
        loc_id = loc.get("id")
        if loc_id is None:
            continue
        stations[loc_id] = {
            "id": loc_id, "lat": float(lat), "lon": float(lon), "country": country,
            "name": str(loc.get("name") or "").strip() or "Air quality station",
            "provider": str((loc.get("provider") or {}).get("name")
                            if isinstance(loc.get("provider"), dict)
                            else loc.get("provider") or "").strip(),
        }
        for sensor in loc.get("sensors") or []:
            if not isinstance(sensor, dict):
                continue
            param = sensor.get("parameter") or {}
            name = str(param.get("name") or "").strip().lower()
            if name not in _EPA_BREAKPOINTS or sensor.get("id") is None:
                continue
            sensors[sensor["id"]] = (stations[loc_id], name,
                                     str(param.get("units") or "").strip())

    worst: dict[int, dict] = {}
    for readings in latest_by_parameter.values():
        for entry in readings or []:
            if not isinstance(entry, dict):
                continue
            found = sensors.get(entry.get("sensorsId"))
            if found is None:
                continue
            station, param, unit = found
            aqi = concentration_to_aqi(param, entry.get("value"), unit)
            if aqi is None:
                continue
            best = worst.get(station["id"])
            if best is not None and best["raw"]["aqi"] >= aqi:
                continue
            observed = entry.get("datetime") or {}
            worst[station["id"]] = {
                "kind": KIND_AIR,
                "provider": "openaq",
                "external_id": f"openaq:{station['id']}"[:120],
                "name": station["name"][:200],
                "lat": station["lat"], "lon": station["lon"],
                "country": station["country"],
                "severity": _AQI_SEVERITY[aqi_category(aqi)],
                "started_at": None,
                "raw": {
                    "aqi": int(aqi),
                    "category": aqi_label(aqi),
                    "parameter": param.upper(),
                    "value": round(float(entry.get("value") or 0), 3),
                    "unit": unit,
                    "site": station["name"],
                    "agency": station["provider"],
                    "observed_utc": str(observed.get("utc") if isinstance(observed, dict)
                                        else observed or "").strip(),
                    # The sentence this whole provider turns on: a category
                    # derived from one recent reading is not an AQI, and the
                    # reader is told so wherever the number is shown.
                    "estimated": True,
                },
            }
    return list(worst.values())


async def _openaq_pages(client: httpx.AsyncClient, path: str, params: dict) -> list | None:
    """Every page of one OpenAQ resource, up to the cap. None on any failure."""
    key = os.environ.get("OPENAQ_API_KEY", "").strip()
    out: list = []
    for page in range(1, OPENAQ_MAX_PAGES + 1):
        try:
            resp = await client.get(f"{OPENAQ_BASE}{path}",
                                    params={**params, "limit": OPENAQ_PAGE_SIZE,
                                            "page": page},
                                    headers={"X-API-Key": key})
        except (httpx.HTTPError, safefetch.BlockedURL) as exc:
            log.warning("openaq: could not reach %s: %s", path, exc)
            return None
        if resp.status_code == 429:
            # A rate limit is emphatically not an empty world. Returning what
            # we have so far would let the prune treat every station we did not
            # reach as retired.
            log.warning("openaq: rate limited on %s", path)
            return None
        if resp.status_code != 200:
            log.warning("openaq: HTTP %s on %s%s", resp.status_code, path,
                        " — check OPENAQ_API_KEY" if resp.status_code in (401, 403) else "")
            return None
        try:
            results = (resp.json() or {}).get("results")
        except ValueError as exc:
            log.warning("openaq: response was not JSON: %s", exc)
            return None
        if not isinstance(results, list):
            log.warning("openaq: %s did not answer with a result list", path)
            return None
        out.extend(results)
        if len(results) < OPENAQ_PAGE_SIZE:
            break
    return out


async def fetch_openaq(client: httpx.AsyncClient) -> list[dict] | None:
    """Stations and their latest readings, worldwide.

    Parameter ids are looked up rather than hardcoded: they are OpenAQ's own
    numbering, they are not part of any contract, and a table of magic numbers
    in this file would fail silently and invisibly if one ever moved.
    """
    if not os.environ.get("OPENAQ_API_KEY", "").strip():
        return None                        # not configured: no layer, no noise
    catalog = await _openaq_pages(client, "/parameters", {})
    if catalog is None:
        return None
    ids = {str(p.get("name") or "").strip().lower(): p.get("id")
           for p in catalog if isinstance(p, dict)}

    locations = await _openaq_pages(client, "/locations", {})
    if locations is None:
        return None

    latest: dict[str, list] = {}
    for name in OPENAQ_PARAMETERS:
        pid = ids.get(name)
        if pid is None:
            continue
        page = await _openaq_pages(client, f"/parameters/{pid}/latest", {})
        if page is None:
            return None                    # partial data would prune the rest
        latest[name] = page

    rows = normalize_openaq(locations, latest)
    if rows is None:
        log.warning("openaq: could not read the station list")
    return rows



# ---------- PurpleAir: the density the Fire and Smoke Map has ----------
#
# The comment above AirNow explains at length why our air layer is thinner than
# the EPA's own Fire and Smoke Map: that map draws tens of thousands of
# PurpleAir consumer sensors alongside the regulatory monitors, and no argument
# to airnowapi.org will ever return one. This is the other half of that
# sentence — PurpleAir's own API, which needs its own key.
#
# **These are not regulatory monitors and are never presented as though they
# were.** A PurpleAir unit is a light-scattering sensor on somebody's fence. It
# reads high in humidity, it can be pointed at a barbecue, and its two channels
# can disagree. Three things follow, and all three are load-bearing:
#
#   · the EPA's published correction is applied rather than the raw number,
#     because the raw number is wrong in a known direction;
#   · sensors the network itself doubts, and sensors indoors, are dropped;
#   · every row is flagged `estimated`, exactly as OpenAQ's are, so the reading
#     shown at a watched place says what it rests on.
#
# And where a regulatory monitor is also in range, it wins — see
# main.air_readings. The crowd's value is *coverage between* the good
# instruments, not a second opinion next to one.

PURPLEAIR_URL = "https://api.purpleair.com/v1/sensors"
# Where to look. The whole world is available and would be about thirty
# thousand sensors, most of them clustered in a few countries — enough to
# crowd every other hazard out of a table this size. The default is North
# America, which is where the density exists and where the smoke question gets
# asked; an operator elsewhere sets their own rectangle.
PURPLEAIR_BBOX = os.environ.get("NEWS_PURPLEAIR_BBOX", "-170,15,-50,72")
# The network's own confidence in a sensor's reading, 0-100, mostly a measure
# of whether its two channels agree. EPA excludes disagreeing sensors from the
# Fire and Smoke Map and so does this.
PURPLEAIR_MIN_CONFIDENCE = int(os.environ.get("NEWS_PURPLEAIR_MIN_CONF", "70"))
# A sensor that has not reported for an hour is not telling you about now.
PURPLEAIR_MAX_AGE_S = int(os.environ.get("NEWS_PURPLEAIR_MAX_AGE_S", "3600"))
# The cap that keeps this from eating the table. Kept by worst reading, because
# the entire reason anybody turns on a smoke layer is to find the smoke.
PURPLEAIR_MAX = int(os.environ.get("NEWS_PURPLEAIR_MAX", "3000"))
PURPLEAIR_FIELDS = ("name,latitude,longitude,pm2.5_cf_1,humidity,confidence,"
                    "location_type,last_seen")


def purpleair_correct(pm_cf1: float, humidity: float) -> float:
    """The EPA's US-wide correction for PurpleAir PM2.5.

    A light-scattering sensor over-reads, and it over-reads more in damp air.
    The correction is Barkjohn's, as adopted for the Fire and Smoke Map, in its
    extended piecewise form so that heavy smoke is handled rather than
    extrapolated:

        PA < 30            0.524·PA − 0.0862·RH + 5.75
        30 <= PA < 50      blended between the two slopes
        50 <= PA < 210     0.786·PA − 0.0862·RH + 5.75
        210 <= PA < 260    blended into the high-concentration curve
        PA >= 260          0.69·PA + 8.84e-4·PA² + 2.97

    Publishing the raw number instead would put a clear day into Moderate on
    nothing but the morning's humidity, which is the single most common way
    consumer air data misleads people.
    """
    pa = max(0.0, float(pm_cf1))
    rh = max(0.0, min(100.0, float(humidity or 0.0)))
    if pa < 30:
        out = 0.524 * pa - 0.0862 * rh + 5.75
    elif pa < 50:
        blend = pa / 20.0 - 1.5
        out = ((0.786 * blend + 0.524 * (1 - blend)) * pa
               - 0.0862 * rh * blend - 0.0862 * rh * (1 - blend) + 5.75)
    elif pa < 210:
        out = 0.786 * pa - 0.0862 * rh + 5.75
    elif pa < 260:
        blend = pa / 50.0 - 4.2
        out = ((0.69 * blend + 0.786 * (1 - blend)) * pa
               - 0.0862 * rh * (1 - blend) + 2.966 * blend
               + 5.75 * (1 - blend) + 8.84e-4 * blend * pa ** 2)
    else:
        out = 0.69 * pa + 8.84e-4 * pa ** 2 + 2.97
    return max(0.0, out)


def normalize_purpleair(payload) -> list[dict] | None:
    """Sensors out of PurpleAir's column-oriented answer.

    The response is `{"fields": [...], "data": [[...], ...]}` — a header row and
    then bare arrays, so the column order is discovered from `fields` rather
    than assumed. Assuming it is how a provider reordering its output turns
    latitude into humidity without anything raising.
    """
    if not isinstance(payload, dict):
        return None
    fields, data = payload.get("fields"), payload.get("data")
    if not isinstance(fields, list) or not isinstance(data, list):
        return None
    index = {str(name): position for position, name in enumerate(fields)}
    needed = ("latitude", "longitude", "pm2.5_cf_1", "confidence", "sensor_index")
    if not all(name in index for name in needed if name != "sensor_index"):
        return None

    def value(row, name, fallback=None):
        position = index.get(name)
        if position is None or position >= len(row):
            return fallback
        return row[position]

    rows = []
    for row in data:
        if not isinstance(row, list):
            continue
        lat, lon = value(row, "latitude"), value(row, "longitude")
        pm = value(row, "pm2.5_cf_1")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        if not isinstance(pm, (int, float)) or isinstance(pm, bool) or pm < 0:
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        confidence = value(row, "confidence", 100)
        if isinstance(confidence, (int, float)) and confidence < PURPLEAIR_MIN_CONFIDENCE:
            continue                       # the network does not trust it either
        if value(row, "location_type", 0) == 1:
            continue                       # indoors: nothing to do with the air outside
        humidity = value(row, "humidity", 0) or 0
        corrected = purpleair_correct(float(pm), float(humidity))
        aqi = concentration_to_aqi("pm25", corrected, "ug/m3")
        if aqi is None:
            continue
        sensor = value(row, "sensor_index")
        key = str(sensor if sensor is not None
                  else f"{round(float(lat), 5)},{round(float(lon), 5)}")
        rows.append({
            "kind": KIND_AIR,
            "provider": "purpleair",
            "external_id": f"purpleair:{key}"[:120],
            "name": str(value(row, "name", "") or "").strip()[:200] or "PurpleAir sensor",
            "lat": float(lat), "lon": float(lon), "country": "",
            "severity": _AQI_SEVERITY[aqi_category(aqi)],
            "started_at": None,
            "raw": {
                "aqi": int(aqi),
                "category": aqi_label(aqi),
                "parameter": "PM2.5",
                "value": round(corrected, 1),
                "raw_value": round(float(pm), 1),
                "unit": "µg/m³",
                "humidity": round(float(humidity)),
                "confidence": confidence if isinstance(confidence, (int, float)) else None,
                "agency": "PurpleAir (community sensor)",
                # Same flag OpenAQ sets, and it means more here: this is a
                # corrected reading from a consumer sensor, not an index any
                # agency stands behind.
                "estimated": True,
                "low_cost": True,
            },
        })
    # Worst first, then cut. The whole reason for a smoke layer is the smoke.
    rows.sort(key=lambda r: r["raw"]["aqi"], reverse=True)
    return rows[:PURPLEAIR_MAX]


async def fetch_purpleair(client: httpx.AsyncClient) -> list[dict] | None:
    key = os.environ.get("PURPLEAIR_API_KEY", "").strip()
    if not key:
        return None                        # not configured: no layer, no noise
    try:
        west, south, east, north = (float(p) for p in PURPLEAIR_BBOX.split(","))
    except ValueError:
        log.warning("purpleair: NEWS_PURPLEAIR_BBOX is not west,south,east,north")
        return None
    params = {
        "fields": PURPLEAIR_FIELDS,
        "location_type": "0",              # outdoors only
        "max_age": str(PURPLEAIR_MAX_AGE_S),
        "nwlng": str(west), "nwlat": str(north),
        "selng": str(east), "selat": str(south),
    }
    try:
        resp = await client.get(PURPLEAIR_URL, params=params,
                                headers={"X-API-Key": key})
    except (httpx.HTTPError, safefetch.BlockedURL) as exc:
        log.warning("purpleair: could not reach the sensor API: %s", exc)
        return None
    if resp.status_code != 200:
        log.warning("purpleair: HTTP %s%s", resp.status_code,
                    " — check PURPLEAIR_API_KEY is a *read* key"
                    if resp.status_code in (401, 403) else "")
        return None
    try:
        rows = normalize_purpleair(resp.json())
    except ValueError as exc:
        log.warning("purpleair: response was not JSON: %s", exc)
        return None
    if rows is None:
        log.warning("purpleair: response was not a fields/data table")
    return rows



# ---------- the rest of what makes a place dangerous ----------
#
# Fires and air were the two Typhon started with, and they are two of maybe
# eight things a reader would call a natural disaster. Three feeds cover almost
# all of the rest, all keyless, all one request:
#
#   GDACS  the global multi-hazard spine — earthquakes, cyclones, floods,
#          volcanoes, droughts and fires, each with a green/orange/red level
#          that is an *impact* judgement rather than a raw magnitude, which is
#          the right input to a severity scale: a magnitude 6 under a city and
#          a magnitude 6 under an ocean are not the same event.
#   USGS   earthquakes, minutes behind the shaking where GDACS is tens of
#          minutes, and carrying PAGER's own impact colour where it has one.
#   NWS    United States severe weather, and the only one of the three that
#          publishes a *polygon* — a tornado warning has a real edge, and the
#          geometry column the fire perimeters added is what draws it.
#
# GDACS is why this is worth doing at all: Typhon was two United States layers
# on a product read in twenty-three languages, and no amount of labelling fixes
# that. This is the correction.

KIND_EARTHQUAKE = "earthquake"
KIND_STORM = "storm"
KIND_FLOOD = "flood"
KIND_VOLCANO = "volcano"
KIND_DROUGHT = "drought"
KIND_SEVERE_WEATHER = "severe_weather"

# Every kind Typhon knows, in the order the layers panel lists them: the two
# that came first, then the global ones, then the United States one. A kind
# absent from here is a kind nothing can switch on, so this is the list to
# extend when a provider brings a new sort of thing.
KINDS = (KIND_WILDFIRE, KIND_AIR, KIND_EARTHQUAKE, KIND_STORM, KIND_FLOOD,
         KIND_VOLCANO, KIND_DROUGHT, KIND_SEVERE_WEATHER)

# Providers for which an empty answer is a real answer rather than a symptom.
#
# The default everywhere else — see _fetch_all — is that zero rows means a
# renamed field or a silent rate limit, because taking it at face value would
# empty the table and re-alert every watcher when it came back. But a quiet
# night genuinely does mean no severe weather warnings anywhere in the United
# States, and treating that as a failure would leave expired tornado warnings
# on the map until something else went wrong. The exception is declared here
# rather than assumed, and it is deliberately short.
EMPTY_IS_PLAUSIBLE = {"nws"}


# ---------- GDACS ----------

GDACS_URL = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
GDACS_DAYS = float(os.environ.get("NEWS_GDACS_DAYS", "10"))
# GDACS's own two-letter codes. WF is taken as well as the rest: not to compete
# with WFIGS, which is far better inside the United States, but because outside
# it GDACS is the only wildfire anybody here has.
_GDACS_KINDS = {"EQ": KIND_EARTHQUAKE, "TC": KIND_STORM, "FL": KIND_FLOOD,
                "VO": KIND_VOLCANO, "DR": KIND_DROUGHT, "WF": KIND_WILDFIRE}
# Green, orange, red — an assessment of likely impact, which is why it beats
# magnitude as a severity input.
_GDACS_SEVERITY = {"red": 90, "orange": 60, "green": 25}


def _gdacs_name(props: dict) -> str:
    for field in ("eventname", "name", "htmldescription", "description"):
        value = str(props.get(field) or "").strip()
        if value:
            return value
    return "Reported event"


def normalize_gdacs(payload) -> list[dict] | None:
    """GDACS events as hazards, one row per event.

    Episodes are deliberately collapsed: GDACS re-issues an event as its
    assessment changes, and the `Hazard` table is built to be rewritten — so
    the event id is the identity and the newest episode simply overwrites the
    row, which is how "the alert went from orange to red" reaches a reader at
    all.
    """
    if not isinstance(payload, dict):
        return None
    features = payload.get("features")
    if not isinstance(features, list):
        return None

    rows = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") or {}
        geom = feature.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if geom.get("type") != "Point" or len(coords) < 2:
            continue
        lon, lat = coords[0], coords[1]
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        code = str(props.get("eventtype") or "").strip().upper()
        kind = _GDACS_KINDS.get(code)
        event_id = str(props.get("eventid") or "").strip()
        if not kind or not event_id:
            continue
        country = str(props.get("iso3") or "")[:2].upper()
        # WFIGS is strictly better inside the United States — an incident point
        # with acreage and containment against a continent-scale bulletin — and
        # two providers drawing the same fire would put two dots on it under
        # two different names.
        if kind == KIND_WILDFIRE and str(props.get("iso3") or "").upper() == "USA":
            continue
        level = str(props.get("alertlevel") or "").strip().lower()
        severity_data = props.get("severitydata") or {}
        rows.append({
            "kind": kind,
            "provider": "gdacs",
            "external_id": f"{code}:{event_id}"[:120],
            "name": _gdacs_name(props)[:200],
            "lat": float(lat), "lon": float(lon),
            "country": country,
            "severity": _GDACS_SEVERITY.get(level, 25),
            "started_at": _iso_ms(props.get("fromdate")),
            "raw": {
                "alert_level": level or "green",
                "event_type": code,
                "country_name": str(props.get("country") or "").strip()[:120],
                "detail": str(severity_data.get("severitytext") or "").strip()[:200],
                "episode": str(props.get("episodeid") or "").strip(),
            },
        })
    return rows


async def fetch_gdacs(client: httpx.AsyncClient) -> list[dict] | None:
    end = datetime.utcnow()
    params = {
        "fromdate": (end - timedelta(days=GDACS_DAYS)).strftime("%Y-%m-%d"),
        "todate": end.strftime("%Y-%m-%d"),
        "alertlevel": "Green;Orange;Red",
        "eventlist": ";".join(_GDACS_KINDS),
    }
    try:
        resp = await client.get(GDACS_URL, params=params)
    except (httpx.HTTPError, safefetch.BlockedURL) as exc:
        log.warning("gdacs: could not reach the event list: %s", exc)
        return None
    if resp.status_code != 200:
        log.warning("gdacs: HTTP %s", resp.status_code)
        return None
    try:
        rows = normalize_gdacs(resp.json())
    except ValueError as exc:
        log.warning("gdacs: response was not JSON: %s", exc)
        return None
    if rows is None:
        log.warning("gdacs: response was not a feature collection")
    return rows


# ---------- USGS earthquakes ----------
#
# The 2.5-and-above feed over 24 hours, and the window is a correctness
# decision rather than a preference. The hourly feeds are legitimately empty on
# a quiet hour, and this module's central rule is that an empty answer is a
# failed poll — so an hourly feed would report failure for ever on exactly the
# nights when nothing is wrong. Magnitude 2.5 worldwide is on the order of a
# hundred events a day, which is never empty in practice.

USGS_URL = ("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/"
            "2.5_day.geojson")
# PAGER's estimate of what the shaking did to people, which is a better answer
# to "how bad is this" than the magnitude — a 6.0 under a city and a 6.0 under
# an ocean are not the same event.
_PAGER_SEVERITY = {"red": 95, "orange": 80, "yellow": 55, "green": 30}
_MAGNITUDE_BANDS = ((8.0, 95), (7.0, 85), (6.0, 70), (5.0, 50), (4.0, 35))


def _quake_severity(magnitude: float, pager: str) -> int:
    scored = _PAGER_SEVERITY.get((pager or "").strip().lower())
    if scored is not None:
        return scored
    for floor, score in _MAGNITUDE_BANDS:
        if magnitude >= floor:
            return score
    return 20


def normalize_usgs(payload) -> list[dict] | None:
    if not isinstance(payload, dict):
        return None
    features = payload.get("features")
    if not isinstance(features, list):
        return None

    rows = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") or {}
        geom = feature.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        lon, lat = coords[0], coords[1]
        depth = coords[2] if len(coords) > 2 else None
        event_id = str(feature.get("id") or "").strip()
        magnitude = props.get("mag")
        if not event_id or not isinstance(lat, (int, float)) \
                or not isinstance(lon, (int, float)):
            continue
        if not isinstance(magnitude, (int, float)) or isinstance(magnitude, bool):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        pager = str(props.get("alert") or "").strip().lower()
        rows.append({
            "kind": KIND_EARTHQUAKE,
            "provider": "usgs",
            "external_id": event_id[:120],
            "name": (str(props.get("title") or "").strip()
                     or f"M {magnitude} earthquake")[:200],
            "lat": float(lat), "lon": float(lon), "country": "",
            "severity": _quake_severity(float(magnitude), pager),
            "started_at": _epoch_ms(props.get("time")),
            "raw": {
                "magnitude": round(float(magnitude), 1),
                "depth_km": round(float(depth), 1) if isinstance(depth, (int, float)) else None,
                "place": str(props.get("place") or "").strip()[:200],
                "pager": pager,
                "tsunami": bool(props.get("tsunami")),
            },
        })
    return rows


async def fetch_usgs(client: httpx.AsyncClient) -> list[dict] | None:
    try:
        resp = await client.get(USGS_URL)
    except (httpx.HTTPError, safefetch.BlockedURL) as exc:
        log.warning("usgs: could not reach the earthquake feed: %s", exc)
        return None
    if resp.status_code != 200:
        log.warning("usgs: HTTP %s", resp.status_code)
        return None
    try:
        rows = normalize_usgs(resp.json())
    except ValueError as exc:
        log.warning("usgs: response was not JSON: %s", exc)
        return None
    if rows is None:
        log.warning("usgs: response was not a feature collection")
    return rows


# ---------- NWS severe weather ----------
#
# Filtered to Severe and Extreme, and that filter is the feature. Unfiltered
# this is thousands of frost advisories and small-craft warnings, and a map
# showing everything shows nothing.
#
# **Only alerts that carry a polygon are drawn**, which is a real limitation
# and is stated rather than hidden. Many NWS alerts are issued against UGC zone
# codes with no outline of their own; placing those would mean a request per
# zone to look the boundary up, which is the "one request per provider"
# property this module is built on. What survives the filter is the
# sharp-edged half — tornado, severe thunderstorm and flash flood warnings —
# which is also the half where a boundary is worth having.

NWS_URL = "https://api.weather.gov/alerts/active"
# api.weather.gov rejects requests without a descriptive User-Agent carrying a
# contact. An env var rather than a literal, because a self-hosted copy is not
# us and should not be the address NWS writes to.
NWS_CONTACT = os.environ.get("NEWS_NWS_CONTACT", "")
_NWS_SEVERITY = {"extreme": 90, "severe": 70, "moderate": 45, "minor": 25}


def _bbox_centre(geometry: dict) -> tuple[float, float] | None:
    """The middle of a shape's extent — enough to hang a marker on.

    Not a true centroid, and it does not need to be: the polygon itself is what
    is drawn and what a reader measures against. This only decides where the
    icon and the popup anchor sit, and for a warning polygon the difference is
    a few kilometres on a shape tens of kilometres across.
    """
    lats, lons = [], []
    for ring in _geo_rings(geometry):
        for point in ring:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                try:
                    lons.append(float(point[0]))
                    lats.append(float(point[1]))
                except (TypeError, ValueError):
                    continue
    if not lats:
        return None
    return ((min(lats) + max(lats)) / 2.0, (min(lons) + max(lons)) / 2.0)


def _geo_rings(geometry: dict):
    gtype = (geometry or {}).get("type", "")
    coords = (geometry or {}).get("coordinates") or []
    if gtype == "Polygon":
        yield from (r for r in coords if isinstance(r, list))
    elif gtype == "MultiPolygon":
        for poly in coords:
            if isinstance(poly, list):
                yield from (r for r in poly if isinstance(r, list))


def normalize_nws(payload) -> list[dict] | None:
    if not isinstance(payload, dict):
        return None
    features = payload.get("features")
    if not isinstance(features, list):
        return None

    rows = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") or {}
        geometry = feature.get("geometry")
        alert_id = str(feature.get("id") or props.get("id") or "").strip()
        if not alert_id or not isinstance(geometry, dict):
            continue                       # zone-coded alert: no outline to draw
        centre = _bbox_centre(geometry)
        if centre is None:
            continue
        severity = str(props.get("severity") or "").strip().lower()
        rows.append({
            "kind": KIND_SEVERE_WEATHER,
            "provider": "nws",
            "external_id": alert_id[-120:],
            "name": (str(props.get("event") or "").strip() or "Weather alert")[:200],
            "lat": centre[0], "lon": centre[1], "country": "US",
            "severity": _NWS_SEVERITY.get(severity, 45),
            "started_at": _iso_ms(props.get("effective") or props.get("sent")),
            "geometry": geometry,
            "raw": {
                "severity": severity,
                "urgency": str(props.get("urgency") or "").strip().lower(),
                "certainty": str(props.get("certainty") or "").strip().lower(),
                "area": str(props.get("areaDesc") or "").strip()[:300],
                "headline": str(props.get("headline") or "").strip()[:300],
                "office": str(props.get("senderName") or "").strip()[:120],
                # Warnings end at a stated time, unlike a fire that simply
                # stops being listed. Retention reads this — see _prune.
                "expires": str(props.get("expires") or props.get("ends") or "").strip(),
            },
        })
    return rows


async def fetch_nws(client: httpx.AsyncClient) -> list[dict] | None:
    if not NWS_CONTACT:
        return None                        # no contact address: no provider
    params = {"status": "actual", "severity": "Severe,Extreme",
              "message_type": "alert"}
    try:
        resp = await client.get(NWS_URL, params=params, headers={
            "User-Agent": f"D.E.L.P.H.I. news dashboard ({NWS_CONTACT})",
            "Accept": "application/geo+json",
        })
    except (httpx.HTTPError, safefetch.BlockedURL) as exc:
        log.warning("nws: could not reach the alerts feed: %s", exc)
        return None
    if resp.status_code != 200:
        log.warning("nws: HTTP %s%s", resp.status_code,
                    " — a descriptive User-Agent with a contact is required"
                    if resp.status_code in (400, 403) else "")
        return None
    try:
        rows = normalize_nws(resp.json())
    except ValueError as exc:
        log.warning("nws: response was not JSON: %s", exc)
        return None
    if rows is None:
        log.warning("nws: response was not a feature collection")
    return rows


PROVIDERS = {"wfigs": fetch_wfigs, "airnow": fetch_airnow,
             "openaq": fetch_openaq, "purpleair": fetch_purpleair,
             "gdacs": fetch_gdacs, "usgs": fetch_usgs, "nws": fetch_nws}


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
        if not result and name not in EMPTY_IS_PLAUSIBLE:
            # An empty list from a provider that answered cleanly is a real
            # answer in principle, but in practice it is far more often a
            # renamed field or a silent rate-limit. Treated as "said nothing",
            # because the cost of being wrong the other way is deleting the
            # table and re-alerting everyone when it comes back.
            #
            # The exception is declared, never inferred: a provider in
            # EMPTY_IS_PLAUSIBLE genuinely has quiet nights, and refusing its
            # empty answer would leave expired warnings on the map for as long
            # as nothing was wrong.
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
    # What is worth telling somebody about: the ones that are new to us and the
    # ones that got worse. Returned as rows rather than counted, because the
    # proximity pass needs the hazards themselves and re-reading the table to
    # find them again would be both slower and a different answer.
    changed: list[Hazard] = []
    for row in rows:
        existing = known.get((row["provider"], row["external_id"]))
        if existing is None:
            fresh = Hazard(**row, first_seen_at=now, updated_at=now, last_seen_at=now)
            db.add(fresh)
            changed.append(fresh)
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
                changed.append(existing)
    db.commit()          # ids are assigned here, which the hits will need
    return {"seen": len(rows), "added": added, "updated": updated,
            "escalated": escalated}, changed


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
    if kind == KIND_AIR:
        # Straight back to the EPA category the severity was derived from, so
        # a step here is a step the published guidance recognises: crossing out
        # of Moderate into Unhealthy for Sensitive Groups, not 51 into 52.
        for category, score in sorted(_AQI_SEVERITY.items()):
            if severity <= score:
                return category
        return max(_AQI_SEVERITY)
    return severity // 20


def bucket_of(hazard: Hazard) -> int:
    return bucket_of_score(hazard.severity, hazard.kind)


def _retention_days() -> dict[str, float]:
    """How long each kind survives the provider going quiet about it.

    Read at call time rather than at import, so a test — or an operator — can
    change the environment without reloading the module.
    """
    return {
        KIND_WILDFIRE: RETENTION_DAYS,
        KIND_AIR: AIR_RETENTION_HOURS / 24.0,
        # An earthquake is over the moment it happens. A day of it on the map
        # is context; a week of it is a map of last week.
        KIND_EARTHQUAKE: 1.0,
        # A warning ends at a stated time and is pruned on that instead — this
        # is only the backstop for one that never named an expiry.
        KIND_SEVERE_WEATHER: 0.5,
        # Cyclones, floods, volcanoes and droughts run for days to months, and
        # GDACS keeps listing them for as long as they do. The retention here
        # is for the gap after it stops.
        KIND_STORM: RETENTION_DAYS,
        KIND_FLOOD: RETENTION_DAYS,
        KIND_VOLCANO: RETENTION_DAYS,
        KIND_DROUGHT: RETENTION_DAYS,
    }


def _prune_expired(db: Session) -> int:
    """Warnings that have passed their own stated end time.

    Runs whether or not NWS answered this poll, and that is the point: an
    expired tornado warning is expired regardless of whether we could reach
    the service, and leaving one drawn because the provider was unreachable
    would be showing a warning that no longer exists.
    """
    now = utcnow()
    doomed = []
    for row in db.scalars(select(Hazard).where(
            Hazard.kind == KIND_SEVERE_WEATHER)):
        when = _iso_ms((row.raw or {}).get("expires"))
        if when is not None and when < now:
            doomed.append(row.id)
    if not doomed:
        return 0
    removed = db.execute(sa_delete(Hazard).where(
        Hazard.id.in_(doomed))).rowcount or 0
    db.commit()
    return removed


def _prune(db: Session, answered: set[str]) -> int:
    """Drop what the provider has stopped listing, and cap the table.

    Scoped to providers that answered this poll: one being down must never
    delete another's rows, and a provider that said nothing keeps everything it
    ever told us until it speaks again.
    """
    if not answered:
        return 0
    removed = 0
    now = utcnow()
    for provider in answered:
        for kind, days in _retention_days().items():
            if days <= 0:
                continue
            removed += db.execute(sa_delete(Hazard).where(
                Hazard.provider == provider, Hazard.kind == kind,
                Hazard.last_seen_at < now - timedelta(days=days))).rowcount or 0

    # The backstop, applied to each provider separately. Keep that provider's
    # worst, since a table this size is read by a map and the severe ones are
    # what anybody is looking for — but never weigh one provider's rows against
    # another's, and never weigh a fire's severity against a station's. Those
    # numbers share a range and mean different things.
    for provider in answered:
        count = db.scalar(select(func.count(Hazard.id)).where(
            Hazard.provider == provider)) or 0
        if count <= MAX_PER_PROVIDER:
            continue
        doomed = db.scalars(
            select(Hazard.id).where(Hazard.provider == provider)
            .order_by(Hazard.severity.asc(), Hazard.last_seen_at.asc())
            .limit(count - MAX_PER_PROVIDER)).all()
        if doomed:
            removed += db.execute(sa_delete(Hazard).where(
                Hazard.id.in_(doomed))).rowcount or 0
            log.warning("hazards: %s is over its cap of %s, dropped %s",
                        provider, MAX_PER_PROVIDER, len(doomed))

    # Nothing is deleted for this one. Per-provider capping bounds the total by
    # construction, so passing this means a provider is writing rows that the
    # cap above is not seeing — a kind nobody prunes, or a provider name that
    # changes between polls. Deleting rows would paper over exactly the bug the
    # number exists to surface.
    total = db.scalar(select(func.count(Hazard.id))) or 0
    if total > MAX_HAZARDS:
        log.error("hazards: %s rows, past the %s tripwire — per-provider caps "
                  "are not holding; check for a provider writing under a name "
                  "that never appears in a poll's answered set", total, MAX_HAZARDS)
    if removed:
        db.commit()
    return removed


# ---------- the fire ring ----------
#
# Fires only. Air quality has no ring and never notifies: it is a continuous
# field with a value everywhere, so a ring around a place holds a dozen
# stations nearly all reading Good, and each would be a message saying nothing
# is wrong. Air is reported *at* the place instead — see main.air_readings.

# The smallest band does not notify. In fire season a 50km ring in California
# would otherwise carry several a day, most of them quarter-acre roadside
# starts that are out by evening, and a notification channel that cries wolf
# daily is one nobody reads on the day it matters.
FIRE_ALERT_MIN_BAND = int(os.environ.get("NEWS_FIRE_ALERT_MIN_BAND", "1"))


def evaluate_fire_ring(db: Session, changed: list[Hazard]) -> list[dict]:
    """Which watched places should hear about which of these fires.

    Runs over what changed, never the whole table — the feed re-reports every
    incident every fifteen minutes, so "is inside the ring" is true of the same
    fires forever and is not on its own a reason to say anything.

    Two guards decide whether a fire that *is* inside the ring is news:
    the unique constraint on (location, hazard), which makes the first sighting
    the only one; and `severity_at_alert`, which lets a fire that grows a band
    speak again. Both live on the HazardHit row.
    """
    fires = [h for h in changed
             if h.kind == KIND_WILDFIRE
             and bucket_of_score(h.severity, h.kind) >= FIRE_ALERT_MIN_BAND]
    if not fires:
        return []
    watchers = db.scalars(select(FavoriteLocation).where(
        FavoriteLocation.fire_km > 0)).all()
    if not watchers:
        return []

    # Every hit already recorded for these places, in one query. Without this
    # the guard would be a lookup per (place, fire) pair on every poll.
    existing = {(h.location_id, h.hazard_id): h for h in db.scalars(
        select(HazardHit).where(
            HazardHit.location_id.in_([w.id for w in watchers])))}

    hits: list[dict] = []
    for loc in watchers:
        for fire in fires:
            km = haversine_km(loc.lat, loc.lon, fire.lat, fire.lon)
            if km > loc.fire_km:
                continue
            band = bucket_of_score(fire.severity, fire.kind)
            seen_before = existing.get((loc.id, fire.id))
            if seen_before is not None:
                # Already told them. Only a worse band earns a second word;
                # the same fire still burning does not.
                if band <= bucket_of_score(seen_before.severity_at_alert, fire.kind):
                    continue
                seen_before.severity_at_alert = fire.severity
                seen_before.distance_km = km
                seen_before.created_at = utcnow()
                seen_before.seen = False
            else:
                db.add(HazardHit(location_id=loc.id, hazard_id=fire.id,
                                 distance_km=km, severity_at_alert=fire.severity))
            hits.append({
                "location_id": loc.id, "location_name": loc.name,
                "user_id": loc.user_id, "pantheon_id": loc.pantheon_id,
                "fire_email": bool(loc.fire_email),
                "hazard_id": fire.id, "name": fire.name,
                "severity": fire.severity, "distance_km": round(km, 1),
                # Where a perimeter has been flown, how far the *edge* is —
                # which is the thing the point distance has always been a
                # stand-in for. None whenever no shape has been published,
                # and 0.0 when the place is inside the fire, which is a
                # sentence worth being able to write plainly.
                "edge_km": (round(edge, 1)
                            if (edge := nearest_edge_km(loc.lat, loc.lon,
                                                        fire.geometry or {})) is not None
                            else None),
                "acres": (fire.raw or {}).get("acres"),
                "containment": (fire.raw or {}).get("containment"),
                "again": seen_before is not None,
            })
    db.commit()
    return hits


def _units_of(user: User) -> str:
    """Kilometres or miles, from the account's own display preferences.

    The reader chose one in Settings and an email is the one place they cannot
    flip a toggle to reinterpret, so it has to arrive in the unit they picked.
    Anything unreadable falls back to km, which is what everything is stored in
    anyway.
    """
    try:
        return "mi" if (json.loads(user.settings or "{}") or {}).get("units") == "mi" else "km"
    except (ValueError, TypeError, AttributeError):
        return "km"


async def deliver(db: Session, hits: list[dict]) -> int:
    """Email whoever asked to be emailed, batched one message per place.

    In-app delivery is the caller's job, the same split `deliver_alerts` uses.
    Never raises: a mail server having a bad afternoon must not stop the poll.
    """
    if not hits:
        return 0
    from collections import defaultdict
    by_place: dict[int, list[dict]] = defaultdict(list)
    for hit in hits:
        if hit.get("fire_email"):
            by_place[hit["location_id"]].append(hit)
    if not by_place or not mailer.enabled():
        return 0

    sent = 0
    for items in by_place.values():
        first = items[0]
        uid = first["user_id"]
        user = db.get(User, int(uid.split(":", 1)[1])) if uid.startswith("acct:") else None
        if not user or not user.email:
            continue
        try:
            # smtplib blocks; keep it off the loop, exactly as alerts do.
            await asyncio.to_thread(mailer.send_hazard_digest,
                                    user.email, first["location_name"], items,
                                    _units_of(user))
            sent += 1
        except Exception:
            log.exception("hazard email delivery failed")
    return sent


def idle_status(reason: str) -> dict:
    """What to report when no poll has run, or none can.

    The shape matches a real poll's, so whatever reads it never has to ask
    whether the key is the one that means "never ran". An absent key was the
    original bug: /api/meta carried ingest.status to every client and simply
    said nothing about hazards, so the client could not distinguish a feature
    that was off from one that was working and quiet.
    """
    return {"ok": False, "enabled": enabled(), "reason": reason,
            "providers": [], "no_key": sorted(_missing_keys())}


def _missing_keys() -> set[str]:
    """Providers that are configured out for want of a credential.

    Reported by name rather than passed over in silence: this is the one
    failure an operator can fix in a minute, and would otherwise never learn
    about — the layer would simply be empty forever.
    """
    missing = set()
    if not os.environ.get("AIRNOW_API_KEY", "").strip():
        missing.add("airnow")
    if not os.environ.get("OPENAQ_API_KEY", "").strip():
        missing.add("openaq")
    if not os.environ.get("PURPLEAIR_API_KEY", "").strip():
        missing.add("purpleair")
    return missing


# What class of instrument each air provider represents, best first. Two tiers,
# and only two, because only one distinction here is real: AirNow and OpenAQ
# both carry **reference-grade monitors** run by agencies, while PurpleAir
# carries **consumer sensors** on people's fences.
#
# Deliberately *not* a ranking of AirNow against OpenAQ. They are the same tier
# and they never meet anyway — AirNow owns the United States and OpenAQ owns
# everywhere else — so ordering them would only decide border cases, and there
# the right answer is the monitor that is actually nearer rather than the
# aggregator this project happens to have listed first.
#
# Used when several are in range of one watched place, and then to pick *which*
# rather than to blend: two scales averaged together is a number neither of
# them made. The community network's value is coverage *between* the good
# instruments, not a rival reading beside one.
AIR_PROVIDER_RANK = {"airnow": 0, "openaq": 0, "purpleair": 1}


async def poll(db: Session) -> dict:
    """One hazard cycle. Never raises into the ingest loop."""
    started = time.monotonic()
    try:
        rows, answered = await _fetch_all()
        if not answered:
            return {**idle_status("no provider answered"),
                    "at": utcnow().isoformat()}
        counts, changed = await asyncio.to_thread(_upsert, db, rows)
        pruned = await asyncio.to_thread(_prune, db, answered)
        pruned += await asyncio.to_thread(_prune_expired, db)
        # Shapes, after the fires exist and after the prune, because a
        # perimeter has nothing to attach to until both have run. Failure here
        # is not failure of the poll: the outlines are decoration on rows that
        # are already correct without them.
        shaped = 0
        if "wfigs" in answered:
            async with safefetch.client(timeout=FETCH_TIMEOUT) as client:
                shapes = await fetch_perimeters(client)
            if shapes:
                shaped = await asyncio.to_thread(attach_perimeters, db, shapes)
        hits = await asyncio.to_thread(evaluate_fire_ring, db, changed)
        # In-app first: it is instant and cannot fail in a way that matters.
        # Email second, and after the hits are committed, so a mail server
        # having a bad afternoon cannot lose a recorded hit.
        for hit in hits:
            broadcaster.publish({"type": "hazard", **hit})
        emailed = await deliver(db, hits)
        return {"ok": True, "enabled": True, "at": utcnow().isoformat(),
                "providers": sorted(answered), "no_key": sorted(_missing_keys()),
                "pruned": pruned, "shaped": shaped,
                "hits": len(hits), "emailed": emailed,
                "seconds": round(time.monotonic() - started, 2), **counts}
    except Exception as exc:                      # never break the news poll
        log.exception("hazard poll failed")
        return {**idle_status(str(exc)), "at": utcnow().isoformat()}
