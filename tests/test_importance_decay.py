"""Importance falls as a story goes quiet.

Three things are worth testing separately and they fail in different ways: the
curve itself (pure arithmetic), the rule that decides *which* moment a story is
aged from (the part the request actually turned on), and the sweep that walks
the archive down the curve without rewriting all of it.
"""
from datetime import datetime, timedelta

import pytest

from backend.app import ingest, scoring
from backend.app.models import Article, Event, Source

NOW = datetime(2026, 8, 23, 12, 0, 0)


# --- the curve ------------------------------------------------------------

def test_a_fresh_article_is_worth_what_it_scored():
    assert scoring.decay_importance(90, 0) == 90


def test_nothing_moves_inside_the_grace_window():
    # Otherwise Home's six-hour "Breaking" column would reshuffle every tick.
    assert scoring.decay_importance(90, scoring.DECAY_GRACE_HOURS - 0.01) == 90


def test_one_half_life_gives_up_half_the_distance_to_the_floor():
    base = 90
    floor = scoring.decay_floor(base)
    aged = scoring.decay_importance(
        base, scoring.DECAY_GRACE_HOURS + scoring.DECAY_HALF_LIFE_HOURS)
    assert aged == round(floor + (base - floor) / 2)


def test_the_score_falls_monotonically_with_age():
    hours = [0, 3, 6, 12, 24, 48, 96, 200, 400, 1000]
    scores = [scoring.decay_importance(90, h) for h in hours]
    assert scores == sorted(scores, reverse=True)


def test_it_stops_at_the_floor_and_never_goes_under():
    floor = scoring.decay_floor(90)
    for hours in (500, 5_000, 50_000):
        assert scoring.decay_importance(90, hours) == floor
    assert floor == 36


def test_the_floor_keeps_the_archive_searchable():
    # A curve running to zero would make min_importance mean "is this a big
    # story this afternoon" and return nothing at all across last year.
    assert scoring.decay_importance(90, 100_000) > scoring.decay_importance(30, 0)


def test_nothing_falls_below_the_scoring_minimum():
    assert scoring.decay_importance(5, 100_000) == 5
    assert scoring.decay_importance(10, 100_000) >= 5


def test_a_future_dated_article_is_not_pushed_above_its_score():
    # Several feeds date items in the future; a negative age must not invert
    # the curve into a bonus.
    assert scoring.decay_importance(70, -240) == 70


def test_decay_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(scoring, "DECAY_HALF_LIFE_HOURS", 0.0)
    assert scoring.decay_importance(90, 100_000) == 90


def test_the_score_is_clamped_to_the_scale():
    assert scoring.decay_importance(300, 0) == 100
    assert scoring.decay_importance(-40, 0) == 5


# --- which moment it is aged from -----------------------------------------

def test_an_unclustered_article_ages_from_its_own_publication():
    pub = NOW - timedelta(hours=100)
    assert scoring.decay_reference(pub) == pub


def test_a_story_still_being_covered_does_not_age():
    pub = NOW - timedelta(hours=100)
    assert scoring.decay_reference(pub, pub, NOW) == NOW


def test_a_story_nobody_has_touched_ages_from_the_break():
    pub = NOW - timedelta(hours=100)
    broke = NOW - timedelta(hours=120)
    assert scoring.decay_reference(pub, broke, broke) == pub


def test_the_lift_from_a_live_cluster_expires(monkeypatch):
    # A war updates for months; its opening-day coverage is still old news.
    monkeypatch.setattr(scoring, "DECAY_LIFT_MAX_HOURS", 336.0)
    pub = NOW - timedelta(hours=5_000)
    ref = scoring.decay_reference(pub, pub, NOW)
    assert ref == pub + timedelta(hours=336)
    assert ref < NOW


def test_a_live_cluster_holds_recent_coverage_at_full_value():
    pub = NOW - timedelta(hours=72)
    quiet = scoring.aged_importance(90, pub, pub, pub, now=NOW)
    live = scoring.aged_importance(90, pub, pub, NOW, now=NOW)
    assert live == 90
    assert quiet < live


def test_an_article_with_no_timestamps_keeps_its_score():
    assert scoring.aged_importance(70, None) == 70


# --- the sweep ------------------------------------------------------------

def _source(db):
    src = Source(name="Wire", rss_url="https://wire.test/rss", scope="international",
                 tier=1, country="us")
    db.add(src)
    db.flush()
    return src


def _article(db, src, *, importance, hours_ago, base=None, event=None):
    art = Article(
        source_id=src.id, url=f"https://wire.test/{db.query(Article).count()}-{hours_ago}",
        title="A thing happened", summary="", importance=importance,
        base_importance=base, published_at=NOW - timedelta(hours=hours_ago),
        fetched_at=NOW - timedelta(hours=hours_ago),
        event_id=event.id if event else None)
    db.add(art)
    db.flush()
    return art


@pytest.fixture(autouse=True)
def _reset_cursor():
    ingest._decay_cursor = 0
    yield
    ingest._decay_cursor = 0


def test_the_sweep_ages_a_stale_article(db):
    src = _source(db)
    art = _article(db, src, importance=90, base=90, hours_ago=200)
    moved = ingest.decay_scores(db, now=NOW)
    db.refresh(art)
    assert moved["changed"] == 1
    assert art.importance < 90
    assert art.base_importance == 90


def test_the_sweep_leaves_a_fresh_article_alone(db):
    src = _source(db)
    art = _article(db, src, importance=90, base=90, hours_ago=1)
    moved = ingest.decay_scores(db, now=NOW)
    db.refresh(art)
    assert art.importance == 90
    assert moved["changed"] == 0


def test_running_the_sweep_twice_does_not_decay_twice(db):
    src = _source(db)
    art = _article(db, src, importance=90, base=90, hours_ago=200)
    ingest.decay_scores(db, now=NOW)
    db.refresh(art)
    once = art.importance
    ingest._decay_cursor = 0
    second = ingest.decay_scores(db, now=NOW)
    db.refresh(art)
    assert art.importance == once
    assert second["changed"] == 0


def test_a_row_from_before_the_column_existed_keeps_its_score_as_its_origin(db):
    src = _source(db)
    art = _article(db, src, importance=82, base=None, hours_ago=1)
    moved = ingest.decay_scores(db, now=NOW)
    db.refresh(art)
    assert moved["backfilled"] == 1
    # Recorded before the score moved, never after.
    assert art.base_importance == 82


def test_the_origin_survives_the_pass_that_decays_the_row(db):
    src = _source(db)
    art = _article(db, src, importance=90, base=None, hours_ago=300)
    ingest.decay_scores(db, now=NOW)
    db.refresh(art)
    assert art.base_importance == 90
    assert art.importance < 90


def test_a_live_cluster_keeps_its_members_from_ageing(db):
    src = _source(db)
    quiet = Event(title="Quiet", first_seen=NOW - timedelta(hours=200),
                  updated_at=NOW - timedelta(hours=200))
    live = Event(title="Live", first_seen=NOW - timedelta(hours=200),
                 updated_at=NOW - timedelta(hours=1))
    db.add_all([quiet, live])
    db.flush()
    a = _article(db, src, importance=90, base=90, hours_ago=200, event=quiet)
    b = _article(db, src, importance=90, base=90, hours_ago=200, event=live)
    ingest.decay_scores(db, now=NOW)
    db.refresh(a)
    db.refresh(b)
    assert b.importance == 90
    assert a.importance < b.importance


def test_the_sweep_is_bounded_and_resumes_where_it_stopped(db, monkeypatch):
    monkeypatch.setattr(ingest, "DECAY_BATCH", 3)
    src = _source(db)
    arts = [_article(db, src, importance=90, base=90, hours_ago=200 + i)
            for i in range(7)]
    first = ingest.decay_scores(db, now=NOW)
    assert first["scanned"] == 3 and first["more"] is True
    assert ingest._decay_cursor == arts[2].id

    seen = first["scanned"]
    for _ in range(10):
        step = ingest.decay_scores(db, now=NOW)
        seen += step["scanned"]
        if not step["more"]:
            break
    assert seen >= 7
    assert ingest._decay_cursor == 0          # the sweep wrapped
    for art in arts:
        db.refresh(art)
        assert art.importance < 90


def test_settled_articles_are_outside_the_window(db):
    # Past twice the lift ceiling a score cannot move again, so the sweep may
    # skip it for ever — and must, or every pass would walk the whole archive.
    src = _source(db)
    old = _article(db, src, importance=90, base=90,
                   hours_ago=2 * scoring.DECAY_LIFT_MAX_HOURS + 48)
    moved = ingest.decay_scores(db, now=NOW)
    db.refresh(old)
    assert moved["scanned"] == 0
    assert old.importance == 90


def test_an_empty_archive_is_not_an_error(db):
    assert ingest.decay_scores(db, now=NOW)["scanned"] == 0


def test_a_cluster_falls_back_to_its_highest_scoring_report(db):
    src = _source(db)
    event = Event(title="Quiet", importance=90,
                  first_seen=NOW - timedelta(hours=200),
                  updated_at=NOW - timedelta(hours=200))
    db.add(event)
    db.flush()
    a = _article(db, src, importance=90, base=90, hours_ago=200, event=event)
    b = _article(db, src, importance=60, base=60, hours_ago=200, event=event)
    moved = ingest.decay_scores(db, now=NOW)
    db.refresh(event)
    db.refresh(a)
    db.refresh(b)
    assert moved["events"] == 1
    # Still the highest of its members — the rule the FAQ states — but the
    # members have faded, so the cluster has too.
    assert event.importance == max(a.importance, b.importance)
    assert event.importance < 90


def test_a_cluster_whose_members_have_not_moved_is_left_alone(db):
    src = _source(db)
    event = Event(title="Fresh", importance=90,
                  first_seen=NOW - timedelta(hours=1),
                  updated_at=NOW - timedelta(hours=1))
    db.add(event)
    db.flush()
    _article(db, src, importance=90, base=90, hours_ago=1, event=event)
    assert ingest.decay_scores(db, now=NOW)["events"] == 0
