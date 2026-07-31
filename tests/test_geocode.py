"""Address lookup: what the gazetteer cannot answer, and at what cost.

Nothing here touches the network. The provider is pointed at a stub, so what is
tested is Delphi's side of the arrangement: the gazetteer answers first, a
lookup only happens when it falls short, results are cached and paced, and an
outage costs the reader nothing but the addresses.
"""
import asyncio

import httpx
import pytest

from backend.app import geocode


def run(coro):
    """These are async functions in a codebase with no async test plugin."""
    return asyncio.run(coro)

OSM_ROWS = [
    {"name": "1600 Pennsylvania Avenue NW",
     "display_name": "1600 Pennsylvania Avenue NW, Washington, DC 20500, United States",
     "lat": "38.8977", "lon": "-77.0365", "addresstype": "road",
     "address": {"country_code": "us"}},
    {"name": "Springfield",
     "display_name": "Springfield, Sangamon County, Illinois, United States",
     "lat": "39.7817", "lon": "-89.6501", "addresstype": "city",
     "address": {"country_code": "us"}},
]


class _Stub:
    """Stands in for Nominatim, counting what Delphi actually asks it."""

    def __init__(self, rows=None, fail=False):
        self.rows = OSM_ROWS if rows is None else rows
        self.fail = fail
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.fail:
            raise httpx.ConnectError("nominatim is down")
        return httpx.Response(200, json=self.rows)


@pytest.fixture
def stub(monkeypatch):
    s = _Stub()
    geocode.clear_cache()
    monkeypatch.setattr(geocode, "PROVIDER", "nominatim")
    monkeypatch.setattr(geocode, "MIN_GAP_S", 0.0)     # pacing has its own test

    real = httpx.AsyncClient

    def fake_client(*a, **kw):
        return real(*a, **{**kw, "transport": httpx.MockTransport(s.handler)})

    monkeypatch.setattr(geocode.httpx, "AsyncClient", fake_client)
    yield s
    geocode.clear_cache()


def test_an_address_comes_back_with_where_it_is(stub):
    hits = run(geocode.search("1600 Pennsylvania Ave"))
    assert hits[0]["name"] == "1600 Pennsylvania Avenue NW"
    assert "Washington" in hits[0]["address"]
    assert (round(hits[0]["lat"], 3), round(hits[0]["lon"], 3)) == (38.898, -77.037)
    assert hits[0]["country"] == "us"
    assert hits[0]["source"] == "osm"


def test_the_same_query_is_only_asked_once(stub):
    run(geocode.search("Springfield"))
    run(geocode.search("springfield"))     # same place, different shift key
    assert len(stub.calls) == 1


def test_a_fragment_is_never_sent(stub):
    assert run(geocode.search("ab")) == []
    assert run(geocode.search("")) == []
    assert stub.calls == []


def test_an_outage_costs_the_reader_nothing(monkeypatch):
    down = _Stub(fail=True)
    geocode.clear_cache()
    monkeypatch.setattr(geocode, "PROVIDER", "nominatim")
    monkeypatch.setattr(geocode, "MIN_GAP_S", 0.0)
    real = httpx.AsyncClient
    monkeypatch.setattr(geocode.httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, **{**kw,
                                                     "transport": httpx.MockTransport(down.handler)}))
    assert run(geocode.search("Springfield")) == []      # no exception


def test_turning_it_off_stops_every_call(stub, monkeypatch):
    monkeypatch.setattr(geocode, "PROVIDER", "off")
    assert not geocode.enabled()
    assert run(geocode.search("Springfield")) == []
    assert stub.calls == []


def test_it_identifies_itself_the_way_the_policy_asks(stub):
    run(geocode.search("Springfield"))
    agent = stub.calls[0].headers["user-agent"]
    assert agent.startswith("Delphi/")
    assert "+" in agent                     # a contact, so an operator is reachable


def test_lookups_are_paced(stub, monkeypatch):
    import time
    monkeypatch.setattr(geocode, "MIN_GAP_S", 0.25)
    started = time.monotonic()
    run(geocode.search("one place"))
    run(geocode.search("another place"))
    assert time.monotonic() - started >= 0.25
    assert len(stub.calls) == 2


# ---------- through the endpoint ----------

def test_a_city_the_gazetteer_knows_needs_no_lookup(client, register, stub):
    got = client.get("/api/geo/search?q=tokyo", headers=register("geo1")).json()
    assert got["results"][0]["name"] == "Tokyo"
    assert got["results"][0]["source"] == "local"
    assert stub.calls == []                 # nothing left the server
    assert got["attribution"] == ""


def test_something_it_does_not_know_is_looked_up(client, register, stub):
    got = client.get("/api/geo/search?q=1600 Pennsylvania Ave",
                     headers=register("geo2")).json()
    names = [h["name"] for h in got["results"]]
    assert "1600 Pennsylvania Avenue NW" in names
    assert len(stub.calls) == 1
    assert got["attribution"] == geocode.ATTRIBUTION


def test_local_matches_come_first(client, register, stub):
    """"york" matches York weakly and looks up too; local hits stay on top."""
    got = client.get("/api/geo/search?q=ork", headers=register("geo3")).json()
    sources = [h["source"] for h in got["results"]]
    assert sources == sorted(sources, key=lambda s: 0 if s == "local" else 1)


def test_a_place_in_both_lists_is_only_shown_once(client, register, monkeypatch):
    """The gazetteer's Kyoto and a lookup's Kyoto are the same pin."""
    geocode.clear_cache()
    monkeypatch.setattr(geocode, "PROVIDER", "nominatim")
    monkeypatch.setattr(geocode, "MIN_GAP_S", 0.0)
    from backend.app.geo import search_places
    known = search_places("ork")            # a weak match, so a lookup happens
    same = _Stub(rows=[{"name": known[0]["name"], "display_name": known[0]["name"],
                        "lat": str(known[0]["lat"]), "lon": str(known[0]["lon"]),
                        "addresstype": "city", "address": {"country_code": "xx"}}])
    real = httpx.AsyncClient
    monkeypatch.setattr(geocode.httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, **{**kw,
                                                     "transport": httpx.MockTransport(same.handler)}))
    got = client.get("/api/geo/search?q=ork", headers=register("geo4")).json()
    pins = [(round(h["lat"], 3), round(h["lon"], 3)) for h in got["results"]]
    assert len(pins) == len(set(pins))
    geocode.clear_cache()


def test_the_endpoint_still_needs_an_account(client, stub):
    assert client.get("/api/geo/search?q=tokyo").status_code == 401
