"""Auto-discovery must not adopt things that merely publish a feed.

85% of the live catalog — 3,826 of about 4,500 sources — was adopted
automatically, and the bar was only "this domain has a working feed". That let
in a record label's ticketing calendar (baodaorecords.kktix.cc, filed as
"national news"), whose concert listings then turned up in an energy feed
because one of the performer's songs is called "Innocent Wind". A funeral home
got in the same way.

Two gates, because neither is sufficient alone. The blocklist names platforms
whose feeds look like news and are not, and now matches subdomains — as exact
strings it never saw baodaorecords.kktix.cc at all. And a domain must be seen
publishing more than once before it is probed, which is what separates an
outlet from a one-off without needing a list of every kind of website that is
not a newspaper.
"""
import pytest

from backend.app import discovery
from backend.app.models import DiscoveredDomain, Source, utcnow


# ---------- the blocklist reaches subdomains ----------

@pytest.mark.parametrize("domain", [
    "kktix.cc", "baodaorecords.kktix.cc", "welcome-music.kktix.cc",
    "news.google.com", "eventbrite.com", "www.eventbrite.com",
    "shop.ticketmaster.com", "peatix.com",
])
def test_platforms_and_everything_under_them_are_refused(domain):
    assert discovery.skipped(domain)


@pytest.mark.parametrize("domain", [
    "bbc.co.uk", "kyivpost.com", "meduza.io", "thehill.com",
    # Not a suffix match: the blocked name has to be a whole label.
    "notkktix.cc", "mykktix.cc.example.org",
])
def test_real_outlets_are_not(domain):
    assert not discovery.skipped(domain)


def test_a_blocked_publisher_is_never_even_collected():
    entries = [{"source": {"href": "https://baodaorecords.kktix.cc/", "title": "KKTIX"}},
               {"source": {"href": "https://www.kyivpost.com/", "title": "Kyiv Post"}}]
    got = discovery.collect_publishers(entries)
    assert "kyivpost.com" in got
    assert not any("kktix" in d for d in got)


# ---------- one sighting is not enough ----------

def test_a_domain_seen_once_is_recorded_and_left_alone(db):
    assert discovery.note_sighting(db, "hofffuneral.com") == 1
    db.commit()
    rec = db.query(DiscoveredDomain).filter_by(domain="hofffuneral.com").one()
    assert rec.sightings == 1 and rec.status == "seen"


def test_sightings_accumulate_across_cycles(db):
    for expected in (1, 2, 3):
        assert discovery.note_sighting(db, "kyivpost.com") == expected
    db.commit()


def test_a_domain_still_counting_stays_eligible(db):
    """A "seen" record must not read as a settled outcome, or the domain could
    never reach a second sighting and nothing would ever be adopted again."""
    discovery.note_sighting(db, "kyivpost.com")
    db.commit()
    assert "kyivpost.com" not in discovery._known_domains(db)


def test_a_failed_probe_still_backs_off(db):
    """The recurrence gate must not undo the existing one: a domain probed and
    found to have no feed is not re-probed until RECHECK_DAYS have passed."""
    discovery._record(db, "nofeed.example", "no-feed")
    db.commit()
    assert "nofeed.example" in discovery._known_domains(db)


def test_an_adopted_domain_is_never_probed_again(db):
    discovery._record(db, "adopted.example", "added")
    db.commit()
    assert "adopted.example" in discovery._known_domains(db)


def test_the_threshold_is_more_than_one(db):
    """The whole point. At 1 this is the old behaviour with extra bookkeeping."""
    assert discovery.MIN_SIGHTINGS >= 2


# ---------- end to end through the real entry point ----------

@pytest.mark.anyio
async def test_first_sighting_adopts_nothing(db, monkeypatch):
    probed = []

    async def never(client, homepage):
        probed.append(homepage)
        return None

    monkeypatch.setattr(discovery, "_find_site_feed", never)
    added = await discovery.discover_new_sources(
        db, {"hofffuneral.com": ("Hoff Funeral Home", "https://hofffuneral.com/")})
    db.commit()

    assert added == []
    assert probed == [], "a domain on its first sighting was probed anyway"
    assert db.query(DiscoveredDomain).filter_by(domain="hofffuneral.com").one().sightings == 1


@pytest.mark.anyio
async def test_a_second_sighting_lets_it_through(db, monkeypatch):
    async def found(client, homepage):
        return ("https://kyivpost.com/feed", [])

    monkeypatch.setattr(discovery, "_find_site_feed", found)
    pubs = {"kyivpost.com": ("Kyiv Post", "https://kyivpost.com/")}

    assert await discovery.discover_new_sources(db, pubs) == []
    db.commit()
    added = await discovery.discover_new_sources(db, pubs)
    db.commit()

    assert len(added) == 1
    source = db.query(Source).filter_by(rss_url="https://kyivpost.com/feed").one()
    assert source.added_by == "auto-discovered"


@pytest.mark.anyio
async def test_a_blocked_platform_is_refused_however_often_it_appears(db, monkeypatch):
    async def found(client, homepage):
        return ("https://baodaorecords.kktix.cc/events.atom", [])

    monkeypatch.setattr(discovery, "_find_site_feed", found)
    pubs = {"baodaorecords.kktix.cc": ("KKTIX", "https://baodaorecords.kktix.cc/")}
    for _ in range(5):
        assert await discovery.discover_new_sources(db, pubs) == []
        db.commit()
    assert db.query(Source).count() == 0


@pytest.fixture
def anyio_backend():
    return "asyncio"
