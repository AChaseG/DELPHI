"""Automatic article translation into each user's preferred language.

Translations are lazy (requested the first time a user whose language differs
from the article's loads it) and cached permanently in the `translations`
table, so each article+language pair costs at most one provider call.

Providers (NEWS_TRANSLATE_PROVIDER):
  - "google" (default): the free public translate.googleapis.com gtx endpoint.
    Convenient for personal/self-hosted use; not an official SLA-backed API —
    switch to LibreTranslate for anything serious.
  - "libretranslate": a LibreTranslate server (self-hostable, open source).
    Set NEWS_LIBRETRANSLATE_URL (and NEWS_LIBRETRANSLATE_KEY if required).
  - "off": disable translation; original text is always returned.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta

import httpx
from sqlalchemy import and_, case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Article, Translation, utcnow

log = logging.getLogger("translate")

PROVIDER = os.environ.get("NEWS_TRANSLATE_PROVIDER", "google")
LIBRETRANSLATE_URL = os.environ.get("NEWS_LIBRETRANSLATE_URL", "").rstrip("/")
LIBRETRANSLATE_KEY = os.environ.get("NEWS_LIBRETRANSLATE_KEY", "")
TIMEOUT = float(os.environ.get("NEWS_TRANSLATE_TIMEOUT", "10"))
# Articles in flight at once. Each now issues its title and summary together,
# so the requests actually in the air are about twice this. Raised from 6 with
# the free gtx endpoint in mind rather than the network: it is not an
# SLA-backed API and leaning on it harder is how you find its rate limit.
CONCURRENCY = int(os.environ.get("NEWS_TRANSLATE_CONCURRENCY", "8"))
MAX_SUMMARY_CHARS = 700

# How many times one article is asked for in one language before Delphi stops
# asking.
#
# It used to be "for ever, silently". A failure stored nothing, so the next
# reader to load the board tried again, and an article the provider will never
# translate — an empty title, a script it does not carry, a permanently
# rejected string — was retried on every single load by every single reader.
# Four attempts is enough to ride out a rate limit and few enough to give up on
# something that is not going to work.
MAX_ATTEMPTS = int(os.environ.get("NEWS_TRANSLATE_MAX_ATTEMPTS", "4"))

# Wait after each failure, doubling. A rate limit is the common failure and the
# worst thing to answer with an immediate retry, which is how a burst of 429s
# becomes a longer burst of 429s.
RETRY_BACKOFF_S = float(os.environ.get("NEWS_TRANSLATE_BACKOFF_S", "300"))

# Counted for the operator console. A translation layer that is quietly failing
# looks exactly like a catalog that happens to be in your language, and the
# only difference visible from outside is this number.
stats = {"translated": 0, "failed": 0, "gave_up": 0}

# Offered in the dashboard language picker (code -> native name).
UI_LANGUAGES = {
    "en": "English", "es": "Español", "fr": "Français", "de": "Deutsch",
    "it": "Italiano", "pt": "Português", "nl": "Nederlands", "pl": "Polski",
    "sv": "Svenska", "tr": "Türkçe", "uk": "Українська", "ru": "Русский",
    "ar": "العربية", "hi": "हिन्दी", "zh": "中文", "ja": "日本語", "ko": "한국어",
}


def enabled() -> bool:
    if PROVIDER == "off":
        return False
    if PROVIDER == "libretranslate":
        return bool(LIBRETRANSLATE_URL)
    return True


async def _google(client: httpx.AsyncClient, text: str, target: str) -> str:
    resp = await client.get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text},
    )
    resp.raise_for_status()
    data = resp.json()
    return "".join(seg[0] for seg in (data[0] or []) if seg and seg[0])


async def _libretranslate(client: httpx.AsyncClient, text: str, target: str) -> str:
    body = {"q": text, "source": "auto", "target": target, "format": "text"}
    if LIBRETRANSLATE_KEY:
        body["api_key"] = LIBRETRANSLATE_KEY
    resp = await client.post(f"{LIBRETRANSLATE_URL}/translate", json=body)
    resp.raise_for_status()
    return resp.json()["translatedText"]


async def _nothing() -> str:
    """An article with no summary still has to return one, and returning it
    from a coroutine keeps the gather below symmetrical."""
    return ""


def _looks_translated(original: str, translated: str, target: str) -> bool:
    """Did the provider actually return a translation?

    Borrowed from what production translation pipelines call quality
    estimation, in the cheapest form that catches the failures seen here: an
    empty answer, and an answer identical to what was sent. The free endpoint
    returns the input unchanged when it cannot do anything with it, and storing
    that as a translation caches a non-answer for ever.

    Deliberately not a language check on the output. A headline that is a
    proper noun — "Samsung" — legitimately survives translation unchanged, and
    rejecting those would cost more than it saves.
    """
    if not translated.strip():
        return False
    if translated.strip() == (original or "").strip():
        # Same string back. Fine for a one-word proper noun, suspicious for a
        # sentence, and a sentence is what a headline is.
        return len((original or "").split()) <= 2
    return True


async def _translate_one(client, sem, article: Article, target: str):
    fn = _libretranslate if PROVIDER == "libretranslate" else _google
    async with sem:
        try:
            # Together, not one after the other. A title and a summary have
            # nothing to do with each other, but asking for the second only
            # once the first came back doubled the depth of every article's
            # translation — on a board of forty stories that is forty waits
            # arranged in series for no reason, and it was most of the delay a
            # reader sat through before the page appeared.
            title, summary = await asyncio.gather(
                fn(client, article.title, target),
                fn(client, article.summary[:MAX_SUMMARY_CHARS], target)
                if article.summary else _nothing(),
            )
            if not _looks_translated(article.title, title, target):
                log.info("translate returned nothing usable for article %s -> %s",
                         article.id, target)
                return article.id, None, None
            return article.id, title.strip(), summary.strip()
        except Exception as exc:
            log.warning("translate failed for article %s -> %s: %s", article.id, target, exc)
            return article.id, None, None


async def translate_articles(db: Session, articles: list[Article], target: str) -> dict[int, dict]:
    """Return {article_id: {"title", "summary"}} for articles not already in
    `target`. Cached translations are reused; misses hit the provider and are
    stored. Failures simply leave the article untranslated."""
    target = (target or "").strip().lower()[:2]
    if not target or not enabled():
        return {}
    todo = [a for a in articles if a.language and a.language[:2] != target]
    if not todo:
        return {}

    cached = db.scalars(
        select(Translation).where(
            Translation.lang == target,
            Translation.article_id.in_([a.id for a in todo]),
        )
    ).all()
    # A row with a title is a translation. A row without one is a record that
    # the provider was asked and did not answer — which is a different thing,
    # and the distinction is the whole point of storing it.
    out = {t.article_id: {"title": t.title, "summary": t.summary}
           for t in cached if t.title}
    failed = {t.article_id: t for t in cached if not t.title}

    now = utcnow()
    missing = []
    for a in todo:
        if a.id in out:
            continue
        record = failed.get(a.id)
        if record is None:
            missing.append(a)
            continue
        if (record.attempts or 0) >= MAX_ATTEMPTS:
            continue                      # asked enough times; it is not coming
        if record.next_try_at and record.next_try_at > now:
            continue                      # still inside the backoff
        missing.append(a)
    if missing:
        sem = asyncio.Semaphore(CONCURRENCY)
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            results = await asyncio.gather(
                *(_translate_one(client, sem, a, target) for a in missing)
            )
        dirty = False
        for article_id, title, summary in results:
            if title:
                record = failed.get(article_id)
                if record is not None:
                    # It failed before and has now succeeded. Fill the record
                    # in rather than inserting beside it — the unique
                    # constraint would reject a second row anyway.
                    record.title, record.summary = title, summary or ""
                    record.next_try_at = None
                else:
                    db.add(Translation(article_id=article_id, lang=target,
                                       title=title, summary=summary or ""))
                out[article_id] = {"title": title, "summary": summary or ""}
                stats["translated"] += 1
                dirty = True
            else:
                record = failed.get(article_id)
                attempts = (record.attempts if record else 0) + 1
                # Doubling, so a provider having a bad hour is asked four times
                # over several hours rather than four times in four minutes.
                wait = timedelta(seconds=RETRY_BACKOFF_S * (2 ** (attempts - 1)))
                if record is None:
                    db.add(Translation(article_id=article_id, lang=target,
                                       title="", summary="", attempts=attempts,
                                       next_try_at=utcnow() + wait))
                else:
                    record.attempts = attempts
                    record.next_try_at = utcnow() + wait
                stats["failed"] += 1
                if attempts >= MAX_ATTEMPTS:
                    stats["gave_up"] += 1
                dirty = True
        if dirty:
            try:
                db.commit()
            except IntegrityError:
                # Concurrent request cached the same article+lang first (e.g.
                # several dashboard columns containing the same article load
                # in parallel). Their rows win; reuse them.
                db.rollback()
                rows = db.scalars(select(Translation).where(
                    Translation.lang == target,
                    Translation.article_id.in_([a.id for a in missing]),
                )).all()
                for t in rows:
                    if t.title:
                        out[t.article_id] = {"title": t.title, "summary": t.summary}
    return out


def pending(db, target: str) -> dict:
    """How the translation layer is doing, for the operator console."""
    target = (target or "").strip().lower()[:2]
    rows = db.execute(select(
        func.count(Translation.id),
        func.sum(case((Translation.title == "", 1), else_=0)),
        func.sum(case((and_(Translation.title == "",
                            Translation.attempts >= MAX_ATTEMPTS), 1), else_=0)),
    ).where(Translation.lang == target)).one() if target else (0, 0, 0)
    total, failing, abandoned = (rows[0] or 0), (rows[1] or 0), (rows[2] or 0)
    return {"cached": total - failing, "failing": failing,
            "given_up": abandoned, "lang": target}
