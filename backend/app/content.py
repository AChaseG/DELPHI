"""Fetch article pages and extract readable body text (stdlib HTML parsing).

Feed entries often carry only a headline and a stub summary; matching feed
criteria against them misses articles whose body is about the topic. This
module pulls the article page and reduces it to text so keywords, boolean
queries, geotagging, and categorization can see the whole story.
"""
from __future__ import annotations

import logging
import re
from html.parser import HTMLParser

import httpx

log = logging.getLogger("content")

MAX_HTML_BYTES = 1_500_000
MAX_TEXT_CHARS = 20_000
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "form", "iframe",
              "head", "footer", "aside", "figure", "button"}
_BODY_TAGS = {"p", "h1", "h2", "h3", "li", "blockquote"}
# Where a bare link is another page rather than this one: menus, "More from"
# rails, "Most read" lists, section indexes, newsletter promos. All of them are
# list items and headings whose whole text is the link.
_LINKISH_TAGS = {"li", "h1", "h2", "h3"}


class _Extractor(HTMLParser):
    """Readable text, minus the parts of the page that advertise other pages.

    `<li>` has to be a body tag — articles have real bullet lists — but it is
    also what every navigation menu and related-articles rail is built from,
    and those are only skipped by the tag list above when a site wraps them in
    <nav>/<aside>. Most do not: `<div class="more-from"><ul><li><a>` is the
    common shape, and it lands in the middle of the stored body.

    That is not cosmetic. Criteria are matched against title + summary + body,
    so a story about the WNBA carried on a site with a "Data Center" section
    link and an "Inside the new AI data center boom" promo *contains* the words
    "data center" as far as matching is concerned — and turns up in a data
    centre feed whose terms appear nowhere a reader can see them. Measured on a
    page of exactly that shape, three unrelated terms — data center,
    hyperscale, colocation — were stored as the body of a WNBA report.

    So link text is dropped inside list items and headings, and kept inside
    paragraphs and quotes, where a link is an ordinary citation in prose.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._body = 0
        self._linkish = 0
        self._link = 0
        self.body_chunks: list[str] = []
        self.all_chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BODY_TAGS:
            self._body += 1
        if tag in _LINKISH_TAGS:
            self._linkish += 1
        elif tag == "a":
            self._link += 1

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BODY_TAGS and self._body:
            self._body -= 1
        if tag in _LINKISH_TAGS and self._linkish:
            self._linkish -= 1
        elif tag == "a" and self._link:
            self._link -= 1

    def _navigation(self) -> bool:
        return bool(self._link and self._linkish)

    def handle_data(self, data):
        if self._skip or self._navigation():
            return
        text = data.strip()
        if not text:
            return
        self.all_chunks.append(text)
        if self._body:
            self.body_chunks.append(text)


def extract_text(html: str) -> str:
    """Readable text of an HTML page: paragraph-level content, falling back
    to all visible text for pages that don't use <p> markup."""
    parser = _Extractor()
    try:
        parser.feed(html[:MAX_HTML_BYTES])
    except Exception:  # malformed HTML: keep whatever was parsed
        pass
    text = " ".join(parser.body_chunks)
    if len(text) < 200:
        text = " ".join(parser.all_chunks)
    return re.sub(r"\s+", " ", text).strip()[:MAX_TEXT_CHARS]


async def fetch_article_text(client: httpx.AsyncClient, url: str) -> str:
    """Fetch one article page and return its body text ('' on any failure)."""
    try:
        resp = await client.get(url, headers={"User-Agent": BROWSER_UA})
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype and "xml" not in ctype:
            return ""
        return extract_text(resp.text)
    except Exception as exc:
        log.debug("content fetch failed for %s: %s", url, exc)
        return ""
