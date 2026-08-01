"""Two ways an ordinary account could reach further than it should: making the
server fetch things inside the network, and starting jobs that walk the whole
archive."""
import httpx
import pytest

from backend.app import safefetch


# ---------- what Delphi will and will not fetch ----------

@pytest.mark.parametrize("url,why", [
    ("http://127.0.0.1/feed", "loopback"),
    ("http://127.0.0.1:8000/api/admin/users", "loopback with a port"),
    ("http://localhost/feed", "loopback by name"),
    ("http://[::1]/feed", "loopback over IPv6"),
    ("http://10.0.0.5/feed", "private v4"),
    ("http://192.168.1.1/feed", "private v4"),
    ("http://172.16.4.4/feed", "private v4"),
    ("http://169.254.169.254/latest/meta-data/", "link-local — the metadata address"),
    ("http://[fdaa::3]/feed", "Fly's private 6PN range"),
    ("http://[fe80::1]/feed", "link-local v6"),
    ("http://0.0.0.0/feed", "unspecified"),
    ("http://[::ffff:127.0.0.1]/feed", "loopback wearing an IPv6 mapping"),
    ("file:///etc/passwd", "not http"),
    ("gopher://example.com/", "not http"),
    ("http://example.com:22/", "a port that serves no feeds"),
    ("http://example.com:6379/", "a port that serves no feeds"),
])
def test_delphi_refuses_to_fetch_inward(url, why):
    with pytest.raises(safefetch.BlockedURL):
        safefetch.check_url(url)
    assert safefetch.is_allowed(url) is False, why


@pytest.mark.parametrize("url", [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "http://example.com/rss",
    "https://example.com:443/rss",
    "http://example.com:80/rss",
])
def test_ordinary_feeds_are_unaffected(url):
    assert safefetch.check_url(url) == url


def test_a_name_that_will_not_resolve_is_refused_rather_than_attempted():
    """Failing closed: an unknown host is not worth a connection attempt."""
    with pytest.raises(safefetch.BlockedURL) as caught:
        safefetch.check_url("http://no-such-host.invalid/feed")
    assert "could not be resolved" in str(caught.value)


def test_the_refusal_says_what_was_wrong():
    """An operator who typed an internal address deserves to know why it was
    rejected, not a blank failure."""
    with pytest.raises(safefetch.BlockedURL) as caught:
        safefetch.check_url("http://192.168.0.10/feed")
    message = str(caught.value)
    assert "192.168.0.10" in message and "private or local" in message


def test_a_redirect_into_the_network_is_blocked_too(monkeypatch):
    """The check that matters. A public host can answer 302 and send the
    fetcher anywhere, so validating a URL when it is saved is not enough —
    every hop has to be checked, which is why the guard is in the transport
    rather than at the call site.

    The real _GuardedTransport runs here; only the network underneath it (its
    parent's handle_async_request) is replaced.
    """
    import asyncio
    import ipaddress

    hops = []

    async def fake_network(self, request):
        hops.append(str(request.url))
        if request.url.host == "outlet.example":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:8000/secrets"},
                                  request=request)
        return httpx.Response(200, text="never reached", request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_network)
    # A public name has to resolve to a public address for the first hop to be
    # allowed at all; the guard's subject is the address, not the lookup.
    monkeypatch.setattr(safefetch, "_resolve",
                        lambda host: [ipaddress.ip_address("93.184.216.34")])

    async def go():
        async with safefetch.client(timeout=5) as c:
            return await c.get("http://outlet.example/feed")

    with pytest.raises(safefetch.BlockedURL):
        asyncio.run(go())
    # It reached the public host and refused the hop inward.
    assert hops == ["http://outlet.example/feed"]


def test_the_guarded_client_is_what_every_fetcher_uses():
    """A guard only helps if nothing bypasses it. Any httpx.AsyncClient built
    directly in a module that fetches user-supplied URLs is a hole."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "backend" / "app"
    # geocode and translate talk to one endpoint the operator configured, not
    # to anything a user can name, so they are deliberately not on this list.
    for name in ("ingest.py", "discovery.py", "repair.py", "content.py", "main.py"):
        text = (root / name).read_text()
        assert "httpx.AsyncClient(" not in text, (
            f"{name} builds an unguarded client; use safefetch.client()")


# ---------- what an ordinary account may set the server to fetch ----------

def test_a_source_cannot_be_pointed_at_the_network_it_runs_in(client, register):
    hdr = register("prober")
    r = client.post("/api/sources", headers=hdr, json={
        "name": "Not a feed", "rss_url": "http://169.254.169.254/latest/meta-data/",
        "homepage": "http://example.com", "country": "US", "region": "Global",
        "language": "en", "scope": "national", "categories": [], "tier": 2})
    assert r.status_code == 422
    assert "private or local" in r.json()["detail"]


def test_an_existing_source_cannot_be_repointed_inward(client, register, db):
    from backend.app.models import Source

    hdr = register("repointer")
    src = Source(name="Real", rss_url="http://example.com/rss", scope="national", tier=2)
    db.add(src)
    db.commit()

    r = client.patch(f"/api/sources/{src.id}", headers=hdr,
                     json={"rss_url": "http://127.0.0.1:8000/api/meta"})
    assert r.status_code == 422
    db.refresh(src)
    assert src.rss_url == "http://example.com/rss"


def test_an_alert_webhook_cannot_point_inward(client, register):
    """A webhook is a URL the server posts to on your behalf — the same kind of
    thing as a feed URL, and it was only ever checked for its scheme."""
    hdr = register("hooker")
    r = client.post("/api/alerts", headers=hdr, json={
        "name": "Exfiltrate", "criteria": {"keywords": ["x"]},
        "webhook_url": "http://10.1.2.3/collect"})
    assert r.status_code == 422
    assert "private or local" in r.json()["detail"]

    ok = client.post("/api/alerts", headers=hdr, json={
        "name": "Fine", "criteria": {"keywords": ["x"]},
        "webhook_url": "https://example.com/collect"})
    assert ok.status_code in (200, 201), ok.text


# ---------- what an ordinary account may ask the server to do ----------

WHOLE_ARCHIVE_JOBS = [
    ("/api/events/rebuild", "re-clusters every article"),
    ("/api/maintenance/reclassify", "re-classifies every article"),
    ("/api/maintenance/detect-languages", "re-detects every language"),
    ("/api/maintenance/fetch-content", "fetches up to a thousand pages"),
    ("/api/sources/seed-cities", "adds five hundred sources"),
]


@pytest.mark.parametrize("path,what", WHOLE_ARCHIVE_JOBS)
def test_an_ordinary_account_cannot_start_a_whole_archive_job(client, register, path, what):
    """One machine serves every request and runs the poller. Any signed-in
    account being able to start these is a self-inflicted outage."""
    hdr = register("ordinary" + path.count("/") * "x" + path[-4:].strip("/-"))
    r = client.post(path, headers=hdr)
    assert r.status_code == 403, f"{path} ({what}) is not gated"
    assert "operator" in r.json()["detail"].lower()


@pytest.mark.parametrize("path,what", WHOLE_ARCHIVE_JOBS)
def test_an_operator_still_can(client, register, db, path, what):
    from backend.app.models import User

    hdr = register("boss" + path[-5:].strip("/-"))
    db.query(User).update({User.is_admin: True})
    db.commit()
    r = client.post(path, headers=hdr)
    assert r.status_code == 200, f"{path} ({what}) broke for operators: {r.text}"


def test_deleting_a_shared_source_is_an_operators_job(client, register, db):
    """It removes the outlet for every reader and takes its articles with it.
    Disabling has the same effect on one board and can be undone."""
    from backend.app.models import Source, User

    hdr = register("deleter")
    src = Source(name="Shared", rss_url="http://example.com/shared", scope="national", tier=2)
    db.add(src)
    db.commit()

    assert client.delete(f"/api/sources/{src.id}", headers=hdr).status_code == 403
    # Disabling it, which anyone may do, is unaffected.
    assert client.patch(f"/api/sources/{src.id}", headers=hdr,
                        json={"enabled": False}).status_code == 200

    db.query(User).update({User.is_admin: True})
    db.commit()
    assert client.delete(f"/api/sources/{src.id}", headers=hdr).status_code == 204


def test_the_manual_poll_stays_open_to_readers(client, register):
    """It is the one thing a reader can do about a stale-looking feed, and only
    one cycle runs at a time — so it is rate-limited rather than gated."""
    hdr = register("refresher")
    r = client.post("/api/ingest/run", headers=hdr)
    assert r.status_code != 403, "readers must keep the Refresh button"


def test_saving_tolerates_a_name_that_will_not_resolve_yet(client, register):
    """A webhook whose host is still being set up, or an outlet having a bad
    DNS day, is not a security problem — the fetch-time guard refuses it if it
    ever resolves inward. Saving rejects what is known to be bad; fetching
    fails closed."""
    # Known-bad is still refused at save time.
    assert not safefetch.is_allowed("http://10.0.0.1/hook")
    # Merely unknown is not.
    assert safefetch.check_url("https://not-yet.invalid/hook",
                               unresolvable_ok=True) == "https://not-yet.invalid/hook"
    with pytest.raises(safefetch.BlockedURL):
        safefetch.check_url("https://not-yet.invalid/hook")   # fetch time

    hdr = register("futurehook")
    r = client.post("/api/alerts", headers=hdr, json={
        "name": "Later", "criteria": {"keywords": ["x"]},
        "webhook_url": "https://hooks.example.com/x"})
    assert r.status_code in (200, 201), r.text
