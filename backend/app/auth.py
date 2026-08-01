"""Lightweight account auth: PBKDF2 password hashing and HMAC-signed tokens.

No external dependencies. The signing secret comes from NEWS_SECRET or is
generated once and persisted next to the database. Tokens are
"base64(user_id:token_version:expiry).hmac" with a 30-day lifetime.

That middle field is what makes a session endable. A signature and an expiry
alone describe a token that is valid for thirty days no matter what happens
afterwards, which leaves someone who believes their account has been broken
into with nothing they can actually do: changing the password does not budge a
token already issued. So the token carries the account's `token_version`, the
account carries the same number, and anything that should end existing sessions
— a password reset, "sign out everywhere" — increments it. Every token minted
before that moment now disagrees with the account and is refused.

The cost is one integer compared against a row that is already being loaded on
each request (the middleware re-reads the account anyway, to catch suspensions
and deletions), so revocation is immediate rather than eventually consistent.

Design note: registered accounts and anonymous browser profiles share the
same Feed.user_id/Alert.user_id string namespace — accounts use the prefix
"acct:<id>", which anonymous ids (random alphanumerics) can never collide
with or spoof.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import NamedTuple

from .database import DB_DIR

TOKEN_TTL_SECONDS = 30 * 86400
# A stream ticket only has to survive the moment between asking for it and the
# EventSource connecting, so it expires long before it could be read out of a
# log and used. Generous enough for a slow phone, short enough to be useless.
STREAM_TICKET_TTL_SECONDS = 60
# Live beside the database so the key persists on the same volume across
# redeploys (otherwise every deploy would invalidate all sessions). Setting
# NEWS_SECRET explicitly still overrides this and is recommended in production.
_SECRET_PATH = DB_DIR / "secret.key"


def _secret() -> bytes:
    env = os.environ.get("NEWS_SECRET")
    if env:
        return env.encode()
    if _SECRET_PATH.exists():
        return _SECRET_PATH.read_bytes()
    secret = secrets.token_bytes(32)
    _SECRET_PATH.write_bytes(secret)
    return secret


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return base64.b64encode(salt).decode() + ":" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_b64, dk_b64 = stored.split(":", 1)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
    except (ValueError, TypeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return hmac.compare_digest(dk, expected)


class Claim(NamedTuple):
    """Who a session token says it is, and which generation of that account."""
    user_id: int
    token_version: int


def make_token(user_id: int, token_version: int = 0) -> str:
    payload = f"{user_id}:{token_version}:{int(time.time()) + TOKEN_TTL_SECONDS}"
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def make_scoped_token(purpose: str, user_id: int, ttl_seconds: int) -> str:
    """One-purpose token (email verification, password reset) — cannot be
    used as a session token and vice versa."""
    payload = f"{purpose}:{user_id}:{int(time.time()) + ttl_seconds}"
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def parse_scoped_token(purpose: str, token: str) -> int | None:
    try:
        payload_b64, sig = token.split(".", 1)
        expected = hmac.new(_secret(), payload_b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        got_purpose, user_id_str, expiry_str = base64.urlsafe_b64decode(padded).decode().split(":", 2)
        if got_purpose != purpose or int(expiry_str) < time.time():
            return None
        return int(user_id_str)
    except (ValueError, TypeError):
        return None


def parse_token(token: str) -> Claim | None:
    """Return the claim a valid, unexpired token makes; None otherwise.

    The caller still has to check `token_version` against the account — this
    function only reports what the token says, since it has no database.

    Tokens minted before token_version existed had two fields rather than
    three, and are refused here rather than read as version 0. Treating them as
    version 0 would have kept everyone signed in across the upgrade, at the
    price of leaving every already-stolen token immune to the revocation being
    added — precisely the tokens this is for. One sign-in is the cheaper half
    of that trade, and it is also the clean sweep the account never had.
    """
    try:
        payload_b64, sig = token.split(".", 1)
        expected = hmac.new(_secret(), payload_b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        user_id_str, version_str, expiry_str = (
            base64.urlsafe_b64decode(padded).decode().split(":", 2))
        if int(expiry_str) < time.time():
            return None
        return Claim(int(user_id_str), int(version_str))
    except (ValueError, TypeError):
        return None


def make_stream_ticket(user_id: int) -> str:
    """A one-minute credential for opening the event stream.

    EventSource cannot set an Authorization header, so whatever authenticates
    the stream has to travel in the URL — where it reaches the access log of
    every proxy in between, and stays there. A session token in that position
    is a thirty-day credential sitting in log files; this is a sixty-second one
    that opens exactly one thing.
    """
    return make_scoped_token("stream", user_id, STREAM_TICKET_TTL_SECONDS)


def parse_stream_ticket(ticket: str) -> int | None:
    return parse_scoped_token("stream", ticket)
