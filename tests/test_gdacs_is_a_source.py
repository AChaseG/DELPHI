"""GDACS belongs in the article corpus, and the rule that says so.

Delphi is picking up hazard data, and the first question is where each kind of
it lives. The line drawn is about *mutability*, not subject matter:

    a hazard belongs in the article corpus when it has a stable URL and stops
    changing once published.

A GDACS bulletin passes — it is a news-shaped item about an event, published
once at its own permalink. A wildfire incident record fails: the same federal
feed re-reports the same fire every few minutes with new acreage and new
containment, and Delphi's ingest is append-only and dedupes on `Article.url`.
Those go in a separate table (see the Typhon hazard layer) precisely because
`evaluate_alerts` only ever sees rows inserted in that tick, so an *updated*
row could never fire an alert — and "the fire near your town grew from 200 to
40,000 acres" is the whole reason anyone would want this.

Because GDACS passes that test it costs no code at all: one catalog entry, and
feeds, boolean queries, geofences, watched places, alerts, email, webhooks and
full-text search all work on it the moment it is seeded. This file guards the
entry itself — a data file has no other test — and the reasoning behind its
category and tier.
"""
import json
import pathlib

from sqlalchemy import select

from backend.app.catalog import seed_sources
from backend.app.models import Source

CATALOG = json.loads(
    (pathlib.Path(__file__).resolve().parent.parent / "backend" / "data"
     / "sources.json").read_text(encoding="utf-8"))

GDACS_URL = "https://www.gdacs.org/xml/rss.xml"


def _gdacs() -> dict:
    return next(s for s in CATALOG if s["rss_url"] == GDACS_URL)


# ---------- the entry ----------

def test_gdacs_is_in_the_catalog():
    entry = _gdacs()
    assert entry["name"] == "GDACS — Global Disaster Alerts"
    assert entry["scope"] == "international"
    assert entry["country"] == "", "it is not any one country's feed"


def test_it_is_filed_as_a_disaster_feed():
    assert _gdacs()["categories"] == ["disaster"]


def test_it_is_weighted_low_on_purpose():
    """Tier feeds the importance score. GDACS posts a great many green
    (low-severity) alerts, and it is 100% disaster — at wire tier it would
    crowd Home's conflict-and-disasters column with quarter-severity events
    and push actual wire copy off the front."""
    assert _gdacs()["tier"] == 3


def test_every_catalog_url_is_still_unique():
    """seed_sources skips on rss_url, so a duplicate would silently mean one
    of the two entries never exists."""
    urls = [s["rss_url"] for s in CATALOG]
    assert len(urls) == len(set(urls))


def test_every_catalog_key_is_a_real_source_column():
    """`Source(**item)` — an unexpected key is a TypeError at startup, and
    startup is where the catalog is read. Entries are not all the same shape
    (the social feeds carry a `platform` the press feeds have no use for), so
    what has to hold is that every key names a column, not that every entry
    names every key."""
    columns = set(Source.__table__.columns.keys())
    for entry in CATALOG:
        assert set(entry) <= columns, (entry["name"], set(entry) - columns)


# ---------- and it actually seeds ----------

def test_seeding_creates_it(client, db):
    seed_sources(db)
    source = db.scalar(select(Source).where(Source.rss_url == GDACS_URL))
    assert source is not None
    assert source.enabled is True
    assert source.added_by == "catalog"


def test_seeding_twice_does_not_double_it(client, db):
    seed_sources(db)
    seed_sources(db)
    rows = db.scalars(select(Source).where(Source.rss_url == GDACS_URL)).all()
    assert len(rows) == 1
