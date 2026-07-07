"""User-defined boolean query strings over article text.

Grammar (case-insensitive keywords):
    expr    := or_expr
    or_expr := and_expr ( OR and_expr )*
    and_expr:= not_expr ( [AND] not_expr )*      # adjacency = implicit AND
    not_expr:= NOT not_expr | atom
    atom    := '(' expr ')' | "quoted phrase" | word

Words and phrases match on word boundaries, case-insensitively.
Example:  ("supply chain" OR semiconductor) AND (china OR taiwan) NOT rumor
"""
from __future__ import annotations

import re
from dataclasses import dataclass


class QueryError(ValueError):
    pass


@dataclass
class Token:
    kind: str  # AND OR NOT LPAREN RPAREN TERM
    value: str
    pos: int


_TOKEN_RE = re.compile(
    r'\s*(?:(?P<lparen>\()|(?P<rparen>\))|(?P<phrase>"[^"]*")|(?P<word>[^\s()"]+))'
)


def tokenize(query: str) -> list[Token]:
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
            elif upper == "&&":
                tokens.append(Token("AND", "AND", m.start()))
            elif upper == "||":
                tokens.append(Token("OR", "OR", m.start()))
            else:
                tokens.append(Token("TERM", word, m.start()))
    return tokens


def _term_regex(term: str) -> re.Pattern:
    # Word-boundary match; multi-word phrases tolerate any whitespace between words.
    parts = [re.escape(p) for p in term.split()]
    return re.compile(r"\b" + r"\s+".join(parts) + r"\b", re.IGNORECASE)


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
        return self.atom()

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
            return ("term", _term_regex(tok.value))
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
