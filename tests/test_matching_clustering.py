"""Criteria matching (incl. deferred article bodies) and event clustering."""
from datetime import timedelta

from sqlalchemy import inspect as sa_inspect

from backend.app.clustering import assign_events
from backend.app.matching import query_articles
from backend.app.models import Article, Source, utcnow
from backend.app.scoring import cluster_tokens


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
