"""Build a synthetic archive so timings mean something.

Delphi is fast on a thousand articles and slow on a quarter of a million, and
only the second number is worth optimizing against. This writes a corpus the
shape of a real one — a month of retention, hundreds of outlets, headlines that
share words the way news does — into a throwaway database.

It is deterministic: the same arguments produce the same corpus, on any machine
and any day, so yesterday's measurements and today's are comparable.

    python bench/corpus.py --db /tmp/bench.db --articles 120000
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import timedelta

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--db", required=True, help="database file to create or fill")
parser.add_argument("--articles", type=int, default=120_000)
parser.add_argument("--sources", type=int, default=600)
args = parser.parse_args()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["NEWS_DB_PATH"] = args.db

from backend.app.database import Base, SessionLocal, engine  # noqa: E402
from backend.app.main import _ensure_schema  # noqa: E402
from backend.app.models import Article, Event, Source, utcnow  # noqa: E402

# Vocabulary wide enough that an index over words can do its job. A corpus of a
# dozen words makes every headline a near-match for every other, which flatters
# an exhaustive scan and slanders an index — the opposite of what real
# headlines, with their long tail of proper nouns, do.
SUBJECTS = """earthquake election strike flood summit outage protest merger vaccine
satellite drought ceasefire inflation wildfire blackout tariff verdict sanctions
recall shortage eruption typhoon blizzard derailment lockdown recount indictment
airstrike blockade referendum impeachment evacuation quarantine bailout layoffs
harvest census strikes ruling treaty accord embargo
""".split()
PLACES = """Tokyo Cairo Lagos Lima Berlin Mumbai Sydney Oslo Nairobi Bogota Warsaw
Manila Dhaka Tunis Quito Ankara Hanoi Lusaka Riga Doha Osaka Toronto Santiago
Jakarta Nicosia Tbilisi Dakar Amman Maputo Helsinki Vilnius Kigali Muscat
""".split()
ACTORS = """ministers regulators inspectors negotiators prosecutors engineers
farmers airlines hospitals universities unions insurers exporters brewers miners
carriers refiners auditors councils
""".split()
VERBS = """halts delays widens narrows resumes suspends approves rejects doubles
reopens investigates warns confirms disputes postpones expands
""".split()
CATEGORIES = ["world", "politics", "conflict", "disaster", "business",
              "technology", "science", "economy", "sports", "culture"]
COUNTRIES = ["US", "JP", "NG", "DE", "BR", "GB", "FR", "IN", "ZA", ""]
LANGUAGES = ["en"] * 8 + ["fr", "es"]

rng = random.Random(20260801)      # the day this corpus was defined

Base.metadata.create_all(engine)
_ensure_schema()

with SessionLocal() as db:
    if db.query(Article).count():
        print(f"{args.db} already has articles — leaving it alone")
        raise SystemExit(0)

    if not db.query(Source).count():
        for i in range(args.sources):
            db.add(Source(
                name=f"Outlet {i}", rss_url=f"http://stub.local/{i}",
                homepage="http://stub.local", country=rng.choice(COUNTRIES) or "US",
                region="Global", language=rng.choice(LANGUAGES),
                scope=rng.choice(["local", "national", "international"]),
                platform=rng.choice(["news"] * 9 + ["reddit"]),
                categories=[], tier=rng.choice([1, 2, 3]), added_by="catalog"))
        db.commit()
    source_ids = [s for (s,) in db.query(Source.id).all()]

    # A quarter as many events as articles: most stories are carried by a few
    # outlets, which is what makes clustering work at all.
    now = utcnow()
    events = []
    for i in range(args.articles // 4):
        tokens = rng.sample(SUBJECTS, 2) + [rng.choice(PLACES).lower()]
        events.append(Event(
            title=f"Event {i}", cluster_tokens=" ".join(sorted(tokens)),
            importance=rng.randint(20, 95), article_count=4,
            countries=[rng.choice(COUNTRIES) or "US"],
            categories=[rng.choice(CATEGORIES)],
            first_seen=now - timedelta(hours=rng.randint(0, 700)),
            updated_at=now - timedelta(hours=rng.randint(0, 700))))
    db.add_all(events)
    db.commit()
    event_ids = [e.id for e in events]

    batch = []
    for i in range(args.articles):
        when = now - timedelta(minutes=rng.randint(0, 43_200))     # 30 days
        place = rng.choice(PLACES)
        subject = rng.choice(SUBJECTS)
        body = " ".join(rng.choices(SUBJECTS + ACTORS + VERBS + PLACES, k=120))
        batch.append(Article(
            source_id=rng.choice(source_ids),
            guid=str(i), url=f"http://stub.local/a/{i}",
            title=f"{subject.title()} in {place}: {rng.choice(ACTORS)} "
                  f"{rng.choice(VERBS)} as {rng.randint(2, 99)} affected",
            summary=f"Officials in {place} responded to the {subject}. " + body[:200],
            content=body,
            image_url="", published_at=when, fetched_at=when,
            language=rng.choice(LANGUAGES), country=rng.choice(COUNTRIES),
            categories=rng.sample(CATEGORIES, rng.randint(1, 2)),
            places=[{"name": place, "country": "JP", "lat": 35.6, "lon": 139.7}],
            importance=rng.randint(10, 95),
            cluster_tokens=" ".join(sorted(rng.sample(SUBJECTS, 2) + [place.lower()])),
            event_id=rng.choice(event_ids)))
        if len(batch) >= 2000:
            db.add_all(batch)
            db.commit()
            batch = []
    if batch:
        db.add_all(batch)
        db.commit()

    print(f"articles={db.query(Article).count()} events={db.query(Event).count()} "
          f"sources={db.query(Source).count()} "
          f"size={os.path.getsize(args.db) / 1e6:.0f}MB")
