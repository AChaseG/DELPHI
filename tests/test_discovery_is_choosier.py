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


# ---------- the rules applied backwards, to what is already here ----------
#
# Tightening what may be adopted does nothing about what already was, and the
# catalog is 85% auto-adopted — the ticketing calendar that started this was
# still in it, still enabled, still filed as national news.

def _src(db, name, rss, added_by="auto-discovered", homepage="", enabled=True):
    s = Source(name=name, rss_url=rss, homepage=homepage, added_by=added_by,
               enabled=enabled, scope="national", tier=3)
    db.add(s)
    db.commit()
    return s


def test_it_disables_a_platform_already_in_the_catalog(db):
    kk = _src(db, "KKTIX", "https://baodaorecords.kktix.cc/events.atom")
    result = discovery.audit_catalog(db)
    db.refresh(kk)
    assert result["disabled"] == 1
    assert kk.enabled is False
    assert "not a news source" in kk.last_status


def test_it_disables_rather_than_deletes(db):
    """Deleting would take the articles with it, and those are shared by every
    feed on the instance. An operator who disagrees can switch it back on."""
    _src(db, "KKTIX", "https://baodaorecords.kktix.cc/events.atom")
    discovery.audit_catalog(db)
    assert db.query(Source).count() == 1


def test_the_reason_is_visible_where_an_operator_looks(db):
    kk = _src(db, "KKTIX", "https://welcome-music.kktix.cc/events.atom")
    discovery.audit_catalog(db)
    db.refresh(kk)
    # Not a bare "disabled" — the Sources panel shows last_status, and an
    # operator finding a dead source needs to know who switched it off.
    assert kk.last_status.startswith("disabled:")


def test_it_matches_on_the_homepage_too(db):
    """A source can carry the platform on either URL."""
    s = _src(db, "Tickets", "https://feeds.example.com/x.xml",
             homepage="https://someone.eventbrite.com/")
    discovery.audit_catalog(db)
    db.refresh(s)
    assert s.enabled is False


def test_it_never_touches_a_source_a_person_added(db):
    """They chose it, and this is a heuristic."""
    mine = _src(db, "My ticket feed", "https://baodaorecords.kktix.cc/events.atom",
                added_by="user")
    result = discovery.audit_catalog(db)
    db.refresh(mine)
    assert result["disabled"] == 0
    assert mine.enabled is True


def test_it_does_not_disable_the_city_coverage(db):
    """The footgun. news.google.com is on the never-adopt list, and the 497
    city sources and every topic tracker are Google News searches — sweeping
    the whole list would switch off the city coverage entirely."""
    city = _src(db, "Taipei", "https://news.google.com/rss/search?q=Taipei",
                added_by="city-catalog")
    topic = _src(db, "Topic: energy", "https://news.google.com/rss/search?q=energy",
                 added_by="topic-tracker")
    repaired = _src(db, "Repaired outlet",
                    "https://news.google.com/rss/search?q=site:outlet.com")

    assert discovery.audit_catalog(db)["disabled"] == 0
    for s in (city, topic, repaired):
        db.refresh(s)
        assert s.enabled is True


def test_an_ordinary_outlet_is_left_alone(db):
    good = _src(db, "Kyiv Post", "https://kyivpost.com/feed", added_by="catalog")
    assert discovery.audit_catalog(db)["disabled"] == 0
    db.refresh(good)
    assert good.enabled is True


def test_running_it_twice_changes_nothing_the_second_time(db):
    _src(db, "KKTIX", "https://baodaorecords.kktix.cc/events.atom")
    assert discovery.audit_catalog(db)["disabled"] == 1
    assert discovery.audit_catalog(db)["disabled"] == 0, (
        "an already-disabled source was counted again")


def test_the_two_lists_are_kept_apart(db):
    """Only the not-news half may be applied backwards."""
    assert "news.google.com" in discovery._SKIP_AGGREGATORS
    assert "news.google.com" not in discovery._SKIP_NOT_NEWS
    assert "kktix.cc" in discovery._SKIP_NOT_NEWS
    # Adoption still refuses both.
    assert discovery.skipped("news.google.com") and discovery.skipped("kktix.cc")
    assert not discovery.not_news("news.google.com")


# ---------- and it runs on its own ----------

def test_the_poller_audits_daily_and_on_restart():
    import inspect

    from backend.app import ingest

    loop = inspect.getsource(ingest.ingest_loop)
    assert "discovery.audit_catalog" in loop, "nothing runs the sweep"
    assert "_next_audit_at" in loop
    assert ingest.AUDIT_EVERY_SECONDS == 24 * 3600
    # Zero at import, so the first tick after a restart runs it — a deploy is
    # exactly when the rules have just changed.
    assert ingest._next_audit_at == 0.0


def test_the_sweep_is_off_the_event_loop():
    import inspect

    from backend.app import ingest
    assert "to_thread(discovery.audit_catalog" in inspect.getsource(ingest.ingest_loop)
