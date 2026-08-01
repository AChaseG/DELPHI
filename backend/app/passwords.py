"""Refuse the passwords attackers try first.

Delphi's other defences assume the password is a secret. Rate limiting bounds
how fast someone can guess; revocable sessions bound the damage once they are
in. Neither helps against the realistic case, which is not guessing at all: an
address and password reused from some other site that was breached, tried here
directly. There is nothing to guess and nothing to slow down.

So the check happens at the moment the password is chosen — registration, a
reset, a change, and an operator setting one — rather than at sign-in.

The list is the 10,000 most common, in frequency order (backend/data). Most are
shorter than the eight-character minimum and would be refused anyway; they are
kept because they are the *stems* people pad, and matching the stem is what
makes them earn their place. "monkey" is refused on length, "monkey123" is
refused because of "monkey".

Three things this deliberately is not:

*Not a strength meter.* Counting character classes pushes people towards
"P@ssw0rd1", which is on every list. Length plus "not one of the common ones"
rejects less and refuses more of what actually gets tried.

*Not a network call.* Checking against a breach API would mean every password
choice depends on a third party being up, and on trusting them with a prefix of
its hash. A file that ships with the code has neither problem.

*Not a substitute for a second factor.* This raises the floor. It does nothing
about a password that is strong, unique, and has been read over someone's
shoulder.
"""
from __future__ import annotations

import re
from pathlib import Path

LIST_PATH = Path(__file__).resolve().parents[1] / "data" / "common_passwords.txt"

MIN_LENGTH = 8

# The smallest stem worth checking after stripping a numeric tail. Below this
# nearly everything matches something and the rule stops meaning anything:
# "a1" would reduce to "a".
_MIN_STEM = 4

# Digits and punctuation people append to reach a length rule. Stripped so the
# word underneath is what gets checked. Two patterns rather than one, because
# the list holds both the bare word and some padded forms: "trustno1" is an
# entry in its own right, so "trustno1!" has to be tried with only the
# punctuation removed as well as with the digit removed too.
_PUNCT_TAIL = re.compile(r"[!@#$%^&*_.\-+=~?]+$")
_TAIL = re.compile(r"[0-9!@#$%^&*_.\-+=~?]+$")

# The usual substitutions, undone. "p@ssw0rd" is not in the list; "password"
# very much is, and they are the same idea to everyone except a string compare.
#
# Two tables because "1" is ambiguous — it stands for l in "l1nux" and for i in
# "adm1n", both common — so both readings are tried rather than guessing which
# was meant. Without the second, "l3tm31n" does not resolve to "letmein".
_LEET_BASE = {"@": "a", "0": "o", "3": "e", "4": "a",
              "5": "s", "$": "s", "7": "t", "8": "b"}
_LEET_TABLES = (str.maketrans({**_LEET_BASE, "1": "l"}),
                str.maketrans({**_LEET_BASE, "1": "i"}))


class WeakPassword(ValueError):
    """A password Delphi will not accept, with a reason fit to show someone."""


def _load() -> frozenset[str]:
    try:
        lines = LIST_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        # A missing list must not stop anyone signing in. The length and
        # look-alike rules below still apply; only the list check goes quiet.
        return frozenset()
    return frozenset(ln.strip().lower() for ln in lines
                     if ln.strip() and not ln.startswith("#"))


COMMON = _load()


def _variants(password: str) -> set[str]:
    """The forms of this password worth looking up.

    Case-folded, with a padding tail removed, and with leetspeak undone — and
    every combination of those, applied until nothing new appears. The order
    matters and no single order is right: "P@ssw0rd1" de-leeted first becomes
    "passwordl", because the trailing 1 is read as an l and the digit-strip
    never fires. Stripping first and de-leeting after finds "password".
    """
    forms = {password.strip().lower()}
    for _ in range(4):          # converges in two or three; bounded regardless
        grown = set(forms)
        for form in forms:
            for table in _LEET_TABLES:
                grown.add(form.translate(table))
            for pattern in (_PUNCT_TAIL, _TAIL):
                stem = pattern.sub("", form)
                if len(stem) >= _MIN_STEM:
                    grown.add(stem)
        if grown == forms:
            break
        forms = grown
    return forms


def _is_a_single_run(password: str) -> bool:
    """"aaaaaaaa", "12345678", "abcdefgh" — long enough, and no secret at all.

    Long runs are not in the list often enough to rely on it, and someone
    reaching for one is reaching for the first thing that satisfies the counter.
    """
    if len(set(password)) == 1:
        return True
    codes = [ord(c) for c in password.lower()]
    steps = {b - a for a, b in zip(codes, codes[1:])}
    return steps in ({1}, {-1})


def check(password: str, *, username: str = "", email: str = "") -> None:
    """Raise WeakPassword if this is not fit to be one, otherwise return.

    `username` and `email` are optional context: a password built out of the
    name it protects is guessable by anyone who can see the name, and no list
    can know that in advance.
    """
    if len(password) < MIN_LENGTH:
        raise WeakPassword(f"Password must be at least {MIN_LENGTH} characters")

    if password.isdigit():
        # Independent of the list, and it has to be: padding a number with more
        # digits leaves nothing for the stem rule to look up. Eight digits is
        # one of a hundred million, which is seconds of work offline.
        raise WeakPassword(
            "A password of only numbers is far weaker than its length "
            "suggests — an eight-digit one is a few seconds' work to try "
            "exhaustively. Please include some words or letters.")

    if _is_a_single_run(password):
        raise WeakPassword(
            "That is a single run of characters, which is one of the first "
            "things tried. Please choose something less predictable.")

    if COMMON & _variants(password):
        raise WeakPassword(
            "That password is one of the most common in use, so it is among "
            "the first an attacker tries — including the version with digits "
            "on the end. Please choose a different one. A few unrelated words "
            "together make a good password and are easy to remember.")

    lowered = password.lower()
    for label, value in (("username", username), ("email address", email.split("@")[0])):
        value = (value or "").strip().lower()
        # Short names would match half the dictionary; "jo" inside a password
        # says nothing about whether it was chosen from the name.
        if len(value) >= 3 and value in lowered:
            raise WeakPassword(
                f"That password contains your {label}, which is the first "
                f"thing anyone who knows it would try. Please choose another.")


def is_acceptable(password: str, *, username: str = "", email: str = "") -> bool:
    """check() as a question rather than an exception."""
    try:
        check(password, username=username, email=email)
        return True
    except WeakPassword:
        return False
