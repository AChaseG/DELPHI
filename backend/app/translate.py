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

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Article, Translation

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
    out = {t.article_id: {"title": t.title, "summary": t.summary} for t in cached}

    missing = [a for a in todo if a.id not in out]
    if missing:
        sem = asyncio.Semaphore(CONCURRENCY)
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            results = await asyncio.gather(
                *(_translate_one(client, sem, a, target) for a in missing)
            )
        dirty = False
        for article_id, title, summary in results:
            if title:
                db.add(Translation(article_id=article_id, lang=target,
                                   title=title, summary=summary or ""))
                out[article_id] = {"title": title, "summary": summary or ""}
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
                    out[t.article_id] = {"title": t.title, "summary": t.summary}
    return out
