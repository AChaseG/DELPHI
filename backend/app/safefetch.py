"""Keep Delphi's outbound requests pointed at the public internet.

Delphi fetches URLs that its users choose: a source's feed, an article's page,
a candidate feed found while repairing a source, and an alert's webhook. Every
one of those is a way to ask the server to make a request on your behalf — and
a server will happily fetch things a browser never could. On this deployment
that means the machine's own ports and Fly's private 6PN network, where other
apps sit behind no authentication because they are "internal".

So every outbound request is checked against where it actually resolves, and
anything that is not a public address is refused.

Two properties matter more than the list of ranges:

*Redirects are covered.* Checking a URL when it is saved is not enough — the
host can answer 302 and send the fetcher wherever it likes. The check lives in
an httpx transport, which httpx re-enters for every hop, so each redirect is
validated the same as the original.

*Fetching fails closed; saving does not.* At fetch time a hostname that will
not resolve is refused rather than attempted. When a URL is merely being saved,
an unresolvable name is allowed through — it may be a webhook whose host is
still being set up — because the fetch-time check is what actually protects the
network. Saving rejects what is known to be bad and stays quiet about what it
cannot yet see.

What this deliberately does not cover: the endpoints an *operator* configures
in the environment — a Nominatim mirror, a LibreTranslate instance. Whoever
sets those already runs the server, and pointing them at a box on the LAN is a
normal thing to want when self-hosting. The guard is for URLs that arrive from
whoever signed up, not from whoever deployed.

Known limit: a hostname is resolved here and resolved again by the connection,
so a DNS entry that changes between the two could slip past (a "rebinding"
attack). Closing that needs the connection pinned to the address that was
checked, which httpx does not expose cleanly. It is a much narrower hole than
the one this closes, and it is documented rather than hidden.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx

# Ports worth talking to. Anything else on a public host is far more likely to
# be someone probing an internal service than a news feed.
ALLOWED_PORTS = {80, 443}
ALLOWED_SCHEMES = ("http", "https")


class BlockedURL(ValueError):
    """A URL Delphi will not fetch, with a reason fit to show someone."""


def _address_is_public(ip: ipaddress._BaseAddress) -> bool:
    """Everything that is not the public internet.

    `is_global` alone is not enough: it is False for some ranges we want to
    name individually, and IPv4-mapped IPv6 (::ffff:127.0.0.1) has to be
    unwrapped or a loopback address arrives dressed as a v6 one.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def _resolve(host: str) -> list[ipaddress._BaseAddress]:
    try:
        info = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise BlockedURL(f"“{host}” could not be resolved ({exc.strerror or exc})") from exc
    addresses = []
    for family, _type, _proto, _canon, sockaddr in info:
        try:
            addresses.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    if not addresses:
        raise BlockedURL(f"“{host}” resolved to no usable address")
    return addresses


def check_url(url: str, *, unresolvable_ok: bool = False) -> str:
    """Return the URL, or raise BlockedURL saying why it was refused.

    `unresolvable_ok` is for the moment a URL is *saved* rather than fetched. A
    name that will not resolve right now may still be a perfectly good webhook
    whose host is being set up, or an outlet having a bad DNS day, and refusing
    it then buys nothing: the fetch-time check is what actually protects the
    network, and it will refuse the address if it ever resolves inward. So
    saving rejects what is known to be bad and stays quiet about what it merely
    cannot see, while fetching fails closed.
    """
    raw = (url or "").strip()
    if not raw:
        raise BlockedURL("The URL is empty")
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise BlockedURL(
            f"Only http:// and https:// addresses can be fetched, not "
            f"“{parsed.scheme or raw[:20]}”")
    host = parsed.hostname
    if not host:
        raise BlockedURL("That URL has no host in it")

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise BlockedURL(
            f"Port {port} is not one Delphi will fetch from — feeds are served "
            f"on 80 or 443")

    # A literal address skips DNS entirely; a name has to be looked at through
    # every address it answers with, because one public A record does not make
    # a host safe if its AAAA record points inward.
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = _resolve(host)
        except BlockedURL:
            if unresolvable_ok:
                return raw
            raise

    for ip in addresses:
        if not _address_is_public(ip):
            raise BlockedURL(
                f"“{host}” resolves to {ip}, which is a private or local "
                f"address. Delphi only fetches from the public internet — this "
                f"is what stops it being used to reach services inside the "
                f"network it runs on.")
    return raw


def is_allowed(url: str) -> bool:
    """check_url as a question rather than an exception."""
    try:
        check_url(url)
        return True
    except BlockedURL:
        return False


class _GuardedTransport(httpx.AsyncHTTPTransport):
    """An httpx transport that refuses anything check_url refuses.

    httpx re-enters the transport for each redirect hop, which is exactly why
    the check belongs here: a host that answers 302 to http://127.0.0.1 gets
    the same treatment as one that was typed in.
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        check_url(str(request.url))
        return await super().handle_async_request(request)


def client(**kwargs) -> httpx.AsyncClient:
    """An httpx.AsyncClient that can only reach public addresses.

    Every outbound fetch of a user-supplied URL goes through one of these.
    """
    kwargs.setdefault("follow_redirects", True)
    return httpx.AsyncClient(transport=_GuardedTransport(), **kwargs)
