"""Which mastheads are running the same newsroom's copy.

Corroboration — how many other outlets are carrying this story — is the signal
behind importance, and it counted feeds. Measured over a twelve-hour window of
105,118 articles, the sources that most often publish a headline someone else
published first turn out not to be random reposters. They come in families:

    Richmond News, St. Albert Gazette, The Albertan, Vancouver Is Awesome
    Diario Córdoba, La Opinión de Málaga, Hoy, La Rioja, Las Provincias
    Leidsch Dagblad, Noordhollands Dagblad, Haarlems Dagblad, BN DeStem

Glacier Media, Prensa Ibérica and Vocento, Mediahuis and DPG. One newsroom
writing under many local titles. Each has its own domain and its own article
URL, so the global URL dedup at ingest never sees them as duplicates — and each
one voted separately, which turned a single newsroom into an apparent consensus
and pushed the story's importance up with it.

This learns those families from what they publish and records them on the
source, so corroboration can count newsrooms instead of feeds.

Deliberately learned rather than configured. A list of media groups would be
wrong within a month, wrong in every country nobody thought to check, and
unowned. Identical copy under two mastheads, several times over, is the
observation itself.

What it does *not* do is disable anything. A local masthead running the group
wire most of the time can still be the only one covering its own town —
Haarlems Dagblad was 42% reposted and first on 58% — so this changes how a
source is counted, never whether it is read.
"""
from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Article, Source, utcnow

log = logging.getLogger("syndication")

# How much history to learn from, and the most rows it will read. Bounded for
# the reason the clustering window is: how many articles a day holds depends on
# how many sources are enabled.
WINDOW_HOURS = float(os.environ.get("NEWS_SYNDICATE_WINDOW_HOURS", "48"))
WINDOW_MAX = int(os.environ.get("NEWS_SYNDICATE_WINDOW_MAX", "120000"))

# Identical headlines two sources must share before they are called one
# newsroom. Two outlets can land on the same words by accident — "Weather
# warning issued for the weekend" — so once is not evidence. Several times is.
MIN_SHARED = int(os.environ.get("NEWS_SYNDICATE_MIN_SHARED", "4"))

# Short headlines collide by coincidence far more often than long ones, so they
# are not evidence of anything.
MIN_TITLE_CHARS = 25

_punct = re.compile(r"[^\w ]+", re.UNICODE)
_space = re.compile(r"\s+")


def normalize(title: str) -> str:
    """A headline reduced to what two mastheads would share if it were the same
    copy: case, punctuation and spacing carry no signal here."""
    return _space.sub(" ", _punct.sub(" ", (title or "").lower())).strip()[:160]


class _Groups:
    """Union-find over source ids.

    Transitive on purpose: if A shares copy with B and B with C, they are one
    newsroom. That is what a wire is. It can over-merge a chain, which is why
    the evidence threshold per pair is several headlines and not one.
    """

    def __init__(self) -> None:
        self._parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self._parent.setdefault(x, x)
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Lowest id wins, so the group key is stable across runs rather
            # than depending on the order rows came back in.
            lo, hi = sorted((ra, rb))
            self._parent[hi] = lo

    def groups(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = defaultdict(list)
        for member in self._parent:
            out[self.find(member)].append(member)
        return {root: sorted(m) for root, m in out.items() if len(m) > 1}


def _shared_headlines(db: Session) -> dict[frozenset[int], int]:
    """How many identical headlines each pair of sources has both run."""
    since = utcnow() - timedelta(hours=WINDOW_HOURS)
    rows = db.execute(
        select(Article.source_id, Article.title)
        .where(Article.published_at >= since, Article.title != "")
        .order_by(Article.published_at.desc())
        .limit(WINDOW_MAX)
    ).all()

    by_title: dict[str, set[int]] = defaultdict(set)
    for source_id, title in rows:
        key = normalize(title)
        if len(key) >= MIN_TITLE_CHARS:
            by_title[key].add(source_id)

    pairs: dict[frozenset[int], int] = defaultdict(int)
    for sources in by_title.values():
        if len(sources) < 2:
            continue
        # Headlines carried by very many sources are wire copy everybody runs,
        # which says nothing about who shares a newsroom with whom. Counting
        # every pair of them would also be quadratic on the widest stories.
        if len(sources) > 12:
            continue
        ordered = sorted(sources)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                pairs[frozenset((a, b))] += 1
    return pairs


def detect(db: Session) -> dict:
    """Learn the newsroom families and record them on their sources.

    Rewrites `Source.syndicate` for every source it has an opinion about, and
    clears it for sources that no longer share copy with anyone — so a group
    that stops syndicating stops being one.
    """
    pairs = _shared_headlines(db)
    groups = _Groups()
    evidence = 0
    for pair, count in pairs.items():
        if count >= MIN_SHARED:
            a, b = tuple(pair)
            groups.union(a, b)
            evidence += 1

    found = groups.groups()
    # The key is the lowest source id in the family, as a string: stable, and
    # it points at a real row somebody can go and look at.
    wanted = {member: str(root) for root, members in found.items()
              for member in members}

    changed = 0
    for source in db.scalars(select(Source)).all():
        want = wanted.get(source.id, "")
        if (source.syndicate or "") != want:
            source.syndicate = want
            changed += 1
    if changed:
        db.commit()

    result = {"groups": len(found), "sources": len(wanted),
              "pairs_with_evidence": evidence, "changed": changed,
              "at": utcnow().isoformat() + "Z"}
    log.info("syndication: %d newsroom group(s) across %d sources "
             "(%d changed)", len(found), len(wanted), changed)
    return result


def group_map(db: Session) -> dict[int, int]:
    """source id -> the id that counts for corroboration.

    Only grouped sources appear; everyone else is their own newsroom and the
    caller can default to the source's own id.
    """
    rows = db.execute(
        select(Source.id, Source.syndicate).where(Source.syndicate != "")).all()
    out: dict[int, int] = {}
    for source_id, key in rows:
        try:
            out[source_id] = int(key)
        except (TypeError, ValueError):
            continue
    return out


def families(db: Session) -> list[dict]:
    """The groups as something an operator can read and disagree with."""
    rows = db.execute(
        select(Source.id, Source.name, Source.syndicate)
        .where(Source.syndicate != "").order_by(Source.syndicate, Source.name)
    ).all()
    by_key: dict[str, list[str]] = defaultdict(list)
    for _, name, key in rows:
        by_key[key].append(name)
    return [{"key": key, "members": names} for key, names in
            sorted(by_key.items(), key=lambda kv: -len(kv[1]))]
