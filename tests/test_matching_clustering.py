"""Criteria matching (incl. deferred article bodies) and event clustering."""
from datetime import timedelta

from sqlalchemy import inspect as sa_inspect

from backend.app.clustering import LiveEvents, SIM_THRESHOLD, assign_events
from backend.app.ingest import RecentClusters
from backend.app.matching import query_articles
from backend.app.models import Article, Event, Source, utcnow
from backend.app.scoring import cluster_tokens, tokens_similarity


def _seed(db):
    src = Source(name="Wire", rss_url="http://w/x", scope="international", tier=1, added_by="user")
    db.add(src); db.flush()
    now = utcnow()
    a1 = Article(source_id=src.id, url="http://w/1", title="Major earthquake strikes Tokyo",
                 summary="tsunami warning", content="A powerful quake hit BODYWORD.",
                 language="en", importance=70, published_at=now,
                 cluster_tokens=cluster_tokens("Major earthquake strikes Tokyo"))
    a2 = Article(source_id=src.id, url="http://w/2", title="Local council approves budget",
                 summary="routine", content="", language="en", importance=30,
                 published_at=now, cluster_tokens=cluster_tokens("Local council approves budget"))
    db.add_all([a1, a2]); db.commit()
    return a1, a2


def test_content_deferred_when_no_text_criteria(db):
    _seed(db)
    db.expire_all()
    res = query_articles(db, {"min_importance": 10}, limit=10)
    assert res and all("content" not in sa_inspect(a).dict for a in res)


def test_text_criteria_loads_and_matches_body(db):
    _seed(db)
    db.expire_all()
    hits = query_articles(db, {"keywords": ["BODYWORD"]}, limit=10)
    assert len(hits) == 1 and hits[0].url == "http://w/1"
    assert "content" in sa_inspect(hits[0]).dict


def test_importance_and_category_filters(db):
    _seed(db)
    assert len(query_articles(db, {"min_importance": 60}, limit=10)) == 1


def test_clustering_groups_similar_and_separates_distinct(db):
    src = Source(name="W", rss_url="http://w/y", scope="international", tier=1, added_by="user")
    db.add(src); db.flush()
    now = utcnow()
    titles = [
        "Massive earthquake strikes near Tokyo tsunami warning",
        "Earthquake near Tokyo triggers tsunami warning authorities say",  # ~same event
        "Stock markets rally on strong tech earnings report",              # distinct
    ]
    arts = []
    for i, t in enumerate(titles):
        a = Article(source_id=src.id, url=f"http://w/e{i}", title=t, summary="",
                    language="en", importance=60, published_at=now - timedelta(minutes=i),
                    cluster_tokens=cluster_tokens(t))
        db.add(a); arts.append(a)
    db.flush()
    created = assign_events(db, arts)
    db.commit()
    assert arts[0].event_id == arts[1].event_id            # clustered together
    assert arts[2].event_id != arts[0].event_id            # separate
    assert created == 2


def _headline(title):
    a = Article(title=title, summary="")
    a.cluster_tokens = cluster_tokens(title)
    return a


def _event(title):
    e = Event(title=title, updated_at=utcnow())
    e.cluster_tokens = cluster_tokens(title)
    return e


TITLES = [
    "Massive earthquake strikes near Tokyo tsunami warning issued",
    "Stock markets rally on strong technology earnings reports",
    "Ceasefire talks resume as diplomats meet in Geneva",
    "Wildfire forces evacuations across northern California counties",
    "Central bank raises rates as inflation stays stubbornly high",
    "Earthquake aftershocks continue near Tokyo, rail services halted",
]


def test_indexed_event_search_picks_what_a_full_scan_would(db):
    """The index exists only to skip events that cannot match. For every
    headline it has to choose the same event an exhaustive comparison does."""
    events = [_event(t) for t in TITLES]
    live = LiveEvents(events)
    for title in TITLES + ["Tokyo earthquake death toll rises as rescuers dig",
                           "A quiet day for local government paperwork"]:
        article = _headline(title)
        words = set(article.cluster_tokens.split())
        want, want_sim = None, 0.0
        if len(words) >= 2:                     # the exhaustive comparison
            for event in events:
                other = set(event.cluster_tokens.split())
                if len(words & other) < 2:
                    continue
                sim = tokens_similarity(article.cluster_tokens, event.cluster_tokens)
                if sim > want_sim:
                    want, want_sim = event, sim
        if want_sim < SIM_THRESHOLD:
            want = None
        at = live.best_match(article)
        assert (live.event(at) if at is not None else None) is want, title


def test_a_merged_event_is_findable_by_its_new_tokens(db):
    """Merging rewrites an event's tokens, and the index has to pick that up —
    the next article on the story matches on words the event only just gained."""
    live = LiveEvents([_event("Wildfire forces evacuations in northern California")])
    event = live.event(0)
    event.cluster_tokens = cluster_tokens(
        "Wildfire smoke blankets Sacramento as evacuations widen in California")
    live.merged(0)
    follow_up = _headline("Sacramento smoke worsens as California wildfire spreads")
    assert live.best_match(follow_up) == 0


def test_corroboration_counts_distinct_other_outlets(db):
    """Corroboration is how many *other* sources ran the same story: an outlet
    repeating itself counts once, and only for someone else."""
    tokens = cluster_tokens("Massive earthquake strikes near Tokyo tsunami warning")
    recent = RecentClusters([
        (1, tokens),                                          # the same outlet
        (2, tokens),
        (2, tokens),                                          # again, still one outlet
        (3, cluster_tokens("Earthquake near Tokyo triggers tsunami warning")),
        (4, cluster_tokens("Stock markets rally on strong tech earnings")),
    ])
    assert recent.corroboration(1, tokens) == 2               # outlets 2 and 3
    assert recent.corroboration(5, cluster_tokens("A council approves its budget")) == 0
    # Articles arriving in the same batch corroborate each other.
    recent.add(6, tokens)
    assert recent.corroboration(1, tokens) == 3


# ---------- clustering in slices ----------
#
# A busy minute brings 800+ articles and clustering all of them in one go held
# the interpreter long enough to take the site off the internet, so the batch is
# now clustered in slices with the loop given back between them. That is only
# an acceptable trade if the slices reach the same answer as one pass — an
# article must still join an event that an earlier article in the same batch
# created, even when a slice boundary falls between them.

def _batch(db, titles, src):
    now = utcnow()
    arts = []
    for i, t in enumerate(titles):
        a = Article(source_id=src.id, url=f"http://w/s{i}", title=t, summary="",
                    language="en", importance=60,
                    published_at=now - timedelta(minutes=len(titles) - i),
                    cluster_tokens=cluster_tokens(t))
        db.add(a); arts.append(a)
    db.flush()
    return arts


def _grouping(articles):
    """Which articles share an event, independent of the ids themselves."""
    groups = {}
    for a in articles:
        groups.setdefault(a.event_id, set()).add(a.url)
    return sorted(sorted(g) for g in groups.values())


TITLES = [
    "Massive earthquake strikes near Tokyo tsunami warning",
    "Stock markets rally on strong tech earnings report",
    "Earthquake near Tokyo triggers tsunami warning authorities say",
    "Tech earnings beat expectations stock markets rally",
    "Flooding closes roads across three northern counties",
    "Earthquake Tokyo tsunami warning issued for coastal areas",
]


def test_slicing_a_batch_clusters_it_the_same_way(db):
    """The whole justification for slicing. Same articles, same order, one
    LiveEvents carried across — the grouping must not move."""
    from backend.app.clustering import live_events

    src = Source(name="W", rss_url="http://w/s", scope="international", tier=1,
                 added_by="user")
    db.add(src); db.flush()

    whole = _batch(db, TITLES, src)
    assign_events(db, whole)
    db.commit()
    expected = _grouping(whole)

    # Same batch again, clustered two at a time against a fresh event table.
    db.query(Article).delete(); db.query(Event).delete(); db.commit()
    sliced = _batch(db, TITLES, src)
    sliced.sort(key=lambda a: a.published_at)
    live = live_events(db)
    created = 0
    for i in range(0, len(sliced), 2):
        created += assign_events(db, sliced[i:i + 2], live=live)
        db.commit()

    assert _grouping(sliced) == expected
    assert created == len(expected)


def test_a_slice_can_join_an_event_an_earlier_slice_created(db):
    """The failure mode if LiveEvents were rebuilt per slice, or not carried:
    every slice would start blind and duplicate the events before it."""
    from backend.app.clustering import live_events

    src = Source(name="W", rss_url="http://w/j", scope="international", tier=1,
                 added_by="user")
    db.add(src); db.flush()
    pair = _batch(db, [TITLES[0], TITLES[2]], src)   # the same earthquake
    pair.sort(key=lambda a: a.published_at)

    live = live_events(db)
    first = assign_events(db, pair[:1], live=live)
    db.commit()
    second = assign_events(db, pair[1:], live=live)
    db.commit()

    assert first == 1, "the first article should have opened an event"
    assert second == 0, "the second opened its own event instead of joining"
    assert pair[0].event_id == pair[1].event_id
