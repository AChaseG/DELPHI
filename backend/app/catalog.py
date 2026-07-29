"""Seed the source table from the bundled catalog, and demo-article seeding
for offline use."""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import cities
from .models import Source

SOURCES_PATH = Path(__file__).resolve().parent.parent / "data" / "sources.json"


def seed_sources(db: Session) -> int:
    """Insert catalog sources that aren't in the DB yet (idempotent)."""
    with open(SOURCES_PATH, encoding="utf-8") as fh:
        catalog = json.load(fh)
    existing = {u for (u,) in db.execute(select(Source.rss_url)).all()}
    added = 0
    for item in catalog:
        if item["rss_url"] in existing:
            continue
        db.add(Source(**item))
        added += 1
    db.commit()
    return added


def seed_city_sources(db: Session) -> int:
    """Register one local-news source per world city (idempotent).

    These are Google News city-scoped feeds; auto-discovery grows each city's
    real local outlets from them over time. The rolling poller (see ingest)
    refreshes them on a slow per-source interval with per-host pacing so
    hundreds of local feeds don't overwhelm ingestion.
    """
    existing = {u for (u,) in db.execute(select(Source.rss_url)).all()}
    added = 0
    for item in cities.build_city_sources():
        if item["rss_url"] in existing:
            continue
        db.add(Source(**item))
        added += 1
    if added:
        db.commit()
    return added


