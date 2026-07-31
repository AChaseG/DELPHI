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
