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


@lru_cache(maxsize=1)
def load_gazetteer() -> dict:
    with open(GAZETTEER_PATH, encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def _compiled_places() -> list[tuple[re.Pattern, dict]]:
    """[(regex, place)] where place = {name, country, lat, lon, kind}."""
    gaz = load_gazetteer()
    compiled: list[tuple[re.Pattern, dict]] = []

    def add(names: list[str], place: dict):
        for n in names:
            if not n:
                continue
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

    for city in gaz.get("cities", []):
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
