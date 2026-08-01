"""Who does the rate limiter think is calling?

Every limit in Delphi — sign-in, registration, password reset, geocoding,
manual polling — is keyed on one string, so the question this file asks is the
only one that decides whether any of them work. The failure mode is not a
limit that is slightly too loose; it is a limit that is not there at all,
while still looking present in the code and returning 429 to honest traffic.

The specific trap: X-Forwarded-For is *appended* to by each proxy, so its
first entry is whatever the caller typed. Keying on it gives an attacker a
fresh bucket per request just by counting upwards.
"""
import time

import pytest
from starlette.requests import Request

from backend.app import ratelimit


def req(peer="198.51.100.9", xff=None, extra=None):
    """A request as it arrives at the app: socket peer plus whatever headers."""
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    for name, value in (extra or {}).items():
        headers.append((name.encode(), value.encode()))
    return Request({"type": "http", "method": "GET", "path": "/",
                    "headers": headers,
                    "client": (peer, 4321) if peer else None})


def through_proxy(real_client, forged=None, inner=()):
    """The header a proxy in front of us produces.

    It appends the address it heard from, so the attacker owns only the prefix:
    anything in `forged` is theirs, `real_client` is the proxy's own doing.
    """
    return ", ".join([*( [forged] if forged else [] ), real_client, *inner])


@pytest.fixture
def direct(monkeypatch):
    monkeypatch.setattr(ratelimit, "TRUSTED_PROXIES", 0)
    monkeypatch.setattr(ratelimit, "CLIENT_IP_HEADER", "")


@pytest.fixture
def behind_proxy(monkeypatch):
    monkeypatch.setattr(ratelimit, "TRUSTED_PROXIES", 1)
    monkeypatch.setattr(ratelimit, "CLIENT_IP_HEADER", "")


# ---------- exposed directly: the header must not be believed at all ----------

def test_direct_uses_the_socket_peer(direct):
    assert ratelimit.client_ip(req(peer="198.51.100.9")) == "198.51.100.9"


def test_direct_ignores_a_forged_header(direct):
    """With nothing in front of us, X-Forwarded-For is pure fiction."""
    spoofed = req(peer="198.51.100.9", xff="9.9.9.9")
    assert ratelimit.client_ip(spoofed) == "198.51.100.9"


def test_direct_ignores_a_long_forged_chain(direct):
    spoofed = req(peer="198.51.100.9", xff="9.9.9.9, 8.8.8.8, 7.7.7.7")
    assert ratelimit.client_ip(spoofed) == "198.51.100.9"


# ---------- behind one proxy: believe exactly one hop, no more ----------

def test_proxy_hop_is_read_as_the_client(behind_proxy):
    arrived = req(peer="10.0.0.1", xff=through_proxy("203.0.113.7"))
    assert ratelimit.client_ip(arrived) == "203.0.113.7"


def test_forged_prefix_is_ignored(behind_proxy):
    """The attacker's own entry sits in front of the one the proxy wrote."""
    arrived = req(peer="10.0.0.1",
                  xff=through_proxy("203.0.113.7", forged="9.9.9.9"))
    assert ratelimit.client_ip(arrived) == "203.0.113.7"


def test_forgery_cannot_win_by_being_longer(behind_proxy):
    """Sending fifty fake hops does not push the real one out of reach."""
    forged = ", ".join(f"9.9.9.{i}" for i in range(50))
    arrived = req(peer="10.0.0.1", xff=through_proxy("203.0.113.7", forged=forged))
    assert ratelimit.client_ip(arrived) == "203.0.113.7"


def test_missing_header_falls_back_to_the_peer(behind_proxy):
    """A request that skipped the proxy still has to key on something real."""
    assert ratelimit.client_ip(req(peer="10.0.0.1", xff=None)) == "10.0.0.1"


def test_whitespace_and_empty_entries_are_not_counted_as_hops(behind_proxy):
    arrived = req(peer="10.0.0.1", xff="  , 203.0.113.7 ,")
    assert ratelimit.client_ip(arrived) == "203.0.113.7"


# ---------- two proxies, and depth mismatches ----------

def test_two_proxies_counts_back_two(monkeypatch):
    monkeypatch.setattr(ratelimit, "TRUSTED_PROXIES", 2)
    monkeypatch.setattr(ratelimit, "CLIENT_IP_HEADER", "")
    # client -> edge -> inner -> app: edge wrote the client, inner wrote edge.
    arrived = req(peer="10.0.0.2", xff="203.0.113.7, 10.0.0.1")
    assert ratelimit.client_ip(arrived) == "203.0.113.7"


def test_two_proxies_still_ignores_a_forged_prefix(monkeypatch):
    monkeypatch.setattr(ratelimit, "TRUSTED_PROXIES", 2)
    monkeypatch.setattr(ratelimit, "CLIENT_IP_HEADER", "")
    arrived = req(peer="10.0.0.2", xff="9.9.9.9, 203.0.113.7, 10.0.0.1")
    assert ratelimit.client_ip(arrived) == "203.0.113.7"


def test_chain_shorter_than_configured_depth_does_not_wrap(monkeypatch):
    """Clamping matters: a negative index would read the forged end.

    Python's negative indexing would quietly turn "count back three" on a
    two-long chain into "read from the front" — which is the attacker's entry.
    """
    monkeypatch.setattr(ratelimit, "TRUSTED_PROXIES", 3)
    monkeypatch.setattr(ratelimit, "CLIENT_IP_HEADER", "")
    arrived = req(peer="10.0.0.1", xff="9.9.9.9")
    assert ratelimit.client_ip(arrived) == "9.9.9.9"  # earliest, not "wrapped to"
    # and with no header at all, the peer — never a crash
    assert ratelimit.client_ip(req(peer="10.0.0.1", xff=None)) == "10.0.0.1"


def test_no_client_at_all_is_named_not_crashed(direct):
    assert ratelimit.client_ip(req(peer=None)) == "unknown"


# ---------- an explicitly trusted, overwritten header ----------

def test_trusted_header_is_used_when_configured(monkeypatch):
    monkeypatch.setattr(ratelimit, "CLIENT_IP_HEADER", "fly-client-ip")
    monkeypatch.setattr(ratelimit, "TRUSTED_PROXIES", 1)
    arrived = req(peer="10.0.0.1", xff="9.9.9.9, 203.0.113.7",
                  extra={"fly-client-ip": "198.51.100.4"})
    assert ratelimit.client_ip(arrived) == "198.51.100.4"


def test_trusted_header_absent_falls_through_to_the_chain(monkeypatch):
    monkeypatch.setattr(ratelimit, "CLIENT_IP_HEADER", "fly-client-ip")
    monkeypatch.setattr(ratelimit, "TRUSTED_PROXIES", 1)
    arrived = req(peer="10.0.0.1", xff="9.9.9.9, 203.0.113.7")
    assert ratelimit.client_ip(arrived) == "203.0.113.7"


def test_trusted_header_is_off_by_default():
    """Believing a header nobody overwrites would be the same bug again."""
    assert ratelimit.CLIENT_IP_HEADER == ""


# ---------- nobody else may rewrite the peer out from under us ----------

@pytest.mark.parametrize("launcher", ["Dockerfile", "run.sh"])
def test_uvicorn_is_told_not_to_rewrite_the_client(launcher):
    """The limiter counts back from the socket peer, so the peer must be real.

    Uvicorn's own proxy-header handling rewrites scope["client"] from
    X-Forwarded-For, and with FORWARDED_ALLOW_IPS="*" — which the usual Fly
    guides recommend — it takes the header's *first* entry, the forgeable one.
    Delphi does this job in one place; this makes sure the other place stays
    switched off, in both ways the app gets started.
    """
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / launcher).read_text()
    assert "--no-proxy-headers" in text, (
        f"{launcher} starts uvicorn without --no-proxy-headers, so uvicorn will "
        f"rewrite the client address from the same header the rate limiter reads")


# ---------- the setting itself ----------

def _depth_with_env(monkeypatch, **env):
    """Re-import the module so the import-time default is recomputed."""
    import importlib
    for name in ("FLY_APP_NAME", "NEWS_TRUSTED_PROXIES"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    try:
        return importlib.reload(ratelimit).TRUSTED_PROXIES
    finally:
        # Leave the module as the rest of the suite found it.
        for name in ("FLY_APP_NAME", "NEWS_TRUSTED_PROXIES"):
            monkeypatch.delenv(name, raising=False)
        importlib.reload(ratelimit)


def test_depth_defaults_to_one_on_fly(monkeypatch):
    """Behind Fly's proxy, believing nothing would put everyone in one bucket."""
    assert _depth_with_env(monkeypatch, FLY_APP_NAME="delphi-2vir0g") == 1


def test_depth_defaults_to_zero_when_nothing_is_in_front(monkeypatch):
    """Off Fly we cannot assume a proxy — assuming one makes limits forgeable."""
    assert _depth_with_env(monkeypatch) == 0


def test_depth_can_be_set_explicitly(monkeypatch):
    assert _depth_with_env(monkeypatch, FLY_APP_NAME="x",
                           NEWS_TRUSTED_PROXIES="2") == 2


def test_a_misspelled_depth_is_refused_loudly(monkeypatch):
    monkeypatch.setenv("NEWS_TRUSTED_PROXIES", "one")
    with pytest.raises(ValueError, match="NEWS_TRUSTED_PROXIES"):
        ratelimit._int_env("NEWS_TRUSTED_PROXIES", "0")


# ---------- end to end, against the real endpoint ----------

@pytest.fixture
def limited(monkeypatch):
    """Rate limiting on (conftest turns it off), one proxy in front, empty table."""
    monkeypatch.setattr(ratelimit, "ENABLED", True)
    monkeypatch.setattr(ratelimit, "TRUSTED_PROXIES", 1)
    monkeypatch.setattr(ratelimit, "CLIENT_IP_HEADER", "")
    ratelimit._hits.clear()
    yield
    ratelimit._hits.clear()


def _guesses_allowed(client, forged=None, real="203.0.113.7", tries=60):
    """How many sign-in attempts get through before the limiter says stop."""
    allowed = 0
    for i in range(tries):
        resp = client.post(
            "/api/auth/login",
            json={"username": "victim", "password": f"guess{i}"},
            headers={"X-Forwarded-For": through_proxy(
                real, forged=forged(i) if forged else None)})
        if resp.status_code == 429:
            break
        allowed += 1
    return allowed


def test_forging_the_header_does_not_buy_extra_password_guesses(client, limited):
    """The regression this file exists for.

    Before the fix this ran to the loop cap — an attacker could guess passwords
    without limit, forever, by incrementing a header.
    """
    client.post("/api/auth/register", json={
        "username": "victim", "email": "victim@example.com", "password": "correct-horse"})
    limit = ratelimit._LIMITS["login"][0]
    ratelimit._hits.clear()

    assert _guesses_allowed(client, forged=lambda i: f"9.9.9.{i}") == limit


def test_honest_clients_are_still_told_apart(client, limited):
    """The fix must not lump everyone behind the proxy into one bucket.

    Getting this wrong is its own outage: one person mistyping a password
    would lock out everybody else on the deployment.
    """
    client.post("/api/auth/register", json={
        "username": "victim", "email": "victim@example.com", "password": "correct-horse"})
    limit = ratelimit._LIMITS["login"][0]
    ratelimit._hits.clear()

    assert _guesses_allowed(client, real="203.0.113.7") == limit
    # a different person, their own allowance, untouched by the first
    assert _guesses_allowed(client, real="203.0.113.8") == limit


# ---------- the table cannot be grown without bound ----------

def test_lapsed_callers_are_forgotten(limited, monkeypatch):
    """Entries were trimmed inside a key but the key itself lived forever."""
    monkeypatch.setattr(ratelimit, "_last_sweep", time.monotonic() - 3600)
    window = ratelimit._LIMITS["login"][1]
    stale = ("login", "203.0.113.99")
    ratelimit._hits[stale].append(time.monotonic() - window - 60)

    ratelimit.check("login", req(peer="10.0.0.1", xff=through_proxy("203.0.113.1")))

    assert stale not in ratelimit._hits


def test_a_flood_of_addresses_stays_bounded(limited, monkeypatch):
    """With spoofing fixed this needs real addresses, but bound it anyway."""
    monkeypatch.setattr(ratelimit, "MAX_TRACKED", 50)
    for i in range(500):
        ratelimit.check("login", req(peer="10.0.0.1",
                                     xff=through_proxy(f"203.0.113.{i // 250}.{i % 250}")))
    assert len(ratelimit._hits) <= ratelimit.MAX_TRACKED + 1
