"""Importance scoring (0-100) and auto-categorization.

Importance blends:
  - the source's reach (scope + tier),
  - breaking-news signal terms in the headline/summary,
  - geographic breadth (multiple countries implicated),
  - cross-source corroboration (similar headlines from distinct outlets).

Tiers used by the UI: >=80 Critical, 60-79 High, 40-59 Notable, <40 Routine.

That score is what a story was worth when it arrived. What it is worth now is
`aged_importance`, which walks it down a half-life curve as the story goes
quiet — see the decay section at the foot of this module.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

from .intl_terms import BREAKING_INTL, CATEGORY_INTL, TAG_SYNONYMS

# Where an article starts before anything is known about the story itself.
#
# 32 points of the score are decided by the outlet: 45/35/25 by scope, plus 8
# for a major wire or minus 4 for a niche or local feed. That is more than the
# entire breaking-signal range (30), and it is meant to be — who is carrying
# something is the strongest single thing Delphi knows about an article before
# reading it, and a catalog that grows by itself fills up with outlets nobody
# chose.
#
# This has been both ways, and the history is worth keeping because both
# failures are real. It was narrowed to 42/38/34 with +4/−3 — 15 points — to
# stop the wires owning Home: "Top stories" asks for 55, a wire's most routine
# item scored 53, and a city outlet's report of two people dead in an explosion
# scored 40, so the headline columns showed the same thirty mastheads however
# many sources were added underneath them.
#
# Narrowing it fixed that and cost something else. With reach worth 15 points a
# discovered blog's routine post lands within a few points of a wire's, so an
# importance floor — the thing every feed and alert filters on — stopped being
# able to tell "an outlet worth reading has this" from "something on the
# internet has this", and feeds admitted material their owner did not want.
# Restored at the operator's request: a filter that discriminates is worth more
# here than an evenly-spread front page.
#
# What to watch, since it is the same trade as before: routine wire copy sits
# 2 points under Home's "Top stories" threshold and any breaking word closes
# that gap, while a city feed's solo breaking story tops out at 47 and cannot
# reach it. Corroboration (+8 each, to +24) is what carries a local story up.
_SCOPE_BASE = {"international": 45, "national": 35, "local": 25}
_TIER_ADJUST = {1: 8, 3: -4}

# CJK/Thai have no ASCII word boundaries, so \b matching fails on them —
# those terms are matched as substrings instead.
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿가-힣฀-๿]")


def _make_matcher(term: str):
    """A predicate(text) -> bool for one term: word-boundary regex for
    space-delimited scripts, substring for CJK/Thai."""
    if _CJK_RE.search(term):
        lt = term.lower()
        return lambda s: lt in s.lower()
    pat = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
    return lambda s: bool(pat.search(s))

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

# English (word-boundary) + multilingual (CJK-aware) breaking-signal matchers.
_BREAKING_MATCHERS = [(_make_matcher(t), w)
                      for t, w in {**BREAKING_TERMS, **BREAKING_INTL}.items()]

STANDARD_CATEGORIES = [
    "world", "politics", "business", "economy", "technology", "science",
    "health", "environment", "conflict", "disaster", "crime", "sports",
    "entertainment", "culture",
]

_CATEGORY_HINTS: dict[str, list[str]] = {
    "politics": ["election", "parliament", "senate", "congress", "president",
                 "minister", "policy", "vote", "campaign", "legislation", "government",
                 "prime minister", "governor", "mayor", "coalition", "diplomat",
                 "sanctions", "bill", "lawmakers", "opposition", "cabinet", "white house"],
    "business": ["company", "merger", "acquisition", "startup", "earnings",
                 "revenue", "ipo", "shares", "stock", "profit", "ceo",
                 "investors", "billion", "deal", "shareholders", "layoffs", "retailer"],
    "economy": ["inflation", "gdp", "recession", "central bank", "interest rate",
                "unemployment", "economy", "trade deficit", "tariff",
                "economic", "federal reserve", "jobs report", "consumer prices", "exports"],
    "technology": ["ai", "artificial intelligence", "software", "chip", "semiconductor",
                   "smartphone", "cyber", "robot", "app", "internet", "data breach", "crypto",
                   "tech", "startup", "google", "apple", "microsoft", "meta", "hacker"],
    "science": ["research", "study finds", "scientists", "space", "nasa", "telescope",
                "physics", "genome", "discovery", "quantum",
                "researchers", "astronomers", "spacecraft", "fossil", "experiment"],
    "health": ["health", "hospital", "vaccine", "virus", "disease", "outbreak",
               "cancer", "drug", "who", "epidemic", "mental health",
               "patients", "doctors", "fda", "medical", "surgery", "obesity"],
    "environment": ["climate", "emissions", "warming", "biodiversity", "pollution",
                    "renewable", "deforestation", "carbon", "drought",
                    "wildlife", "conservation", "heatwave", "climate change", "solar", "wind farm"],
    "conflict": ["war", "military", "troops", "airstrike", "missile", "ceasefire",
                 "invasion", "insurgent", "front line", "offensive",
                 "shelling", "drone strike", "hostilities", "armed group"],
    "disaster": ["earthquake", "flood", "hurricane", "wildfire", "tsunami", "typhoon",
                 "eruption", "landslide", "storm", "evacuation",
                 "cyclone", "tornado", "avalanche", "heat wave"],
    "crime": ["police", "arrested", "murder", "fraud", "trafficking", "trial",
              "sentenced", "theft", "shooting", "court",
              "homicide", "kidnapping", "smuggling", "indictment", "manhunt"],
    "sports": ["match", "tournament", "league", "cup", "olympic", "championship",
               "goal", "coach", "player", "fifa", "grand slam",
               "season", "playoffs", "transfer", "medal", "grand prix"],
    "entertainment": ["film", "movie", "album", "celebrity", "box office", "netflix",
                      "festival", "actor", "singer", "premiere",
                      "trailer", "hollywood", "series", "concert", "streaming"],
    "culture": ["museum", "heritage", "art exhibition", "literature", "novel", "opera",
                "gallery", "archaeologists", "unesco", "manuscript"],
}

_CATEGORY_MATCHERS = {
    cat: [_make_matcher(h) for h in hints + CATEGORY_INTL.get(cat, [])]
    for cat, hints in _CATEGORY_HINTS.items()
}

_STOPWORDS = frozenset(
    "a an the of to in on at for with and or but as is are was were be been from by "
    "that this it its his her their our your after before over under into out up down "
    "new says said say will would could may might amid us he she they we not no more".split()
)


def breaking_signal(text: str) -> int:
    score = 0
    for match, weight in _BREAKING_MATCHERS:
        if match(text):
            score += weight
    return min(score, 30)


def _tags_to_categories(entry_tags: list[str] | None) -> set[str]:
    """Map an RSS item's own section tags to our categories — high precision,
    language-agnostic where the outlet's section name is recognized."""
    out: set[str] = set()
    for tag in entry_tags or []:
        cat = TAG_SYNONYMS.get((tag or "").strip().lower())
        if cat:
            out.add(cat)
    return out


def classify_categories(text: str, source_categories: list[str] | None = None,
                        entry_tags: list[str] | None = None) -> list[str]:
    """Assign standard categories from the outlet's own RSS section tags plus
    headline + summary keywords (English and a multilingual core, so foreign
    articles don't all fall into "world").

    A hit in the headline (first line of `text`) counts double: headlines are
    short and deliberate, so one strong keyword there is a reliable signal,
    while body text needs corroboration. Many feeds ship little or no summary,
    so requiring two hits overall would leave most articles category-less.
    """
    title, _, body = text.partition("\n")
    cats = set(c for c in (source_categories or []) if c in STANDARD_CATEGORIES)
    cats |= _tags_to_categories(entry_tags)
    for cat, matchers in _CATEGORY_MATCHERS.items():
        score = sum(2 if m(title) else (1 if m(body) else 0) for m in matchers)
        if score >= 2 or (score >= 1 and cat in ("disaster", "conflict", "sports")):
            cats.add(cat)
    if not cats:
        cats.add("world")
    return sorted(cats)


# Complete short numbers (not embedded in longer digit runs), independent of
# ASCII word boundaries — \b fails against CJK, so "マグニチュード6.8" wouldn't match.
_NUM_RE = re.compile(r"(?<!\d)\d{1,4}(?:[.,]\d+)?(?!\d)")


def invariant_anchors(text: str, places: list[dict] | None = None) -> list[str]:
    """Language-invariant tokens for cross-language clustering: canonical city
    names (from geotagging, identical across scripts) and significant numbers.
    A Japanese and an English report of the same earthquake share e.g.
    {"tokyo", "6.8"} even though their headline words don't. Excluded to keep
    precision: countries (too broad), bare 1-digit and 4-digit numbers (years/
    ids are common and non-discriminative) — kept: decimals and 2-3 digit
    integers (magnitudes, tolls, counts)."""
    anchors: set[str] = set()
    for p in places or []:
        if p.get("kind") == "city":
            for w in re.findall(r"[a-z0-9]+", (p.get("name") or "").lower()):
                if len(w) > 2:
                    anchors.add(w)
    for m in _NUM_RE.findall(text or "")[:8]:
        norm = m.replace(",", ".")
        if "." in norm or 2 <= len(norm) <= 3:
            anchors.add(norm)
    return sorted(anchors)


def cluster_tokens(title: str, text: str | None = None,
                   places: list[dict] | None = None) -> str:
    """Normalized significant tokens for corroboration/event grouping: the
    headline's own words plus language-invariant anchors (so coverage in
    different languages of the same event can still cluster)."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    sig = {w for w in words if len(w) > 2 and w not in _STOPWORDS}
    sig |= set(invariant_anchors(text if text is not None else title, places))
    return " ".join(sorted(sig)[:15])


def sets_similarity(sa: set[str], sb: set[str]) -> float:
    """Headline-token similarity: the max of Jaccard and containment.

    Containment (overlap / size of the smaller set) matters for follow-up
    coverage: "Japan earthquake update: aftershocks continue" shares its
    anchor tokens with the original event but adds new vocabulary, which
    Jaccard alone punishes — and story updates would wrongly found new events.

    Takes the token sets rather than the strings: callers comparing one
    headline against thousands would otherwise re-split the same tokens
    thousands of times.
    """
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    return max(inter / len(sa | sb), inter / min(len(sa), len(sb)))


def tokens_similarity(a: str, b: str) -> float:
    """`sets_similarity` for two token strings."""
    return sets_similarity(set(a.split()), set(b.split()))


def score_importance(
    text: str,
    scope: str,
    tier: int,
    places: list[dict],
    corroborating_sources: int = 0,
) -> int:
    score = _SCOPE_BASE.get(scope, 30) + _TIER_ADJUST.get(tier, 0)
    score += breaking_signal(text)
    countries = {p.get("country") for p in places if p.get("country")}
    if len(countries) >= 2:
        score += 6
    score += min(corroborating_sources * 8, 24)
    return max(5, min(100, score))


# ---------------------------------------------------------------------------
# Decay: importance is a judgement about now, not a permanent property
# ---------------------------------------------------------------------------
#
# `score_importance` answers "how much does this matter?" at the moment an
# article arrives, and then the answer is frozen. That is wrong in a way every
# reader notices: a feed with a min-importance floor of 60 fills with what was
# urgent last Tuesday, and Home's "Top stories" keeps a week-old wire alert
# above this morning's local one, because the two were scored against different
# days and compared as though they were not.
#
# So the stored score falls with time. Three parameters shape the curve, and
# each exists to stop a specific failure:
#
#   grace     Nothing decays for the first few hours. Without it an article
#             ingested twenty minutes ago already ranks under one ingested now,
#             and Home's "Breaking" column — six hours, floor 40 — would churn
#             its own contents every tick for no reason a reader could see.
#
#   half-life How fast the rest of it goes. 48 hours is the honest middle: a
#             story is worth half as much the day after tomorrow as it is now.
#             Shorter turns the archive into a live wire; longer is barely
#             distinguishable from not decaying at all.
#
#   floor     A score never falls below this fraction of where it started. A
#             curve that runs to zero destroys the archive: `min_importance`
#             stops meaning "was this a big story" and starts meaning "is it a
#             big story this afternoon", so a search across last year returns
#             nothing above 60 however large the events in it were. Holding at
#             40% keeps a Critical story permanently above a routine one while
#             still dropping it below anything fresh of the same magnitude.
#
# Set NEWS_DECAY_HALF_LIFE_H=0 to switch the whole thing off; scores then behave
# exactly as they did before this existed.
DECAY_HALF_LIFE_HOURS = float(os.environ.get("NEWS_DECAY_HALF_LIFE_H", "48"))
DECAY_GRACE_HOURS = float(os.environ.get("NEWS_DECAY_GRACE_H", "6"))
DECAY_FLOOR_FRACTION = float(os.environ.get("NEWS_DECAY_FLOOR", "0.4"))

# How long a story's own updates can keep its earlier coverage fresh.
#
# The rule below ages an article from the *later* of its publication and its
# cluster's last update, so a piece from Monday stays near its opening score
# while the story it belongs to is still moving. That is the intent, and left
# uncapped it has a tail: a conflict or an election updates for months, and
# every article ever filed under it would sit at full importance for as long as
# the story ran, then freeze there the moment it stopped.
#
# So the lift expires. After this long, an article ages from its own
# publication whatever its cluster is doing — coverage from three weeks ago is
# old coverage even when the story continues. It is also what makes the sweep
# terminate: past twice this age nothing can still be moving, which is the
# predicate `ingest.decay_scores` uses to know what it may leave alone.
DECAY_LIFT_MAX_HOURS = float(os.environ.get("NEWS_DECAY_LIFT_MAX_H", "336"))


def decay_reference(
    published_at,
    event_first_seen=None,
    event_updated_at=None,
):
    """The moment a story last showed signs of life.

    The later of the article's own publication, the moment its cluster broke,
    and the moment that cluster was last added to — so a story still being
    covered does not age, and one nobody has touched since it broke ages from
    the break. An article in no cluster has only its own publication.

    The lift is capped at `DECAY_LIFT_MAX_HOURS` past publication: a live
    cluster can hold an article fresh, but not indefinitely.
    """
    if published_at is None:
        return event_updated_at or event_first_seen
    latest = published_at
    for stamp in (event_first_seen, event_updated_at):
        if stamp is not None and stamp > latest:
            latest = stamp
    ceiling = published_at + timedelta(hours=DECAY_LIFT_MAX_HOURS)
    return min(latest, ceiling)


def decay_floor(base: int) -> int:
    """The value a score falls to and stays at."""
    return max(5, min(base, round(base * DECAY_FLOOR_FRACTION)))


def decay_importance(base: int, age_hours: float) -> int:
    """`base`, aged by `age_hours`, on the curve documented above.

    Returns `base` unchanged for anything inside the grace window, for a
    negative age (a feed that dates its items in the future — several do), and
    when decay is switched off.
    """
    base = max(5, min(100, int(base)))
    if DECAY_HALF_LIFE_HOURS <= 0:
        return base
    floor = decay_floor(base)
    if base <= floor:
        return base
    moving = (age_hours or 0.0) - DECAY_GRACE_HOURS
    if moving <= 0:
        return base
    kept = 0.5 ** (moving / DECAY_HALF_LIFE_HOURS)
    return max(floor, min(base, round(floor + (base - floor) * kept)))


def aged_importance(
    base: int,
    published_at,
    event_first_seen=None,
    event_updated_at=None,
    now=None,
) -> int:
    """`decay_importance` with the age worked out from the timestamps."""
    now = now or datetime.utcnow()
    ref = decay_reference(published_at, event_first_seen, event_updated_at)
    if ref is None:
        return max(5, min(100, int(base)))
    return decay_importance(base, (now - ref).total_seconds() / 3600.0)
