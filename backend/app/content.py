"""Fetch article pages and extract readable body text (stdlib HTML parsing).

Feed entries often carry only a headline and a stub summary; matching feed
criteria against them misses articles whose body is about the topic. This
module pulls the article page and reduces it to text so keywords, boolean
queries, geotagging, and categorization can see the whole story.

What comes out of here *is* what a filter searches, alongside the headline and
the feed summary — nothing else in the article row is ever matched. So a word
that reaches this text is a word that can put the article in a feed, and every
piece of the page that is not the story is a false match waiting to happen. The
extractor is written for that: it keeps prose and drops the page's furniture.
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
              "head", "footer", "aside", "figure", "button", "select",
              "template", "dialog"}
_BODY_TAGS = {"p", "h1", "h2", "h3", "li", "blockquote"}

# Elements that hold text without being prose themselves. They get a buffer of
# their own so the link rule below can judge them, but their text is never
# promoted to body text.
_OTHER_BLOCKS = {"div", "section", "article", "main", "header", "td", "th",
                 "dd", "dt", "h4", "h5", "h6", "figcaption", "details",
                 "summary", "label", "caption"}
_BLOCKS = _BODY_TAGS | _OTHER_BLOCKS

# Elements that never have content, so they must never be pushed as open.
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
              "link", "meta", "param", "source", "track", "wbr"}

# HTML's own rule: any of these opening implies the end of an open <p>. It is
# not decoration — see the note on _close_implied.
_CLOSES_P = {"address", "article", "aside", "blockquote", "details", "div",
             "dl", "fieldset", "figcaption", "figure", "footer", "form",
             "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main",
             "nav", "ol", "p", "pre", "section", "table", "ul"}

# A chunk of the fallback text has to look like a sentence to be kept. Ten
# words is above the length of menu items, buttons, bylines, kickers and promo
# one-liners, and below the length of almost any real sentence of reporting.
_MIN_PROSE_WORDS = 10

# The page talking to the reader about the page. These are second-person site
# copy, not reporting, and they are matched as phrases rather than words:
# "cookies" alone stays searchable, because an article about a cookie-consent
# fine is a real article about cookies and its words are its own.
_SITE_VOICE = (
    "we use cookies", "uses cookies", "accept all cookies", "cookie settings",
    "all rights reserved", "sign up for our", "subscribe to our",
    "enable javascript", "javascript is disabled", "javascript to continue",
    "your browser does not support", "please enable", "already a subscriber",
    "create a free account", "start your free trial",
)


class _Open:
    """An element that has been opened and not yet closed.

    `at` is where its text belongs in the document. A block's text is only
    judged once the block closes, which is after any nested block has already
    closed, so the pieces come out inside-first — and proximity searches
    (`a NEAR/5 b`) read the text as one string, so the order it is reassembled
    in has to be the order it was written in.
    """

    __slots__ = ("tag", "buf", "at")

    def __init__(self, tag: str, buf: list | None, at: int):
        self.tag = tag
        self.buf = buf
        self.at = at


class _Extractor(HTMLParser):
    """Readable text, minus the parts of the page that advertise other pages.

    Three things are dropped, and each one was a filter matching words no
    reader could see in the article.

    *Blocks whose whole text is a link.* `<li>` has to count as prose — articles
    have real bullet lists — but it is also what navigation menus and
    related-article rails are built from, and so are bare `<div>`s and
    "related" `<p>`s: `<p class="related"><a>Inside the AI data centre
    boom</a></p>` sitting under a WNBA report puts that report in a data-centre
    feed. The test is not which tag it is, it is whether the block says anything
    of its own: a block that is nothing but link text is a pointer to another
    page, while a link inside a sentence is an ordinary citation and the
    sentence keeps all of its words.

    *Furniture after an unclosed tag.* `</p>` and `</li>` are optional in HTML
    and plenty of newsrooms leave them out. Nothing here does the browser's
    implied-close, so one omitted `</p>` left the parser believing it was still
    inside the article for the rest of the document — and the newsletter promo,
    the "most read" box and the subscription pitch at the bottom of the page all
    became article text. Measured on a page of exactly that shape, three
    unrelated terms — data center, hyperscale, colocation — were stored as the
    body of a WNBA report.

    *The site's own voice.* A consent notice or a paywall pitch is prose by
    every structural measure, and it is on a large minority of pages.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._stack: list[_Open] = []
        self._skip = 0
        self._link = 0
        self._at = 0
        self._kept: list[tuple[int, str, bool]] = []   # (position, text, is body)

    def _next_at(self) -> int:
        self._at += 1
        return self._at

    @property
    def body_chunks(self) -> list[str]:
        return [t for _at, t, body in sorted(self._kept) if body]

    @property
    def all_chunks(self) -> list[str]:
        return [t for _at, t, _body in sorted(self._kept)]

    # ---------- the open-element stack ----------

    def _close_implied(self, tag: str) -> None:
        """Close what this start tag implies the end of.

        Without this the counters never come back down, and everything after the
        first newsroom that omits a `</p>` is treated as part of the article.
        """
        while self._stack:
            top = self._stack[-1].tag
            if top == "p" and tag in _CLOSES_P:
                self._pop()
            elif top == "li" and tag == "li":
                self._pop()
            elif top in ("dd", "dt") and tag in ("dd", "dt"):
                self._pop()
            else:
                return

    def _pop(self) -> None:
        el = self._stack.pop()
        if el.tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        if el.buf is not None:
            self._flush(el)

    def handle_starttag(self, tag, attrs):
        self._close_implied(tag)
        if tag in _VOID_TAGS:
            return
        if tag == "a":
            self._link += 1
            return
        if tag in _SKIP_TAGS:
            self._skip += 1
        buffered = tag in _BLOCKS and not self._skip
        self._stack.append(
            _Open(tag, [] if buffered else None,
                  self._next_at() if buffered else 0))

    def handle_endtag(self, tag):
        if tag == "a":
            if self._link:
                self._link -= 1
            return
        if tag in _VOID_TAGS:
            return
        # An end tag also closes anything still open inside it: a `</div>` is
        # how most unclosed paragraphs actually end. A stray end tag with no
        # opening — also common — closes nothing.
        if not any(el.tag == tag for el in self._stack):
            return
        while self._stack:
            closing = self._stack[-1].tag
            self._pop()
            if closing == tag:
                return

    # ---------- text ----------

    def _buffer(self) -> list | None:
        for el in reversed(self._stack):
            if el.buf is not None:
                return el.buf
        return None

    def _in_body(self) -> bool:
        return any(el.tag in _BODY_TAGS for el in self._stack)

    def handle_data(self, data):
        if self._skip:
            return
        text = data.strip()
        if not text:
            return
        buf = self._buffer()
        if buf is not None:
            buf.append((text, bool(self._link)))
        elif not self._link:
            # Loose text with no block around it. A bare link out here is a
            # menu item, so it is dropped the same way.
            self._kept.append((self._next_at(), text, False))

    def _flush(self, el: _Open) -> None:
        """A block has closed: keep its text, or recognise it as furniture."""
        if not el.buf:
            return
        if not any(not in_link for _text, in_link in el.buf):
            return                      # every word of it was a link label
        text = " ".join(t for t, _ in el.buf)
        low = text.lower()
        if any(phrase in low for phrase in _SITE_VOICE):
            return
        self._kept.append((el.at, text, el.tag in _BODY_TAGS or self._in_body()))

    def close(self):
        super().close()
        while self._stack:                # unclosed tags at end of document
            self._pop()


def extract_text(html: str) -> str:
    """Readable text of an HTML page: paragraph-level content, falling back
    to the page's visible sentences for pages that don't use <p> markup.

    The fallback is where a page with no paragraph markup — a JavaScript shell,
    a paywall stub, a wrapper of nested `<div>`s — used to hand over its entire
    chrome as the article body: the section menu, the "most read" list, the
    subscribe box, every word of it searchable and none of it the story. It is
    still worth having, because some sites really do write prose in `<div>`s, so
    what changes is what qualifies: sentence-length chunks only. A page whose
    visible text holds no sentences yields no body at all, and the article is
    matched on its headline and feed summary — which is the honest answer for a
    page whose text was never retrieved.
    """
    parser = _Extractor()
    try:
        parser.feed(html[:MAX_HTML_BYTES])
        parser.close()
    except Exception:  # malformed HTML: keep whatever was parsed
        pass
    text = " ".join(parser.body_chunks)
    if len(text) < 200:
        # Only if it actually finds more than the markup did: a genuinely short
        # story marked up properly keeps its own three sentences rather than
        # being replaced by whatever the rest of the page happened to say.
        loose = " ".join(c for c in parser.all_chunks
                         if len(c.split()) >= _MIN_PROSE_WORDS)
        if len(loose) > len(text):
            text = loose
    return re.sub(r"\s+", " ", text).strip()[:MAX_TEXT_CHARS]


# Trailers a feed generator appends to every item's summary. They are not the
# article, and one of them is worse than noise: WordPress's "The post … appeared
# first on <Site>" ends every summary with the publication's own name, so a feed
# watching "energy" collects everything Energy Voice publishes regardless of
# subject. Anchored at the end, where a generator puts them, so a story that
# happens to contain the words keeps them.
_SUMMARY_TRAILERS = (
    re.compile(r"\bthe post\b.{0,300}?\bappeared first on\b.*$", re.I | re.S),
    re.compile(r"\bshare this\s*:.*$", re.I | re.S),
    re.compile(r"\b(?:continue reading|read (?:more|the (?:full|rest)))\b[^.]{0,80}$", re.I),
    re.compile(r"\b(?:this (?:article|story)|it) (?:first |originally )?appeared "
               r"(?:first )?on\b.*$", re.I | re.S),
    re.compile(r"\boriginally published (?:on|at|by)\b.*$", re.I | re.S),
    re.compile(r"\bfiled under\s*:.*$", re.I | re.S),
    re.compile(r"\brelated\s*:\s*$", re.I),
)


def clean_summary(summary: str) -> str:
    """A feed summary with its generator's trailers removed.

    Whatever survives is searched exactly as the headline is, so the same rule
    applies: if it is not the story, it should not be able to put the story in
    somebody's feed.
    """
    text = summary or ""
    for pattern in _SUMMARY_TRAILERS:
        text = pattern.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" -–—|·•").strip()


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
