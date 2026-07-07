"""Seed the source table from the bundled catalog, and demo-article seeding
for offline use."""
from __future__ import annotations

import json
import random
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .geo import extract_places
from .models import Article, Source, utcnow
from .scoring import classify_categories, cluster_tokens, score_importance

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


DEMO_HEADLINES = [
    ("Magnitude 6.8 earthquake strikes off the coast of Japan, tsunami advisory issued",
     "A powerful earthquake struck near Honshu, Japan on Monday. Authorities in Tokyo issued a tsunami advisory for coastal prefectures as emergency crews assessed damage.", "JP"),
    ("EU leaders reach provisional deal on AI liability rules at Brussels summit",
     "European Union negotiators in Brussels agreed on a framework assigning liability for harms caused by artificial intelligence systems, a milestone for technology regulation in Europe.", "BE"),
    ("Wildfire forces evacuations in northern California as heatwave intensifies",
     "Thousands of residents near Redding, California were ordered to evacuate as a fast-moving wildfire spread. A state of emergency was declared amid record temperatures.", "US"),
    ("Central bank of Brazil surprises markets with rate cut amid cooling inflation",
     "Brazil's central bank lowered its benchmark rate, citing cooling inflation. Economists in São Paulo said the move could accelerate economic recovery across Latin America.", "BR"),
    ("Ceasefire talks resume as humanitarian convoy reaches besieged city",
     "Negotiators resumed ceasefire talks on Monday while a United Nations humanitarian convoy delivered aid. Casualties have mounted in the conflict over recent weeks.", ""),
    ("Breaking: Explosion reported near port district, casualties feared",
     "An explosion was reported near the port district late on Sunday. Emergency services deployed and a state of emergency is under consideration, officials said.", ""),
    ("Semiconductor giant announces $40bn investment in new chip fabrication plants",
     "The company said the investment would fund chip fabrication facilities, intensifying competition in the global semiconductor supply chain between the United States and Taiwan.", "TW"),
    ("Kenya launches largest solar power project in East Africa",
     "The renewable energy facility outside Nairobi is expected to power one million homes, part of Kenya's push to expand clean energy and cut carbon emissions.", "KE"),
    ("Cyberattack disrupts hospital systems across several regions",
     "A ransomware cyberattack disrupted hospital scheduling and records systems. Health officials said patient safety was maintained while cybersecurity teams responded to the data breach.", ""),
    ("Historic election sees record turnout as opposition claims narrow victory",
     "Preliminary results from the parliament election suggest a narrow opposition victory with record turnout. International observers called the vote largely free and fair.", ""),
    ("Monsoon floods displace thousands in Bangladesh, relief operations underway",
     "Severe flooding following monsoon rains displaced tens of thousands near Dhaka, Bangladesh. Relief agencies launched evacuation and aid operations across low-lying districts.", "BD"),
    ("Champions League final ends in dramatic penalty shootout",
     "The match in Madrid went to a penalty shootout after extra time. The tournament final drew a record global audience, the league said.", "ES"),
    ("New study finds Antarctic ice sheet melting faster than projected",
     "Scientists reported that the Antarctic ice sheet is losing mass faster than climate models projected, with implications for sea level rise and global warming targets.", ""),
    ("Oil prices jump after supply disruption in key shipping corridor",
     "Crude prices rose sharply after a supply disruption in a critical shipping corridor. Analysts warned of knock-on effects for inflation and the global economy.", ""),
    ("Protests erupt in Paris over pension reform legislation",
     "Demonstrators gathered across Paris, France to protest pension reform legislation. Unions called a general strike as parliament debated the bill.", "FR"),
    ("Tech startup in Bangalore raises record funding for language AI",
     "The Bangalore-based startup raised one of India's largest funding rounds for artificial intelligence focused on Indian languages, investors said.", "IN"),
    ("Volcanic eruption in Iceland prompts flight diversions",
     "A volcanic eruption on the Reykjanes peninsula in Iceland sent ash plumes skyward, prompting airlines to divert some transatlantic flights as a precaution.", "IS"),
    ("Drought emergency declared as reservoir levels hit historic lows",
     "Authorities declared a drought emergency as reservoirs fell to historic lows, imposing water restrictions on agriculture and urging conservation amid the environment crisis.", ""),
    ("Major museum returns looted artifacts in landmark heritage agreement",
     "The museum in London agreed to return artifacts in a landmark cultural heritage deal that experts say could set a precedent for restitution claims worldwide.", "GB"),
    ("Local council approves downtown transit expansion after marathon session",
     "The city council voted to approve the downtown transit expansion following a marathon public session, with construction expected to begin next year.", "US"),
]

# (title, summary, country, language) — cross-source variants of the same story
# (they cluster into one event) and non-English items (they exercise translation).
DEMO_EXTRA = [
    ("Powerful magnitude 6.8 earthquake strikes off Japan coast, tsunami advisory in effect",
     "A magnitude 6.8 earthquake struck off the coast of Japan on Monday, prompting a tsunami advisory as officials in Tokyo assessed the damage.", "JP", "en"),
    ("Japan earthquake: 6.8 magnitude quake off coast triggers tsunami advisory",
     "Authorities issued a tsunami advisory after a 6.8 magnitude earthquake struck off Japan's coast. No major damage was immediately reported.", "JP", "en"),
    ("Grève des transports à Paris : le trafic du métro fortement perturbé lundi",
     "Les syndicats ont appelé à une grève des transports à Paris. Le trafic du métro sera fortement perturbé, ont annoncé les autorités.", "FR", "fr"),
    ("El Gobierno de España aprueba un nuevo paquete de ayudas a la vivienda",
     "El Consejo de Ministros aprobó un paquete de ayudas destinado a facilitar el acceso a la vivienda de los jóvenes en España.", "ES", "es"),
]


def seed_demo_articles(db: Session) -> int:
    """Populate sample articles (for offline demo / first-run experience)."""
    count = db.scalar(select(func.count(Article.id))) or 0
    sources = db.scalars(select(Source).where(Source.enabled.is_(True))).all()
    if not sources:
        return 0
    rng = random.Random(42 + count)
    now = utcnow()
    added = 0
    entries = [(t, s, c, "en") for (t, s, c) in DEMO_HEADLINES] + DEMO_EXTRA
    for i, (title, summary, country_hint, lang) in enumerate(entries):
        source = rng.choice(sources)
        text = f"{title}\n{summary}"
        places = extract_places(text)
        country = country_hint or (places[0]["country"] if places else source.country)
        url = f"https://example.org/demo/{now.timestamp():.0f}/{i}"
        db.add(Article(
            source_id=source.id,
            url=url,
            title=title,
            summary=summary,
            published_at=now - timedelta(minutes=rng.randint(2, 2400)),
            language=lang,
            country=country or "",
            categories=classify_categories(text, source.categories),
            places=places,
            importance=score_importance(text, source.scope, source.tier, places,
                                        corroborating_sources=rng.choice([0, 0, 1, 2])),
            cluster_tokens=cluster_tokens(title),
        ))
        added += 1
    db.commit()
    return added
