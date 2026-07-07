"""Importance scoring (0-100) and auto-categorization.

Importance blends:
  - the source's reach (scope + tier),
  - breaking-news signal terms in the headline/summary,
  - geographic breadth (multiple countries implicated),
  - cross-source corroboration (similar headlines from distinct outlets).

Tiers used by the UI: >=80 Critical, 60-79 High, 40-59 Notable, <40 Routine.
"""
from __future__ import annotations

import re

_SCOPE_BASE = {"international": 45, "national": 35, "local": 25}

# term -> weight. Matched on word boundaries, case-insensitive.
BREAKING_TERMS: dict[str, int] = {
    "breaking": 14, "urgent": 12, "live updates": 10,
    "war": 10, "invasion": 12, "airstrike": 12, "air strike": 12, "missile": 10,
    "explosion": 12, "blast": 10, "attack": 9, "terror": 10, "shooting": 10,
    "hostage": 10, "coup": 14, "uprising": 10, "martial law": 14,
    "earthquake": 12, "tsunami": 14, "hurricane": 10, "typhoon": 10,
    "cyclone": 9, "wildfire": 8, "flood": 8, "eruption": 10, "landslide": 8,
    "outbreak": 10, "pandemic": 12, "epidemic": 10, "recall": 5,
    "killed": 8, "dead": 7, "death toll": 12, "casualties": 9, "injured": 5,
    "evacuation": 8, "evacuate": 8, "state of emergency": 14, "emergency": 6,
    "sanctions": 7, "ceasefire": 9, "nuclear": 9, "cyberattack": 10,
    "data breach": 8, "assassination": 14, "impeach": 9, "indicted": 8,
    "resigns": 7, "resignation": 7, "election": 5, "referendum": 6,
    "default": 5, "bailout": 7, "crash": 8, "collapse": 8, "bankruptcy": 6,
    "rate cut": 5, "rate hike": 5, "inflation": 4, "recession": 7,
    "summit": 4, "treaty": 5, "crisis": 7, "protest": 5, "strike": 4,
}

_BREAKING_RES = [(re.compile(r"\b" + re.escape(t) + r"\b", re.IGNORECASE), w)
                 for t, w in BREAKING_TERMS.items()]

STANDARD_CATEGORIES = [
    "world", "politics", "business", "economy", "technology", "science",
    "health", "environment", "conflict", "disaster", "crime", "sports",
    "entertainment", "culture",
]

_CATEGORY_HINTS: dict[str, list[str]] = {
    "politics": ["election", "parliament", "senate", "congress", "president",
                 "minister", "policy", "vote", "campaign", "legislation", "government"],
    "business": ["company", "merger", "acquisition", "startup", "earnings",
                 "revenue", "ipo", "shares", "stock", "profit", "ceo"],
    "economy": ["inflation", "gdp", "recession", "central bank", "interest rate",
                "unemployment", "economy", "trade deficit", "tariff"],
    "technology": ["ai", "artificial intelligence", "software", "chip", "semiconductor",
                   "smartphone", "cyber", "robot", "app", "internet", "data breach", "crypto"],
    "science": ["research", "study finds", "scientists", "space", "nasa", "telescope",
                "physics", "genome", "discovery", "quantum"],
    "health": ["health", "hospital", "vaccine", "virus", "disease", "outbreak",
               "cancer", "drug", "who", "epidemic", "mental health"],
    "environment": ["climate", "emissions", "warming", "biodiversity", "pollution",
                    "renewable", "deforestation", "carbon", "drought"],
    "conflict": ["war", "military", "troops", "airstrike", "missile", "ceasefire",
                 "invasion", "insurgent", "front line", "offensive"],
    "disaster": ["earthquake", "flood", "hurricane", "wildfire", "tsunami", "typhoon",
                 "eruption", "landslide", "storm", "evacuation"],
    "crime": ["police", "arrested", "murder", "fraud", "trafficking", "trial",
              "sentenced", "theft", "shooting", "court"],
    "sports": ["match", "tournament", "league", "cup", "olympic", "championship",
               "goal", "coach", "player", "fifa", "grand slam"],
    "entertainment": ["film", "movie", "album", "celebrity", "box office", "netflix",
                      "festival", "actor", "singer", "premiere"],
    "culture": ["museum", "heritage", "art exhibition", "literature", "novel", "opera"],
}

_CATEGORY_RES = {
    cat: [re.compile(r"\b" + re.escape(h) + r"\b", re.IGNORECASE) for h in hints]
    for cat, hints in _CATEGORY_HINTS.items()
}

_STOPWORDS = frozenset(
    "a an the of to in on at for with and or but as is are was were be been from by "
    "that this it its his her their our your after before over under into out up down "
    "new says said say will would could may might amid us he she they we not no more".split()
)


def breaking_signal(text: str) -> int:
    score = 0
    for pattern, weight in _BREAKING_RES:
        if pattern.search(text):
            score += weight
    return min(score, 30)


def classify_categories(text: str, source_categories: list[str] | None = None) -> list[str]:
    cats = set(c for c in (source_categories or []) if c in STANDARD_CATEGORIES)
    for cat, patterns in _CATEGORY_RES.items():
        hits = sum(1 for p in patterns if p.search(text))
        if hits >= 2 or (hits == 1 and cat in ("disaster", "conflict", "sports")):
            cats.add(cat)
    if not cats:
        cats.add("world")
    return sorted(cats)


def cluster_tokens(title: str) -> str:
    """Normalized significant tokens of a headline, for corroboration grouping."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    sig = [w for w in words if len(w) > 2 and w not in _STOPWORDS]
    return " ".join(sorted(set(sig))[:12])


def tokens_similarity(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def score_importance(
    text: str,
    scope: str,
    tier: int,
    places: list[dict],
    corroborating_sources: int = 0,
) -> int:
    score = _SCOPE_BASE.get(scope, 30)
    if tier == 1:
        score += 8
    elif tier == 3:
        score -= 4
    score += breaking_signal(text)
    countries = {p.get("country") for p in places if p.get("country")}
    if len(countries) >= 2:
        score += 6
    score += min(corroborating_sources * 8, 24)
    return max(5, min(100, score))
