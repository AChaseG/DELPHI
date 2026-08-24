"""User-defined boolean query strings over article text.

Grammar (case-insensitive keywords):
    expr     := or_expr
    or_expr  := and_expr ( OR and_expr )*
    and_expr := not_expr ( [AND] not_expr )*     # adjacency = implicit AND
    not_expr := NOT not_expr | near_expr
    near_expr:= atom ( NEAR[/n] atom )*          # operands: words/phrases only
    atom     := '(' expr ')' | [field:] ( "quoted phrase" | word )

Words and phrases match on word boundaries, case-insensitively.
Example:  ("supply chain" OR semiconductor) AND (china OR taiwan) NOT rumor

Additional operator types:
  - Wildcards inside words/phrases: `*` = any run of letters/digits
    (strik* matches strike/strikes/striking), `?` = exactly one character
    (organi?ation). A wildcarded word needs at least two literal characters.
  - Proximity: `a NEAR/5 b` matches when the two terms occur with at most
    5 words between them, in either order; bare NEAR defaults to NEAR/10.
    Chains (`a NEAR/3 b NEAR/3 c`) require each adjacent pair to be close.
    `a AROUND(5) b` is Google's spelling of the same thing.
  - Field scoping: `intitle:`, `intext:`, `source:` and `site:` restrict a word
    or phrase to one part of the article instead of all of it.

Google-style input is accepted too, since users paste queries they first
tried in a search engine: curly “smart quotes” are normalized, a leading
minus negates a term (-sports == NOT sports), and | / || mean OR (&& = AND).

**Everything in this grammar either works or says it doesn't.** That is a rule
rather than an aspiration, and it is here because the alternative shipped: every
operator on Google's own cheat-sheet used to parse and then quietly do nothing.
`intitle:sleep` became a literal word "intitle:sleep", which no article contains,
so the feed was empty and nothing said why. `sleep AROUND (5) anxiety` parsed as
`sleep AND 5 AND anxiety` and demanded the digit 5 in the text. `~academic`
matched nothing at all. A query language that accepts a line and silently means
something else is worse than one that refuses it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


class QueryError(ValueError):
    pass


@dataclass
class Token:
    kind: str  # AND OR NOT NEAR LPAREN RPAREN TERM FIELD
    value: str
    pos: int


# What a scoped term is matched against. Written out as its own type because
# "the text" stopped being one string the moment `intitle:` existed, and passing
# a bare string around would have made every field operator silently match the
# whole article — the exact failure this grammar is meant to stop making.
@dataclass(frozen=True)
class Text:
    all: str = ""          # headline + summary + body, what an unscoped term reads
    headline: str = ""     # intitle:
    text: str = ""         # intext: — summary and body, the article's own words
    source: str = ""       # source: — the publication's name
    site: str = ""         # site: — the host the article was published on

    @classmethod
    def plain(cls, text: str) -> "Text":
        """A bare string, for callers with nothing but the article's text.

        The scoped fields stay empty rather than falling back to the whole
        article: a caller that cannot say what the headline is must not have
        `intitle:` quietly answer as though everything were the headline."""
        return cls(all=text, text=text)

    def field(self, name: str) -> str:
        return {"title": self.headline, "text": self.text,
                "source": self.source, "site": self.site}.get(name, self.all)


def host_of(url: str) -> str:
    """The bare host of an article URL, for `site:`. www. is dropped so
    `site:bbc.co.uk` matches www.bbc.co.uk, which is what anyone means."""
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


# The field prefixes, and the aliases people actually type. Google's names are
# the canonical ones because that is where the habit comes from.
_FIELDS = {
    "intitle": "title", "title": "title", "headline": "title",
    "intext": "text", "text": "text", "body": "text",
    "source": "source", "publication": "source", "outlet": "source",
    "site": "site", "host": "site", "domain": "site",
}
_FIELD_PREFIX_RE = re.compile(r"^(" + "|".join(sorted(_FIELDS)) + r"):(.*)$", re.I)

# Google writes proximity as AROUND(5); this grammar writes NEAR/5. Rewritten
# before tokenizing, because "(" is a grouping token here and `AROUND (5) x`
# would otherwise parse as an AND of the number 5.
_AROUND_RE = re.compile(r"\bAROUND\s*(?:\(\s*(\d+)\s*\)|/\s*(\d+))", re.I)
_BARE_AROUND_RE = re.compile(r"\bAROUND\b(?!\s*/)", re.I)


_TOKEN_RE = re.compile(
    r'\s*(?:(?P<lparen>\()|(?P<rparen>\))|(?P<or_>\|\|?)|(?P<and_>&&)'
    r'|(?P<phrase>"[^"]*")|(?P<word>[^\s()"|&]+))'
)

_QUOTE_TRANSLATION = str.maketrans({
    "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
    "‘": "'", "’": "'",
})


def normalize_quotes(text: str) -> str:
    return text.translate(_QUOTE_TRANSLATION)


def tokenize(query: str) -> list[Token]:
    query = normalize_quotes(query)
    query = _AROUND_RE.sub(lambda m: "NEAR/" + (m.group(1) or m.group(2)), query)
    query = _BARE_AROUND_RE.sub("NEAR", query)
    tokens: list[Token] = []
    i = 0
    while i < len(query):
        m = _TOKEN_RE.match(query, i)
        if not m:
            if query[i:].strip():
                raise QueryError(f"Unexpected character at position {i}: {query[i]!r}")
            break
        i = m.end()
        if m.group("lparen"):
            tokens.append(Token("LPAREN", "(", m.start()))
        elif m.group("rparen"):
            tokens.append(Token("RPAREN", ")", m.start()))
        elif m.group("or_"):
            tokens.append(Token("OR", "OR", m.start()))
        elif m.group("and_"):
            tokens.append(Token("AND", "AND", m.start()))
        elif m.group("phrase") is not None:
            phrase = m.group("phrase")[1:-1].strip()
            if not phrase:
                raise QueryError(f"Empty phrase at position {m.start()}")
            tokens.append(Token("TERM", phrase, m.start()))
        else:
            word = m.group("word")
            upper = word.upper()
            if upper in ("AND", "OR", "NOT"):
                tokens.append(Token(upper, upper, m.start()))
            elif upper == "NEAR" or upper.startswith("NEAR/"):
                if upper == "NEAR":
                    n = 10
                else:
                    try:
                        n = int(upper.split("/", 1)[1])
                    except ValueError:
                        raise QueryError(
                            f"NEAR needs a whole number, e.g. NEAR/5 (position {m.start()})")
                    if not 1 <= n <= 100:
                        raise QueryError("NEAR distance must be between 1 and 100")
                tokens.append(Token("NEAR", str(n), m.start()))
            elif word.startswith("-") and len(word) > 1:
                # Google-style negation: -sports == NOT sports, and -site:x too.
                tokens.append(Token("NOT", "NOT", m.start()))
                tokens.extend(_word_tokens(word[1:], m.start() + 1))
            elif word.startswith("~"):
                # Google's synonym operator. Delphi has no thesaurus, and the
                # honest answer is the one that says so: this used to compile to
                # \b~academic\b and match nothing, forever, in silence.
                raise QueryError(
                    f"~ (synonyms) isn't supported at position {m.start()} — there "
                    f"is no thesaurus here to expand it. Write the alternatives "
                    f"yourself: ({word[1:]} OR …).")
            else:
                tokens.extend(_word_tokens(word, m.start()))
    return tokens


def _word_tokens(word: str, pos: int) -> list[Token]:
    """One bare word, which may carry a `field:` prefix.

    A prefix with nothing after it (`intitle:"sleep deprivation"`) scopes
    whatever comes next; the parser picks that up from the FIELD token.
    """
    m = _FIELD_PREFIX_RE.match(word)
    if not m:
        return [Token("TERM", word, pos)]
    field, rest = _FIELDS[m.group(1).lower()], m.group(2)
    if not rest:
        return [Token("FIELD", field, pos)]
    return [Token("FIELD", field, pos), Token("TERM", rest, pos + len(m.group(1)) + 1)]


def _word_pattern(word: str) -> str:
    """One word to regex source; * and ? are wildcards, the rest is literal."""
    out, literals = [], 0
    for ch in word:
        if ch == "*":
            out.append(r"\w*")
        elif ch == "?":
            out.append(r"\w")
        else:
            out.append(re.escape(ch))
            literals += 1
    if literals < 2 and ("*" in word or "?" in word):
        raise QueryError(
            f"Wildcard term {word!r} needs at least two literal characters")
    return "".join(out)


def _term_pattern(term: str) -> str:
    # Multi-word phrases tolerate any whitespace between words.
    return r"\s+".join(_word_pattern(p) for p in term.split())


# Scripts written without spaces between words: CJK ideographs and the kana,
# Hangul, and Thai. A word boundary is a transition between a word character
# and a non-word one, and in these scripts every character is a word
# character — so "\b地震\b" inside "大規模な地震が発生" has no boundary on either
# side and never matches. Neither does any other term a reader might write in
# Chinese, Japanese, Korean or Thai.
#
# The failure was total and silent: the query parsed, validated, and returned
# nothing for ever. `scoring.py` found this first and documented it for
# breaking-news terms; the Boolean engine kept using boundaries everywhere and
# so could not be used at all in four of the languages Delphi reads.
_UNSPACED_SCRIPT = re.compile(
    r"[\u3040-\u30ff"      # kana
    r"\u3400-\u4dbf"       # CJK extension A
    r"\u4e00-\u9fff"       # CJK unified ideographs
    r"\uf900-\ufaff"       # CJK compatibility ideographs
    r"\uac00-\ud7a3"       # Hangul syllables
    r"\u1100-\u11ff"       # Hangul jamo
    r"\u0e00-\u0e7f]")     # Thai


def unspaced(term: str) -> bool:
    """Is this term written in a script with no spaces between words?"""
    return bool(_UNSPACED_SCRIPT.search(term or ""))


def _edge(term: str, side: str) -> str:
    """The boundary assertion to use at one end of a term.

    Chosen per end rather than per term, because a mixed term — "AI기업",
    "5G네트워크", which is how these languages actually write about
    technology — needs a real boundary on its Latin end and none on its CJK
    end. Using the same rule for both would either lose the mixed terms or
    give the Latin ones the substring matching they must not have.
    """
    if not term:
        return ""
    char = term[0] if side == "start" else term[-1]
    if _UNSPACED_SCRIPT.match(char):
        return ""
    # A wildcard end has no fixed character to sit against, and \b after \w*
    # is satisfied by anything; leaving it off changes nothing and avoids
    # asserting a boundary the pattern cannot honour.
    if char in "*?":
        return ""
    return r"\b"


def _term_regex(term: str) -> re.Pattern:
    return re.compile(_edge(term, "start") + _term_pattern(term)
                      + _edge(term, "end"), re.IGNORECASE)


def _near_regex(a: str, b: str, n: int) -> re.Pattern:
    """`a` and `b` with at most n words between them, either order.

    "Words" is the wrong unit for a script that does not space them, so for a
    CJK or Thai operand the gap is counted in characters instead — which is
    the closest honest equivalent, and keeps NEAR working rather than silently
    matching nothing.
    """
    pa, pb = _term_pattern(a), _term_pattern(b)
    if unspaced(a) or unspaced(b):
        # Roughly two characters to the word in these scripts, which is what
        # the reader who wrote NEAR/5 is thinking in.
        gap = r".{0,%d}?" % (n * 2)
        return re.compile(rf"(?:{pa}{gap}{pb}|{pb}{gap}{pa})",
                          re.IGNORECASE | re.DOTALL)
    gap = r"(?:\W+\w+){0,%d}?\W+" % n
    return re.compile(rf"\b(?:{pa}{gap}{pb}|{pb}{gap}{pa})\b", re.IGNORECASE)


class _Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.i = 0

    def peek(self) -> Token | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def next(self) -> Token:
        tok = self.peek()
        if tok is None:
            raise QueryError("Unexpected end of query")
        self.i += 1
        return tok

    def parse(self):
        node = self.or_expr()
        if self.peek() is not None:
            tok = self.peek()
            raise QueryError(f"Unexpected {tok.value!r} at position {tok.pos}")
        return node

    def or_expr(self):
        left = self.and_expr()
        while self.peek() and self.peek().kind == "OR":
            self.next()
            right = self.and_expr()
            left = ("or", left, right)
        return left

    def and_expr(self):
        left = self.not_expr()
        while True:
            tok = self.peek()
            if tok is None or tok.kind in ("OR", "RPAREN"):
                return left
            if tok.kind == "AND":
                self.next()
                tok = self.peek()
                if tok is None:
                    raise QueryError("Expected a term after AND")
            right = self.not_expr()
            left = ("and", left, right)

    def not_expr(self):
        tok = self.peek()
        if tok and tok.kind == "NOT":
            self.next()
            return ("not", self.not_expr())
        return self.near_expr()

    def near_expr(self):
        left = self.atom()
        prev = left  # the operand a chained NEAR measures from
        while self.peek() and self.peek().kind == "NEAR":
            op = self.next()
            right = self.atom()
            if prev[0] != "term" or right[0] != "term":
                raise QueryError(
                    f"NEAR (position {op.pos}) works between two words or quoted "
                    "phrases, not groups — write (a NEAR/5 c) OR (b NEAR/5 c) instead")
            pair = ("term", _near_regex(prev[2], right[2], int(op.value)))
            left = pair if left is prev else ("and", left, pair)
            prev = right
        return left

    def atom(self):
        tok = self.next()
        if tok.kind == "LPAREN":
            node = self.or_expr()
            closing = self.peek()
            if closing is None or closing.kind != "RPAREN":
                raise QueryError(f"Missing ')' for '(' at position {tok.pos}")
            self.next()
            return node
        if tok.kind == "FIELD":
            operand = self.peek()
            if operand is None or operand.kind != "TERM":
                raise QueryError(
                    f"{tok.value}: at position {tok.pos} needs a word or a "
                    f'"quoted phrase" straight after it')
            self.next()
            return ("scoped", tok.value, _term_regex(operand.value), operand.value)
        if tok.kind == "TERM":
            return ("term", _term_regex(tok.value), tok.value)
        if tok.kind == "NEAR":
            raise QueryError(f"NEAR at position {tok.pos} needs a word on each side")
        raise QueryError(f"Unexpected {tok.value!r} at position {tok.pos}")


def compile_query(query: str):
    """Compile a boolean query string into predicate(Text | str) -> bool.

    A bare string is accepted and read as `Text.plain` — the whole article with
    no fields, so scoped operators see only what that leaves them.

    Raises QueryError on invalid syntax.
    """
    tokens = tokenize(query)
    if not tokens:
        raise QueryError("Empty query")
    tree = _Parser(tokens).parse()

    def evaluate(node, text: Text) -> bool:
        kind = node[0]
        if kind == "term":
            return bool(node[1].search(text.all))
        if kind == "scoped":
            return bool(node[2].search(text.field(node[1])))
        if kind == "not":
            return not evaluate(node[1], text)
        if kind == "and":
            return evaluate(node[1], text) and evaluate(node[2], text)
        if kind == "or":
            return evaluate(node[1], text) or evaluate(node[2], text)
        raise AssertionError(kind)

    return lambda text: evaluate(
        tree, text if isinstance(text, Text) else Text.plain(text))


# Terms FTS5 cannot be trusted to tokenize the way the Python matcher does:
# wildcards, and scripts SQLite's default tokenizer does not split on.
#
# Built from `_UNSPACED_SCRIPT` rather than from its own list of ranges, and
# that is the whole point of the change. The two lists were written separately
# and drifted: this one covered kana, Han and Thai and omitted Hangul, so a
# Korean term was handed to FTS5, whose tokenizer reads 고용률 as one
# indivisible token and finds no match for 고용 — dropping the row before the
# matcher could ever see it, and breaking the superset guarantee that the whole
# narrowing step depends on.
#
# It was invisible while the matcher rejected every CJK term anyway. Fixing the
# matcher without fixing this would have turned a silent failure into a subtler
# one: Korean feeds that work on a full scan and quietly lose rows as soon as
# the index is used. One definition, used twice, cannot drift again.
FTS_UNSAFE = re.compile(r"[*?]|" + _UNSPACED_SCRIPT.pattern)
_FTS_UNSAFE_TERM = FTS_UNSAFE


def fts_expression(query: str) -> str | None:
    """The query as an FTS5 MATCH expression that matches a *superset* of it —
    or None when no safe superset exists.

    The query keeps its shape on the way to the index. Reducing it to a flat
    set of words instead — which for `(tokyo OR osaka) AND earthquake` leaves
    just {earthquake} — is correct but hands the index a third of the corpus,
    where the intersection is a fraction of it: measured on 244,000 articles,
    78,562 candidate rows become 5,174 and the query 119ms becomes 15ms.

    A NOT branch is dropped rather than translated: FTS's idea of the excluded
    text and the matcher's are not guaranteed to agree, and dropping it only
    widens the candidate set, which the matcher then narrows exactly."""
    try:
        tokens = tokenize(query)
        if not tokens:
            return None
        tree = _Parser(tokens).parse()
    except QueryError:
        return None
    return _fts_node(tree)


def _fts_node(node) -> str | None:
    kind = node[0]
    if kind == "scoped":
        # The index holds title, summary and content, so a term scoped to any of
        # those is still findable through it — unscoped, which is a superset and
        # therefore safe. A term scoped to the publication or the host is not in
        # the index at all, and pretending otherwise would drop real matches, so
        # that branch gives up on narrowing instead.
        if node[1] not in ("title", "text"):
            return None
        return _fts_node(("term", node[2], node[3]))
    if kind == "term":
        # NEAR pairs are ("term", regex) with no original text to search for.
        if len(node) <= 2:
            return None
        word = node[2].lower()
        if not word or _FTS_UNSAFE_TERM.search(word):
            return None
        return '"' + word.replace('"', '""') + '"'
    if kind == "not":
        return None
    if kind == "and":
        left, right = _fts_node(node[1]), _fts_node(node[2])
        if left and right:
            return f"({left} AND {right})"
        return left or right          # one side alone is still a superset
    if kind == "or":
        left, right = _fts_node(node[1]), _fts_node(node[2])
        # An unbounded side makes the whole disjunction unbounded.
        return f"({left} OR {right})" if left and right else None
    return None


def validate_query(query: str) -> str | None:
    """Return an error message, or None if the query is valid."""
    try:
        compile_query(query)
        return None
    except QueryError as exc:
        return str(exc)


def query_terms(query: str) -> list[tuple[str, re.Pattern]]:
    """Every literal word or phrase in a query, with the pattern that finds it.

    For explaining a match back to the reader: "this arrived because the words
    'AI industry' are in it" is a different and far more useful answer than
    "it matched", especially when those words are nowhere they can see.

    NEAR pairs are skipped — they are compiled as one pattern over two operands
    with no single piece of text to point at.
    """
    try:
        tree = _Parser(tokenize(query)).parse()
    except QueryError:
        return []

    out: list[tuple[str, re.Pattern]] = []
    seen: set[str] = set()

    def walk(node):
        if node[0] == "scoped":
            if node[3].lower() not in seen:
                seen.add(node[3].lower())
                out.append((node[3], node[2]))
            return
        if node[0] == "term":
            if len(node) > 2 and node[2].lower() not in seen:
                seen.add(node[2].lower())
                out.append((node[2], node[1]))
            return
        for child in node[1:]:
            walk(child)

    walk(tree)
    return out


def plain_terms(query: str) -> list[tuple[str, re.Pattern]]:
    """Only the terms that search the whole article — no field-scoped ones.

    Used by the passing-mention rule, which asks whether a word is prominent
    enough to be what an article is about. A word the reader already pinned to
    the headline, the publication or the host is not that question: they have
    said where they want it, and where they want it is the answer.
    """
    try:
        tree = _Parser(tokenize(query)).parse()
    except QueryError:
        return []

    out: list[tuple[str, re.Pattern]] = []
    seen: set[str] = set()

    def walk(node):
        if node[0] == "scoped":
            return
        if node[0] == "term":
            if len(node) > 2 and node[2].lower() not in seen:
                seen.add(node[2].lower())
                out.append((node[2], node[1]))
            return
        for child in node[1:]:
            walk(child)

    walk(tree)
    return out


# Bare words that mean one thing in a query and something else in the world.
#
# This exists because of a real report: an energy feed built from
# ("solar" OR "wind" OR "coal" OR "nuclear" OR …) carried a Taiwanese concert
# listing. Nothing was broken — the page lists the performer's songs and one is
# called "Innocent Wind", so the article genuinely contains the word. A search
# for a bare common word finds every use of it, and most uses of these words
# are not about energy.
#
# A list, not a language model, and deliberately short: only words that are
# both frequently searched for in a specialist sense and frequently written in
# an everyday one. The suggestions are the pairing that fixes it, because
# "your term is ambiguous" without a next move is just a complaint.
AMBIGUOUS_TERMS: dict[str, str] = {
    "wind":     '"wind power", "wind farm", "offshore wind"',
    "solar":    '"solar power", "solar farm", "solar panel"',
    "coal":     '"coal-fired", "coal plant", "coal mine"',
    "nuclear":  '"nuclear power", "nuclear plant", "nuclear reactor"',
    "gas":      '"natural gas", "gas pipeline", "gas field"',
    "oil":      '"crude oil", "oil field", "oil refinery"',
    "power":    '"power plant", "power grid", "power outage"',
    "plant":    '"power plant", "chemical plant", "plant closure"',
    "mine":     '"coal mine", "gold mine", "mine collapse"',
    "grid":     '"power grid", "grid operator"',
    "strike":   '"labour strike", "air strike", "strike action"',
    "crash":    '"plane crash", "market crash", "car crash"',
    "fire":     '"wildfire", "house fire", "fire crews"',
    "storm":    '"tropical storm", "winter storm", "storm damage"',
    "shell":    '"Shell plc", "shell company", "artillery shell"',
    "apple":    '"Apple Inc", "Apple iPhone"',
    "amazon":   '"Amazon.com", "Amazon rainforest"',
    "meta":     '"Meta Platforms", "Meta AI"',
    "target":   '"Target Corp", "target of"',
    "delta":    '"Delta Air Lines", "Nile delta", "Delta variant"',
    "orange":   '"Orange SA", "Orange County"',
}


def ambiguous_terms(query: str) -> list[str]:
    """The bare one-word terms in `query` that are known to over-match.

    Only bare words count. A phrase is already specific — somebody who wrote
    "wind power" has done the very thing this would advise — and a wildcard is
    a deliberate widening, which is a different decision and not one to
    second-guess.
    """
    try:
        tokens = tokenize(query)
    except QueryError:
        return []
    seen: list[str] = []
    scoped_next = False
    for tok in tokens:
        if tok.kind == "FIELD":
            scoped_next = True
            continue
        if tok.kind != "TERM":
            continue
        if scoped_next:
            # `intitle:wind` is not the bare word this warns about: the reader
            # has already said where they want it, which is most of the advice.
            scoped_next = False
            continue
        word = tok.value.strip().lower()
        if " " in word or "*" in word or "?" in word:
            continue
        if word in AMBIGUOUS_TERMS and word not in seen:
            seen.append(word)
    return seen


def query_advisories(query: str) -> list[str]:
    """Things a valid query probably does not mean. Never an error.

    AND binds tighter than OR, as everywhere else, so

        "data center" OR datacenter NOT sports

    is `"data center" OR (datacenter AND NOT sports)` — the exclusion applies
    to the second alternative and nothing else, and sports coverage matching
    the first one comes through untouched. That is the standard reading and
    the engine is right to use it, but almost nobody writing that line means
    it: they mean "either of these, and never sports". The precedence is not
    something to change, because it would silently alter every query already
    saved. Saying so is.
    """
    try:
        tokens = tokenize(query)
        _Parser(tokens).parse()          # advice on a broken query is noise
    except QueryError:
        return []

    notes: list[str] = []
    depth = 0
    or_at_depth: set[int] = set()
    for tok in tokens:
        if tok.kind == "LPAREN":
            depth += 1
        elif tok.kind == "RPAREN":
            or_at_depth.discard(depth)
            depth = max(0, depth - 1)
        elif tok.kind == "OR":
            or_at_depth.add(depth)
        elif tok.kind == "NOT" and depth in or_at_depth:
            notes.append(
                "NOT applies only to the alternative just before it, because "
                "AND binds tighter than OR. Put the OR list in brackets — "
                "(a OR b) NOT c — to exclude from all of it.")
            break

    bare = ambiguous_terms(query)
    if bare:
        for word in bare[:3]:      # three is enough to make the point
            notes.append(
                f"“{word}” on its own matches every use of the word, not just "
                f"the one you mean — song titles, company names and ordinary "
                f"prose included. Pairing it with its context is usually what "
                f"was intended: {AMBIGUOUS_TERMS[word]}.")
        rest = len(bare) - 3
        if rest > 0:
            notes.append(f"{rest} more bare term{'s' if rest > 1 else ''} in this "
                         f"query {'have' if rest > 1 else 'has'} the same problem: "
                         f"{', '.join(bare[3:])}.")
    return notes
