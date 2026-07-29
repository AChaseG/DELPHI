"""Geotagging and geofence matching.

Articles are geotagged by matching country/city names from a bundled gazetteer
against the title + summary. Feeds and alerts can carry a `geo` criterion drawn
on the dashboard map:

  - GeoJSON Polygon / MultiPolygon (coordinates are [lon, lat], GeoJSON order)
  - {"type": "Circle", "center": [lat, lon], "radius_km": <float>}

An article matches a geofence when any of its tagged places falls inside it.
"""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path

GAZETTEER_PATH = Path(__file__).resolve().parent.parent / "data" / "gazetteer.json"

# Aliases this short are matched case-sensitively (avoids "us"/"in" false hits).
_CASE_SENSITIVE_MAX_LEN = 3

# CJK/Thai have no ASCII word boundaries, so \b never matches — those aliases
# (e.g. 東京, 서울) are matched as substrings instead.
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿가-힣฀-๿]")
# Cyrillic place names decline heavily (Москва → в Москве), so match the stem
# plus any case ending rather than the exact nominative.
_CYR_RE = re.compile(r"[а-яёіїєґ]", re.IGNORECASE)
_CYR_ENDING_VOWELS = "аяоеыиуюьйії"


@lru_cache(maxsize=1)
def load_gazetteer() -> dict:
    with open(GAZETTEER_PATH, encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def _merged_cities() -> list[dict]:
    """Gazetteer cities plus every source-catalog city not already present.

    Catalog cities that lack exact coordinates are placed at their country's
    centroid — approximate for a map marker, but always country-correct for
    geofencing — so all ~500 catalog cities become geotaggable. Native-script
    aliases (cities.CITY_ALIASES) are attached so non-English coverage matches.
    """
    from . import cities as _cities
    gaz = load_gazetteer()
    countries = {c["iso2"]: c for c in gaz["countries"]}
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for city in gaz.get("cities", []):
        seen.add((city["name"].lower(), city["country"]))
        out.append({**city,
                    "aliases": list(city.get("aliases", []))
                    + _cities.CITY_ALIASES.get(city["name"], [])})
    for name, cc in _cities.catalog_cities():
        key = (name.lower(), cc)
        if key in seen:
            continue
        centroid = countries.get(cc)
        if not centroid:
            continue
        seen.add(key)
        out.append({"name": name, "country": cc,
                    "lat": centroid["lat"], "lon": centroid["lon"],
                    "aliases": _cities.CITY_ALIASES.get(name, [])})
    return out


@lru_cache(maxsize=1)
def _compiled_places() -> list[tuple[re.Pattern, dict]]:
    """[(regex, place)] where place = {name, country, lat, lon, kind}."""
    gaz = load_gazetteer()
    compiled: list[tuple[re.Pattern, dict]] = []

    def add(names: list[str], place: dict):
        for n in names:
            if not n:
                continue
            if _CJK_RE.search(n):
                # CJK/Thai: no word boundaries — match as a substring.
                pattern = re.compile(re.escape(n))
            elif _CYR_RE.search(n) and len(n) >= 4:
                # Slavic declension: strip a trailing vowel and allow any ending.
                stem = n[:-1] if n[-1].lower() in _CYR_ENDING_VOWELS else n
                pattern = re.compile(r"\b" + re.escape(stem) + r"[а-яёіїєґ]*", re.IGNORECASE)
            else:
                flags = 0 if len(n) <= _CASE_SENSITIVE_MAX_LEN else re.IGNORECASE
                pattern = re.compile(r"\b" + re.escape(n) + r"\b", flags)
            compiled.append((pattern, place))

    for c in gaz["countries"]:
        place = {
            "name": c["name"],
            "country": c["iso2"],
            "lat": c["lat"],
            "lon": c["lon"],
            "kind": "country",
        }
        add([c["name"], *c.get("aliases", [])], place)

    for city in _merged_cities():
        place = {
            "name": city["name"],
            "country": city["country"],
            "lat": city["lat"],
            "lon": city["lon"],
            "kind": "city",
        }
        add([city["name"], *city.get("aliases", [])], place)

    return compiled


@lru_cache(maxsize=1)
def countries_index() -> dict[str, dict]:
    return {c["iso2"]: c for c in load_gazetteer()["countries"]}


def extract_places(text: str, max_places: int = 8) -> list[dict]:
    """Extract distinct known places mentioned in text."""
    found: dict[str, dict] = {}
    for pattern, place in _compiled_places():
        if place["name"] in found:
            continue
        if pattern.search(text):
            found[place["name"]] = place
            if len(found) >= max_places:
                break
    return list(found.values())


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _point_in_ring(lat: float, lon: float, ring: list) -> bool:
    """Ray casting; ring is a GeoJSON linear ring of [lon, lat] pairs."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def _point_in_polygon(lat: float, lon: float, coordinates: list) -> bool:
    """GeoJSON Polygon: [exterior_ring, hole1, ...]."""
    if not coordinates:
        return False
    if not _point_in_ring(lat, lon, coordinates[0]):
        return False
    for hole in coordinates[1:]:
        if _point_in_ring(lat, lon, hole):
            return False
    return True


def point_in_geo(lat: float, lon: float, geo: dict) -> bool:
    gtype = (geo or {}).get("type", "")
    if gtype == "Circle":
        center = geo.get("center", [0, 0])
        radius = float(geo.get("radius_km", 0))
        return haversine_km(lat, lon, center[0], center[1]) <= radius
    if gtype == "Polygon":
        return _point_in_polygon(lat, lon, geo.get("coordinates", []))
    if gtype == "MultiPolygon":
        return any(_point_in_polygon(lat, lon, poly) for poly in geo.get("coordinates", []))
    if gtype == "Feature":
        return point_in_geo(lat, lon, geo.get("geometry", {}))
    if gtype in ("FeatureCollection", "GeometryCollection"):
        parts = geo.get("features") or geo.get("geometries") or []
        return any(point_in_geo(lat, lon, part) for part in parts)
    return False


def search_places(query: str, limit: int = 12) -> list[dict]:
    """Look up a place name in the built-in gazetteer.

    Deliberately local: no third-party geocoder is called, so adding a favourite
    location works offline, costs nothing, needs no API key, and sends the
    user's places of interest nowhere. The trade-off is coverage — roughly 480
    cities and 154 countries — so the map also accepts a dropped pin for
    anywhere that isn't listed.

    Exact matches first, then prefix, then substring; aliases (native-script
    names) are searched too.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    exact, prefix, contains = [], [], []
    for city in _merged_cities():
        names = [city["name"]] + list(city.get("aliases") or [])
        low = [n.lower() for n in names]
        hit = {"name": city["name"], "country": city.get("country", ""),
               "lat": city["lat"], "lon": city["lon"], "kind": "city"}
        if any(n == q for n in low):
            exact.append(hit)
        elif any(n.startswith(q) for n in low):
            prefix.append(hit)
        elif any(q in n for n in low):
            contains.append(hit)
    for iso, c in countries_index().items():
        name = c.get("name", "")
        low = name.lower()
        hit = {"name": name, "country": iso, "lat": c["lat"], "lon": c["lon"],
               "kind": "country"}
        if low == q:
            exact.append(hit)
        elif low.startswith(q):
            prefix.append(hit)
        elif q in low:
            contains.append(hit)
    return (exact + prefix + contains)[:limit]


def places_match_geo(places: list[dict], country: str, geo: dict) -> bool:
    """True if any tagged place (or the article's country centroid) is inside geo."""
    for p in places or []:
        if point_in_geo(p.get("lat", 0), p.get("lon", 0), geo):
            return True
    if not places and country:
        c = countries_index().get(country)
        if c and point_in_geo(c["lat"], c["lon"], geo):
            return True
    return False
