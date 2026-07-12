"""Lightweight account auth: PBKDF2 password hashing and HMAC-signed tokens.

No external dependencies. The signing secret comes from NEWS_SECRET or is
generated once and persisted next to the database. Tokens are
"base64(user_id:expiry).hmac" with a 30-day lifetime.

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

from .database import DB_DIR

TOKEN_TTL_SECONDS = 30 * 86400
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


def make_token(user_id: int) -> str:
    payload = f"{user_id}:{int(time.time()) + TOKEN_TTL_SECONDS}"
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


def parse_token(token: str) -> int | None:
    """Return the user id for a valid, unexpired token; None otherwise."""
    try:
        payload_b64, sig = token.split(".", 1)
        expected = hmac.new(_secret(), payload_b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        user_id_str, expiry_str = base64.urlsafe_b64decode(padded).decode().split(":", 1)
        if int(expiry_str) < time.time():
            return None
        return int(user_id_str)
    except (ValueError, TypeError):
        return None
