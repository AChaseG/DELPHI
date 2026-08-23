"""Where a publisher's reliability rating comes from, and what it is worth.

An article carries an outlet's name and nothing about whether that outlet is
any good. This attaches a published judgement to it.

## Why this list and not the obvious one

The obvious one is Media Bias/Fact Check, which has ratings for 11,000-odd
outlets including political lean, and it was considered first. Three things
ruled it out as the thing to build *first*, and only one of them is about
money:

  - It is a **commercial licence**. The data is paid for, per use case and
    volume, and Delphi is self-hostable — so every copy anybody stands up
    would inherit an obligation its operator never agreed to. Any MBFC support
    has to be off by default and keyed to the operator's own agreement.

  - Its ratings are **one person's opinions**, which MBFC says itself: its own
    disclaimer calls the method "not a tested scientific method... a simple
    guide to the idea of a source's bias." A 2026 survey of the research
    literature (arXiv:2607.12108) found papers treating it as authoritative
    while describing it in ways its own About page contradicts, and told
    researchers to stop.

  - It is **not auditable**. You cannot read why an outlet was rated as it was,
    or argue with it.

Wikipedia's perennial sources list is smaller and covers less — roughly 620
outlets with a usable judgement, against MBFC's 11,000 — and it is better on
every one of those three counts. It is free, it is CC BY-SA so redistribution
is explicitly permitted with attribution, and every entry links to the
discussion that produced it, so a reader who disagrees can go and read the
argument. That last property is the one that matters for a news product: this
is not a score Delphi is asserting, it is somebody else's published finding
with a link to the reasoning.

**What it does not give us is political lean**, and there is no free source
that does. That half of the question is unanswered here rather than answered
badly.

## Why it is fetched weekly and not polled

The list changes by a handful of entries a month. Everything else in Delphi
that fetches is fetching news; this is a reference table, and treating it like
a feed would be a request every fifteen minutes for an answer that changes six
times a year.

## Why the parse reads text rather than markup

The obvious parse walks the wiki template that builds each row. That template
is an implementation detail of a page thousands of people edit, and when it
changes the parse does not fail — it quietly returns fewer rows, which is the
failure mode that matters, because a rating that silently disappears looks
exactly like an outlet nobody has assessed.

So the parse keys on the five status phrases themselves — the words a reader
sees in the table — and on the external links in the same row. Both are what
the page is *for*, so neither can change without the page visibly changing.
And a fetch that comes back with implausibly few rows is treated as a failed
fetch: the previous ratings stand, and nothing is deleted. That is the same
rule Typhon's providers follow, and for the same reason.
"""
from __future__ import annotations

import logging
import os
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

from sqlalchemy import select

from . import safefetch
from .models import SourceRating, utcnow

log = logging.getLogger("reputation")

PROVIDER = "wikipedia"
API_URL = "https://en.wikipedia.org/w/api.php"
PAGE = "Wikipedia:Reliable sources/Perennial sources"
PAGE_URL = "https://en.wikipedia.org/wiki/Wikipedia:Reliable_sources/Perennial_sources"
LICENCE = "CC BY-SA 4.0"
ATTRIBUTION = "Wikipedia community · Reliable sources/Perennial sources"

ENABLED = os.environ.get("NEWS_SOURCE_RATINGS", "1") == "1"
REFRESH_EVERY_SECONDS = float(os.environ.get("NEWS_RATINGS_EVERY_S", "604800"))
FETCH_TIMEOUT = float(os.environ.get("NEWS_RATINGS_TIMEOUT", "30"))

# Below this many rows the page did not parse as expected, whatever it
# returned. The list has carried over a thousand entries for years, so a
# hundred is far under any real edit and far over any accident.
MIN_PLAUSIBLE_ROWS = int(os.environ.get("NEWS_RATINGS_MIN_ROWS", "100"))

# 1 best .. 5 worst. Ordered so the worse rating always wins a tie, which is
# the safe direction: an outlet listed twice should show the warning, not hide
# behind the better of its two entries.
STATUSES = {
    "generally reliable": (1, "Generally reliable"),
    "no consensus": (2, "No consensus"),
    "generally unreliable": (3, "Generally unreliable"),
    "deprecated": (4, "Deprecated"),
    "blacklisted": (5, "Blacklisted"),
}

# Wikipedia's own infrastructure, cited in every row as the discussion link.
# Matching them as publishers would rate the encyclopedia rather than the
# outlet the row is about.
_IGNORED_HOSTS = {
    "wikipedia.org", "wikimedia.org", "wikidata.org", "wiktionary.org",
    "wikinews.org", "wikiquote.org", "wikisource.org", "archive.org",
    "web.archive.org", "doi.org", "worldcat.org", "google.com",
}


def enabled() -> bool:
    return ENABLED


def _host(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    host = host.split(":")[0].removeprefix("www.")
    return host


def _ignored(domain: str) -> bool:
    return any(domain == bad or domain.endswith("." + bad) for bad in _IGNORED_HOSTS)


class _RowParser(HTMLParser):
    """Collect each table row's visible text and its external links.

    Nothing here knows what template built the page. A row is a `<tr>`, a link
    is an `<a href>`, and the status is one of five phrases a reader can see —
    all three are what the page is for, so none can change silently.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[str, list[str]]] = []
        self._depth = 0
        self._text: list[str] = []
        self._links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._depth += 1
            self._text, self._links = [], []
        elif tag == "a" and self._depth:
            href = dict(attrs).get("href") or ""
            if href.startswith("http"):
                self._links.append(href)

    def handle_endtag(self, tag):
        if tag == "tr" and self._depth:
            self._depth -= 1
            self.rows.append((" ".join(self._text), list(self._links)))
            self._text, self._links = [], []

    def handle_data(self, data):
        if self._depth:
            self._text.append(data)


def _status_of(text: str) -> tuple[int, str] | None:
    """The first status phrase the row states, worst-first on a tie."""
    low = re.sub(r"\s+", " ", text).lower()
    found = [STATUSES[phrase] for phrase in STATUSES if phrase in low]
    return max(found, key=lambda pair: pair[0]) if found else None


def parse_page(html: str) -> dict[str, dict] | None:
    """domain -> {status, rank, label, url}, or None when the page did not parse.

    None and an empty dict are different answers and the caller treats them
    differently: None keeps whatever ratings are already stored, because a page
    that stopped parsing is not a world in which nobody has been assessed.
    """
    if not html:
        return None
    parser = _RowParser()
    try:
        parser.feed(html)
    except Exception:
        return None

    out: dict[str, dict] = {}
    for text, links in parser.rows:
        status = _status_of(text)
        if not status:
            continue
        rank, label = status
        # The outlet's name is the row's first few words; the discussion links
        # that follow are Wikipedia's own and are filtered out below.
        name = re.sub(r"\s+", " ", text).strip()[:200]
        for link in links:
            domain = _host(link)
            if not domain or _ignored(domain):
                continue
            prior = out.get(domain)
            if prior is None or rank > prior["rank"]:
                out[domain] = {"status": label, "rank": rank,
                               "label": name, "url": PAGE_URL}
    if len(out) < MIN_PLAUSIBLE_ROWS:
        log.warning("perennial sources parsed to only %d domains — treating the "
                    "fetch as failed and keeping the ratings already stored",
                    len(out))
        return None
    return out


async def fetch(client=None) -> dict[str, dict] | None:
    """Fetch and parse the list. None on any failure — never an empty world."""
    params = {"action": "parse", "page": PAGE, "prop": "text",
              "format": "json", "formatversion": "2", "redirects": "1"}
    try:
        if client is not None:
            resp = await client.get(API_URL, params=params)
        else:
            async with safefetch.client(timeout=FETCH_TIMEOUT) as own:
                resp = await own.get(API_URL, params=params)
        if resp.status_code != 200:
            log.warning("perennial sources: HTTP %s", resp.status_code)
            return None
        html = (resp.json().get("parse") or {}).get("text") or ""
    except Exception:
        log.exception("perennial sources: could not fetch the list")
        return None
    return parse_page(html if isinstance(html, str) else (html or {}).get("*", ""))


def store(db, ratings: dict[str, dict]) -> dict:
    """Replace this provider's ratings with a freshly fetched set.

    Rows that vanished from the list are deleted, which is right: an outlet
    removed from the perennial list is one Wikipedia no longer has a standing
    view about, and keeping a judgement nobody holds any more would be worse
    than showing none. Safe to do wholesale only because `fetch` refuses to
    report an empty or implausible list as success.
    """
    existing = {r.domain: r for r in db.scalars(
        select(SourceRating).where(SourceRating.provider == PROVIDER))}
    added = changed = 0
    for domain, rating in ratings.items():
        row = existing.pop(domain, None)
        if row is None:
            db.add(SourceRating(provider=PROVIDER, domain=domain,
                                status=rating["status"], rank=rating["rank"],
                                label=rating["label"][:200], url=rating["url"]))
            added += 1
        elif row.rank != rating["rank"] or row.status != rating["status"]:
            row.status, row.rank = rating["status"], rating["rank"]
            row.label, row.url = rating["label"][:200], rating["url"]
            row.updated_at = utcnow()
            changed += 1
    dropped = len(existing)
    for row in existing.values():
        db.delete(row)
    db.commit()
    return {"total": len(ratings), "added": added,
            "changed": changed, "dropped": dropped}


async def refresh(db) -> dict:
    """One weekly pass: fetch, parse, store. Never raises."""
    if not enabled():
        return {"ok": False, "enabled": False, "reason": "not enabled"}
    ratings = await fetch()
    if ratings is None:
        return {"ok": False, "enabled": True,
                "reason": "the list could not be fetched or did not parse"}
    import asyncio
    result = await asyncio.to_thread(store, db, ratings)
    return {"ok": True, "enabled": True, "at": utcnow().isoformat() + "Z", **result}


def lookup(db, domains: set[str]) -> dict[str, dict]:
    """Ratings for a set of publisher domains, matching parent domains too.

    news.bbc.co.uk is rated by the entry for bbc.co.uk: the judgement is about
    the publisher, and a section of a site is the same publisher. Walked from
    the most specific label inward, so a subdomain with its own entry — which
    the list does carry, for outlets whose blogs are rated separately — wins
    over its parent.
    """
    if not domains:
        return {}
    wanted: set[str] = set()
    for domain in domains:
        parts = (domain or "").split(".")
        for i in range(len(parts) - 1):
            wanted.add(".".join(parts[i:]))
    if not wanted:
        return {}
    rows = db.scalars(select(SourceRating).where(
        SourceRating.provider == PROVIDER,
        SourceRating.domain.in_(sorted(wanted)))).all()
    by_domain = {r.domain: r for r in rows}
    out: dict[str, dict] = {}
    for domain in domains:
        parts = (domain or "").split(".")
        for i in range(len(parts) - 1):
            row = by_domain.get(".".join(parts[i:]))
            if row is not None:
                out[domain] = {
                    "status": row.status, "rank": row.rank, "url": row.url,
                    "provider": ATTRIBUTION, "licence": LICENCE,
                }
                break
    return out
