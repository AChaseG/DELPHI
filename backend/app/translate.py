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
from sqlalchemy.orm import Session

from .models import Article, Translation

log = logging.getLogger("translate")

PROVIDER = os.environ.get("NEWS_TRANSLATE_PROVIDER", "google")
LIBRETRANSLATE_URL = os.environ.get("NEWS_LIBRETRANSLATE_URL", "").rstrip("/")
LIBRETRANSLATE_KEY = os.environ.get("NEWS_LIBRETRANSLATE_KEY", "")
TIMEOUT = float(os.environ.get("NEWS_TRANSLATE_TIMEOUT", "10"))
CONCURRENCY = int(os.environ.get("NEWS_TRANSLATE_CONCURRENCY", "6"))
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


async def _translate_one(client, sem, article: Article, target: str):
    fn = _libretranslate if PROVIDER == "libretranslate" else _google
    async with sem:
        try:
            title = await fn(client, article.title, target)
            summary = ""
            if article.summary:
                summary = await fn(client, article.summary[:MAX_SUMMARY_CHARS], target)
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
            db.commit()
    return out
