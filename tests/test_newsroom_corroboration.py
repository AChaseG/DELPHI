"""Seven mastheads running one newsroom's copy are one opinion, not seven.

Corroboration — how many other outlets carry this story — drives importance, and
it counted feeds. Measured over twelve hours of 105,118 live articles, the
sources that most often publish a headline someone else published first are not
random reposters; they arrive in families. Four Glacier Media titles in British
Columbia, seven Vocento and Prensa Ibérica papers in Spain, four Mediahuis and
DPG mastheads in the Netherlands. Each has its own domain and its own article
URL, so the global URL dedup at ingest never sees a duplicate, and each voted
separately — which turned one newsroom into an apparent consensus and pushed the
story's importance up with it.

Two things this must get right, and they pull against each other. A group's
shared copy has to stop double-counting. And a masthead's *own* reporting has to
keep working normally, because the group papers break real local stories —
Haarlems Dagblad was 42% reposted and first on 58% of what it ran. A fix that
silenced it would be worse than the problem.
"""
from datetime import timedelta

import pytest

from backend.app import ingest, syndication
from backend.app.ingest import RecentClusters
from backend.app.models import Article, Source, utcnow

STORY = "flood warning issued for the river valley towns"
OTHER = "council approves new library building plan downtown"


# ---------- counting newsrooms, not mastheads ----------

def test_one_newsroom_under_many_mastheads_counts_once():
    rows = [(i, STORY) for i in range(1, 8)]
    grouped = RecentClusters(rows, newsroom={i: 1 for i in range(1, 8)})

    assert RecentClusters(rows).corroboration(99, STORY) == 7, "the old behaviour"
    assert grouped.corroboration(99, STORY) == 1


def test_independent_outlets_still_count_separately():
    """The signal has to survive: genuine pickup is what importance is for."""
    rows = [(1, STORY), (2, STORY), (3, STORY)]
    assert RecentClusters(rows, newsroom={}).corroboration(99, STORY) == 3


def test_a_masthead_does_not_corroborate_its_own_group():
    """The case that mattered: a Vocento paper reading a Vocento story."""
    rows = [(1, STORY), (2, STORY), (3, STORY)]
    nr = {1: 1, 2: 1, 3: 1}
    assert RecentClusters(rows, newsroom=nr).corroboration(2, STORY) == 0


def test_a_group_plus_an_outsider_counts_two():
    rows = [(1, STORY), (2, STORY), (3, STORY), (50, STORY)]
    nr = {1: 1, 2: 1, 3: 1}
    assert RecentClusters(rows, newsroom=nr).corroboration(99, STORY) == 2


def test_a_groups_own_local_story_still_corroborates_normally():
    """The thing that must not break. A group masthead breaking its own story,
    picked up by three unrelated outlets, is still a well-corroborated story."""
    rows = [(1, OTHER), (60, OTHER), (61, OTHER), (62, OTHER)]
    nr = {1: 1, 2: 1, 3: 1}          # 1 is in a group; 60-62 are not
    assert RecentClusters(rows, newsroom=nr).corroboration(1, OTHER) == 3


def test_an_ungrouped_source_is_its_own_publisher():
    index = RecentClusters([], newsroom={5: 1})
    assert index.publisher(5) == 1
    assert index.publisher(77) == 77


def test_no_mapping_behaves_exactly_as_before():
    rows = [(1, STORY), (2, STORY)]
    assert RecentClusters(rows).corroboration(9, STORY) == 2
    assert RecentClusters(rows, newsroom=None).corroboration(9, STORY) == 2


def test_headlines_added_during_a_batch_are_grouped_too():
    """process_entries adds as it goes, so the mapping has to apply there and
    not only to what was loaded from the database."""
    index = RecentClusters([], newsroom={1: 1, 2: 1})
    index.add(1, STORY)
    index.add(2, STORY)
    assert index.corroboration(99, STORY) == 1


# ---------- learning who shares a newsroom ----------

@pytest.fixture
def sources(db):
    made = []
    for i in range(6):
        s = Source(name=f"masthead {i}", rss_url=f"http://m{i}.example/feed")
        db.add(s)
        made.append(s)
    db.commit()
    return made


def _publish(db, source, title, *, hours_old=1, tag=""):
    db.add(Article(
        source_id=source.id, title=title,
        url=f"http://m{source.id}.example/{tag}{abs(hash(title)) % 10**8}",
        summary="s", cluster_tokens="", published_at=utcnow() - timedelta(hours=hours_old),
        fetched_at=utcnow(), importance=10))


def test_shared_copy_is_learned(db, sources, monkeypatch):
    monkeypatch.setattr(syndication, "MIN_SHARED", 3)
    a, b = sources[0], sources[1]
    for i in range(3):
        title = f"regional council story number {i} in some detail"
        _publish(db, a, title)
        _publish(db, b, title)
    db.commit()

    result = syndication.detect(db)

    assert result["groups"] == 1
    db.refresh(a); db.refresh(b)
    assert a.syndicate == b.syndicate != ""


def test_one_coincidence_is_not_a_newsroom(db, sources, monkeypatch):
    """Two outlets can land on the same words once. That is not evidence."""
    monkeypatch.setattr(syndication, "MIN_SHARED", 4)
    a, b = sources[0], sources[1]
    _publish(db, a, "weather warning issued for the coming weekend")
    _publish(db, b, "weather warning issued for the coming weekend")
    db.commit()

    assert syndication.detect(db)["groups"] == 0
    db.refresh(a)
    assert a.syndicate == ""


def test_short_headlines_are_not_evidence(db, sources, monkeypatch):
    """They collide by accident far more often than long ones."""
    monkeypatch.setattr(syndication, "MIN_SHARED", 2)
    a, b = sources[0], sources[1]
    for i in range(5):
        _publish(db, a, "budget row", tag=f"a{i}")
        _publish(db, b, "budget row", tag=f"b{i}")
    db.commit()

    assert syndication.detect(db)["groups"] == 0


def test_a_family_is_transitive(db, sources, monkeypatch):
    """A shares with B, B with C: one wire, one newsroom."""
    monkeypatch.setattr(syndication, "MIN_SHARED", 2)
    a, b, c = sources[0], sources[1], sources[2]
    for i in range(2):
        t = f"first shared story about the harbour works {i}"
        _publish(db, a, t); _publish(db, b, t)
        t2 = f"second shared story about the ring road {i}"
        _publish(db, b, t2); _publish(db, c, t2)
    db.commit()

    syndication.detect(db)
    db.refresh(a); db.refresh(b); db.refresh(c)
    assert a.syndicate == b.syndicate == c.syndicate != ""


def test_a_group_that_stops_syndicating_stops_being_one(db, sources, monkeypatch):
    """Otherwise the mapping is a claim about last month that nobody revisits."""
    monkeypatch.setattr(syndication, "MIN_SHARED", 2)
    a, b = sources[0], sources[1]
    for i in range(2):
        t = f"shared story from the group wire number {i} today"
        _publish(db, a, t); _publish(db, b, t)
    db.commit()
    syndication.detect(db)
    db.refresh(a)
    assert a.syndicate != ""

    db.query(Article).delete(synchronize_session=False)
    db.commit()
    syndication.detect(db)

    db.refresh(a); db.refresh(b)
    assert a.syndicate == ""
    assert b.syndicate == ""


def test_the_group_key_is_stable_across_runs(db, sources, monkeypatch):
    monkeypatch.setattr(syndication, "MIN_SHARED", 2)
    a, b = sources[0], sources[1]
    for i in range(2):
        t = f"a sufficiently long shared headline number {i} here"
        _publish(db, a, t); _publish(db, b, t)
    db.commit()

    syndication.detect(db)
    db.refresh(a)
    first = a.syndicate
    syndication.detect(db)
    db.refresh(a)
    assert a.syndicate == first
    assert first == str(min(a.id, b.id)), "the lowest member id, so it is stable"


def test_a_story_everybody_runs_does_not_group_everybody(db, monkeypatch):
    """Wire copy carried by dozens of outlets says nothing about who shares a
    newsroom, and pairing them all would be quadratic on the widest stories."""
    monkeypatch.setattr(syndication, "MIN_SHARED", 1)
    many = []
    for i in range(20):
        s = Source(name=f"outlet {i}", rss_url=f"http://o{i}.example/feed")
        db.add(s)
        many.append(s)
    db.commit()
    for s in many:
        _publish(db, s, "president signs the long awaited trade agreement today")
    db.commit()

    assert syndication.detect(db)["groups"] == 0


def test_nothing_is_ever_disabled(db, sources, monkeypatch):
    """It changes how a source counts, never whether it is read."""
    monkeypatch.setattr(syndication, "MIN_SHARED", 2)
    a, b = sources[0], sources[1]
    for i in range(2):
        t = f"the same copy under two mastheads number {i} again"
        _publish(db, a, t); _publish(db, b, t)
    db.commit()

    syndication.detect(db)

    assert all(s.enabled for s in db.query(Source).all())


def test_normalize_ignores_case_and_punctuation():
    assert (syndication.normalize("Flood WARNING — issued!")
            == syndication.normalize("flood warning issued"))


# ---------- it is wired in, and visible ----------

def test_the_index_uses_the_learned_mapping():
    import inspect
    assert "syndication.group_map(db)" in inspect.getsource(ingest._recent_clusters)


def test_the_audit_relearns_it_and_rebuilds_the_index():
    """A stale index holds the old grouping, so relearning without rebuilding
    would record the change and not apply it."""
    import inspect
    import re
    body = inspect.getsource(ingest.ingest_loop)
    assert "reset_cluster_index()" in body
    # Off the loop: it reads a window of headlines and pairs them up, which is
    # the same shape of work as the index build that caused the stalls.
    assert re.search(r"asyncio\.to_thread\(\s*syndication\.detect", body), (
        "detect must run in a thread, not on the event loop")


def test_group_map_only_lists_grouped_sources(db, sources, monkeypatch):
    monkeypatch.setattr(syndication, "MIN_SHARED", 2)
    a, b = sources[0], sources[1]
    for i in range(2):
        t = f"grouped copy under both mastheads number {i} indeed"
        _publish(db, a, t); _publish(db, b, t)
    db.commit()
    syndication.detect(db)

    mapping = syndication.group_map(db)
    assert set(mapping) == {a.id, b.id}
    assert set(mapping.values()) == {min(a.id, b.id)}


def test_an_operator_can_read_the_families(client):
    from backend.app import main
    reg = client.post("/api/auth/register", json={
        "username": "boss", "email": "boss@example.com",
        "password": "correct-horse-staple"})
    headers = {"Authorization": "Bearer " + reg.json()["token"]}

    denied = client.get("/api/admin/syndication", headers=headers)
    assert denied.status_code == 403, "ordinary accounts cannot read it"

    main._ADMIN_HANDLES = frozenset({"boss"})
    try:
        res = client.get("/api/admin/syndication", headers=headers)
        assert res.status_code == 200, res.text
        body = res.json()
        assert "families" in body and "groups" in body
        assert body["min_shared_headlines"] == syndication.MIN_SHARED
    finally:
        main._ADMIN_HANDLES = frozenset()


def test_families_names_the_members(db, sources, monkeypatch):
    monkeypatch.setattr(syndication, "MIN_SHARED", 2)
    a, b = sources[0], sources[1]
    for i in range(2):
        t = f"shared masthead copy example number {i} for the test"
        _publish(db, a, t); _publish(db, b, t)
    db.commit()
    syndication.detect(db)

    (fam,) = syndication.families(db)
    assert sorted(fam["members"]) == sorted([a.name, b.name])
