"""What kind of thing is this feed, actually?

Delphi adopts outlets automatically: an unknown domain turns up publishing in
someone else's coverage, `discovery` hunts for its feed, and if the feed parses
it becomes a source. The only question ever asked of the feed itself was "does
this parse and is it non-empty" — which a ticketing calendar answers just as
well as a newspaper does, because a ticketing calendar *is* a well-formed feed
of dated entries with headlines and bodies. That is how baodaorecords.kktix.cc,
a record label's ticket sales calendar, became a "national news" source and put
concert listings into an energy feed.

Two defences existed and both have the same shape. `_SKIP_NOT_NEWS` is a list
of domains somebody thought of, and it cannot name the ones nobody thought of —
the catalog had also picked up a funeral home. `MIN_SIGHTINGS` waits for a
domain to recur, which separates a real publisher from a one-off but says
nothing at all about a *recurring* ticket vendor. Neither one ever reads the
feed.

This does. The entries are already in hand at the moment of adoption — the
probe fetched and parsed them to decide the feed was real — and they are
thrown away. Everything below is computed from that same list, so it costs one
pass of arithmetic over at most a few dozen entries and not a single request.

## The signals, and why each one separates

**Dates in the future.** The sharpest of them, and the one that needs no
vocabulary in any language. A news article is published when it is written, so
a news feed's entries are always in the past. An events platform dates each
entry by *when the event happens*, so its feed is almost entirely in the
future. Nothing a newspaper does looks like this. The threshold is deliberately
far out — a day and a half — because a feed that mislabels local time as UTC
can be up to fourteen hours out, and that is a bug in a real news feed rather
than evidence of anything.

**What the links point at.** `/tickets/`, `/products/`, `/jobs/`, `/checkout/`.
This is the one that generalises past the domain list: it catches every Shopify
store rather than the ones already named, and it does it without knowing what
Shopify is. It has to be judged as a *proportion*, because a newspaper has an
events section and an obituaries section and links into both — what it does not
have is a feed where nine in ten links are tickets.

**What the text says.** Prices, "sold out", "apply now", "add to cart". Weaker
than the other two and much more English-shaped, which is why it is worth one
point rather than two in a product that reads 23 languages.

**Headlines built from a template.** "ARTIST at VENUE" three hundred times.
Real headlines share very few words with each other; generated ones share
almost all of them. Measured with the same set similarity that clusters
coverage, so there is one definition of "these two headlines are alike".

## What it does with them

It declines to adopt; it does not classify. Nothing here is confident enough to
be worth a wrong answer about a real newspaper, so the bar is two independent
signals — or the future-dating one alone, which is worth two by itself because
no legitimate news feed can trip it.

Everything above is a heuristic and will eventually be wrong about something.
So a rejection is always recorded with the reasons that produced it, in words
an operator can read and disagree with, and a source switched off this way is
switched off rather than deleted.
"""
from __future__ import annotations

import os
import re
from calendar import timegm
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlparse

from .scoring import sets_similarity

# How far ahead an entry has to be dated to count as "in the future".
#
# Not a small number, on purpose. A feed that writes local timestamps and
# labels them UTC is out by up to fourteen hours, and that is a common bug in
# feeds that are unambiguously news. Thirty-six hours is past anything a
# timezone can explain and far short of anything an events calendar does.
FUTURE_HOURS = float(os.environ.get("NEWS_KIND_FUTURE_H", "36"))

# Each signal needs a minimum sample before it may fire at all: a proportion
# over three entries is not a proportion, it is a coincidence.
MIN_DATED = 3
MIN_LINKED = 4
MIN_TEXTS = 4
MIN_TITLES = 6

FUTURE_SHARE = 0.5
PATH_SHARE = 0.6
WORD_SHARE = 0.5
TEMPLATE_SIMILARITY = 0.45

# Two signals, or the future-dating one by itself.
REJECT_AT = int(os.environ.get("NEWS_KIND_REJECT_AT", "2"))

# Path segments that say what a link is for. Matched as whole path segments
# rather than as substrings, so /product/ does not match /production-notes and
# /event/ does not match /eventual-collapse.
#
# Every one of these appears on real news sites too — a paper has an events
# listing and a jobs board and sells subscriptions — which is exactly why this
# is scored as a share of the feed and never as a single hit.
_EVENT_PATHS = {
    "event", "events", "ticket", "tickets", "ticketing", "veranstaltung",
    "veranstaltungen", "evento", "eventos", "evenement", "evenements",
    "agenda", "whats-on", "box-office", "boxoffice", "showtimes", "lineup",
    "sesion", "sesiones", "registration", "register-event", "rsvp",
    "activity", "activities", "workshop", "workshops", "webinar", "webinars",
    "booking", "bookings", "reservation", "reservations", "tour-dates",
}
_COMMERCE_PATHS = {
    "product", "products", "shop", "store", "collections", "collection",
    "cart", "checkout", "basket", "catalog", "catalogue", "produkt",
    "produkte", "producto", "productos", "boutique", "tienda", "loja",
    "pricing", "buy", "order", "orders", "deal", "deals", "coupon",
    "coupons", "listing", "listings", "classifieds", "for-sale",
}
_JOB_PATHS = {
    "job", "jobs", "career", "careers", "vacancy", "vacancies", "hiring",
    "recruitment", "stellenangebote", "empleo", "empleos", "vagas",
    "opportunity", "opportunities", "position", "positions",
}
_NOT_NEWS_PATHS = _EVENT_PATHS | _COMMERCE_PATHS | _JOB_PATHS

# A price, in the currencies a feed is likely to quote one in. Anchored on the
# symbol followed by digits, so "$1.2bn in aid" — a real headline — is caught
# too; that is why this signal is worth one point and never fires alone.
_PRICE_RE = re.compile(
    r"(?:[$£€¥₩₹₪₺]|NT\$|HK\$|US\$|A\$|C\$|R\$|RM|฿|₫|₱|Rp)\s?\d",
    re.IGNORECASE)

_EVENT_WORDS = (
    "buy tickets", "get tickets", "tickets on sale", "on sale now", "sold out",
    "doors open", "early bird", "rsvp", "box office", "general admission",
    "vip package", "book now", "reserve your", "limited seats", "line-up",
    "eintritt", "vorverkauf", "entradas", "boletos", "ingressos", "billets",
    "售票", "早鳥", "門票", "チケット", "예매",
)
_COMMERCE_WORDS = (
    "add to cart", "in stock", "out of stock", "free shipping", "shop now",
    "order now", "best seller", "discount code", "use code", "% off",
    "new arrival", "pre-order", "add to basket", "en venta", "comprar",
    "kaufen", "acheter",
)
_JOB_WORDS = (
    "apply now", "we are hiring", "we're hiring", "now hiring", "full-time",
    "part-time", "salary range", "job opening", "job description",
    "years of experience", "equal opportunity employer", "remote position",
    "bewerben", "vacante", "contratando",
)
_WORD_GROUPS = {
    "events": _EVENT_WORDS,
    "commerce": _COMMERCE_WORDS,
    "jobs": _JOB_WORDS,
}


@dataclass
class Verdict:
    """What the feed looks like, and what said so."""
    kind: str = "news"            # news | events | commerce | jobs
    score: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def is_news(self) -> bool:
        return self.score < REJECT_AT

    def summary(self) -> str:
        """One line for a status column, in words an operator can disagree with."""
        if self.is_news:
            return ""
        return f"{self.kind}: " + "; ".join(self.reasons)


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        stamp = entry.get(key) if hasattr(entry, "get") else getattr(entry, key, None)
        if stamp:
            try:
                return datetime.utcfromtimestamp(timegm(stamp))
            except (ValueError, OverflowError, TypeError):
                continue
    return None


def _entry_link(entry) -> str:
    link = (entry.get("link") or "") if hasattr(entry, "get") else ""
    return link if isinstance(link, str) else ""


def _entry_text(entry) -> str:
    get = entry.get if hasattr(entry, "get") else (lambda *_a: "")
    parts = [get("title") or "", get("summary") or ""]
    return " ".join(p for p in parts if isinstance(p, str))


def _path_segments(link: str) -> set[str]:
    try:
        path = urlparse(link).path.lower()
    except ValueError:
        return set()
    return {seg for seg in path.split("/") if seg}


def _future_dated(entries, now: datetime) -> float:
    """Share of dated entries published further ahead than a timezone can explain."""
    stamps = [t for t in (_entry_time(e) for e in entries) if t is not None]
    if len(stamps) < MIN_DATED:
        return 0.0
    ahead = now + timedelta(hours=FUTURE_HOURS)
    return sum(1 for t in stamps if t > ahead) / len(stamps)


def _path_share(entries) -> tuple[float, str]:
    """Share of links pointing at something that is not an article, and which."""
    links = [ln for ln in (_entry_link(e) for e in entries) if ln.startswith("http")]
    if len(links) < MIN_LINKED:
        return 0.0, ""
    hits = {"events": 0, "commerce": 0, "jobs": 0}
    matched = 0
    for link in links:
        segs = _path_segments(link)
        if segs & _JOB_PATHS:
            hits["jobs"] += 1
        elif segs & _EVENT_PATHS:
            hits["events"] += 1
        elif segs & _COMMERCE_PATHS:
            hits["commerce"] += 1
        else:
            continue
        matched += 1
    if not matched:
        return 0.0, ""
    return matched / len(links), max(hits, key=hits.get)


def _word_share(entries) -> tuple[float, str]:
    """Share of entries whose text is selling something, and what kind."""
    texts = [t.lower() for t in (_entry_text(e) for e in entries) if t.strip()]
    if len(texts) < MIN_TEXTS:
        return 0.0, ""
    hits = {"events": 0, "commerce": 0, "jobs": 0}
    matched = 0
    for text in texts:
        priced = bool(_PRICE_RE.search(text))
        found = ""
        for kind, words in _WORD_GROUPS.items():
            if any(w in text for w in words):
                found = kind
                break
        # A price on its own is not evidence of anything — "$1.2bn in aid" is a
        # headline. It counts only alongside a phrase that is selling.
        if found:
            hits[found] += 1
            matched += 1
        elif priced:
            continue
    if not matched:
        return 0.0, ""
    return matched / len(texts), max(hits, key=hits.get)


def _template_similarity(entries) -> float:
    """How alike the headlines are to one another.

    Generated titles — "ARTIST at VENUE", "PRODUCT — 40% off" — share nearly
    every word with their neighbours. Written headlines share almost none. The
    median of consecutive pairs rather than the mean, so one repeated
    housekeeping entry in an otherwise varied feed does not carry it.
    """
    titles = []
    for entry in entries:
        title = (entry.get("title") or "") if hasattr(entry, "get") else ""
        words = {w for w in re.split(r"\W+", title.lower()) if len(w) > 2}
        if words:
            titles.append(words)
    if len(titles) < MIN_TITLES:
        return 0.0
    pairs = [sets_similarity(titles[i], titles[i + 1]) for i in range(len(titles) - 1)]
    pairs.sort()
    mid = len(pairs) // 2
    return pairs[mid] if len(pairs) % 2 else (pairs[mid - 1] + pairs[mid]) / 2


def classify(entries, now: datetime | None = None) -> Verdict:
    """Read a feed's own entries and say whether it is news.

    Never raises: this runs on third-party content in the middle of both the
    adoption path and the poll, and a feed shaped in a way nobody anticipated
    must leave the catalog exactly as it found it rather than take a cycle
    down. An unreadable feed is reported as news, because "I could not tell"
    and "this is not news" are different answers and only one of them is
    allowed to switch a source off.
    """
    verdict = Verdict()
    try:
        entries = list(entries or [])
        if not entries:
            return verdict
        now = now or datetime.utcnow()

        future = _future_dated(entries, now)
        if future >= FUTURE_SHARE:
            # Worth two by itself: a news feed cannot be dated in the future,
            # so there is no second signal that would make this more certain.
            verdict.score += 2
            verdict.kind = "events"
            verdict.reasons.append(
                f"{future:.0%} of entries are dated more than "
                f"{FUTURE_HOURS:.0f} hours in the future, which is an events "
                f"calendar rather than a publication date")

        path, path_kind = _path_share(entries)
        if path >= PATH_SHARE and path_kind:
            verdict.score += 1
            verdict.kind = path_kind
            verdict.reasons.append(
                f"{path:.0%} of links point at {path_kind} pages rather than articles")

        word, word_kind = _word_share(entries)
        if word >= WORD_SHARE and word_kind:
            verdict.score += 1
            if verdict.kind == "news":
                verdict.kind = word_kind
            verdict.reasons.append(
                f"{word:.0%} of entries read as {word_kind} listings")

        template = _template_similarity(entries)
        if template >= TEMPLATE_SIMILARITY:
            verdict.score += 1
            verdict.reasons.append(
                f"headlines are {template:.0%} alike to one another, so they "
                f"are generated from a template rather than written")

        if verdict.is_news:
            verdict.kind = "news"
        return verdict
    except Exception:
        return Verdict()
