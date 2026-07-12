"""User-defined boolean query strings over article text.

Grammar (case-insensitive keywords):
    expr     := or_expr
    or_expr  := and_expr ( OR and_expr )*
    and_expr := not_expr ( [AND] not_expr )*     # adjacency = implicit AND
    not_expr := NOT not_expr | near_expr
    near_expr:= atom ( NEAR[/n] atom )*          # operands: words/phrases only
    atom     := '(' expr ')' | "quoted phrase" | word

Words and phrases match on word boundaries, case-insensitively.
Example:  ("supply chain" OR semiconductor) AND (china OR taiwan) NOT rumor

Additional operator types:
  - Wildcards inside words/phrases: `*` = any run of letters/digits
    (strik* matches strike/strikes/striking), `?` = exactly one character
    (organi?ation). A wildcarded word needs at least two literal characters.
  - Proximity: `a NEAR/5 b` matches when the two terms occur with at most
    5 words between them, in either order; bare NEAR defaults to NEAR/10.
    Chains (`a NEAR/3 b NEAR/3 c`) require each adjacent pair to be close.

Google-style input is accepted too, since users paste queries they first
tried in a search engine: curly “smart quotes” are normalized, a leading
minus negates a term (-sports == NOT sports), and | / || mean OR (&& = AND).
"""
from __future__ import annotations

import re
from dataclasses import dataclass


class QueryError(ValueError):
    pass


@dataclass
class Token:
    kind: str  # AND OR NOT NEAR LPAREN RPAREN TERM
    value: str
    pos: int


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
                # Google-style negation: -sports == NOT sports
                tokens.append(Token("NOT", "NOT", m.start()))
                tokens.append(Token("TERM", word[1:], m.start() + 1))
            else:
                tokens.append(Token("TERM", word, m.start()))
    return tokens


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


def _term_regex(term: str) -> re.Pattern:
    return re.compile(r"\b" + _term_pattern(term) + r"\b", re.IGNORECASE)


def _near_regex(a: str, b: str, n: int) -> re.Pattern:
    """`a` and `b` with at most n words between them, either order."""
    gap = r"(?:\W+\w+){0,%d}?\W+" % n
    pa, pb = _term_pattern(a), _term_pattern(b)
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
        if tok.kind == "TERM":
            return ("term", _term_regex(tok.value), tok.value)
        if tok.kind == "NEAR":
            raise QueryError(f"NEAR at position {tok.pos} needs a word on each side")
        raise QueryError(f"Unexpected {tok.value!r} at position {tok.pos}")


def compile_query(query: str):
    """Compile a boolean query string into predicate(text) -> bool.

    Raises QueryError on invalid syntax.
    """
    tokens = tokenize(query)
    if not tokens:
        raise QueryError("Empty query")
    tree = _Parser(tokens).parse()

    def evaluate(node, text: str) -> bool:
        kind = node[0]
        if kind == "term":
            return bool(node[1].search(text))
        if kind == "not":
            return not evaluate(node[1], text)
        if kind == "and":
            return evaluate(node[1], text) and evaluate(node[2], text)
        if kind == "or":
            return evaluate(node[1], text) or evaluate(node[2], text)
        raise AssertionError(kind)

    return lambda text: evaluate(tree, text)


def validate_query(query: str) -> str | None:
    """Return an error message, or None if the query is valid."""
    try:
        compile_query(query)
        return None
    except QueryError as exc:
        return str(exc)
