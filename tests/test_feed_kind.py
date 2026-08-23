"""Telling a news feed from a ticketing calendar by reading it.

The catalog's only defences were a list of domains somebody thought of and a
rule that a domain must recur. Neither ever looked at what a feed publishes,
which is how a record label's ticket calendar became a national news source.
"""
import time
from datetime import datetime, timedelta

import pytest

from backend.app import discovery, feedkind, ingest
from backend.app.models import Source

NOW = datetime(2026, 8, 23, 12, 0, 0)


def _stamp(when: datetime):
    return time.struct_time(when.timetuple())


def _entry(title, link="", summary="", when=None):
    entry = {"title": title, "link": link, "summary": summary}
    if when is not None:
        entry["published_parsed"] = _stamp(when)
    return entry


def _news(n=10):
    """A plausible newspaper feed: varied headlines, article paths, past dates."""
    headlines = [
        "Council approves budget after late amendment",
        "Two rescued from flooded underpass",
        "Steelworks to cut 400 jobs by spring",
        "Heatwave breaks August record in the south",
        "Ferry service resumes after week-long strike",
        "Hospital trust names new chief executive",
        "Cyclists win case over disputed lane",
        "Wildfire smoke closes three schools",
        "Election petition dismissed by high court",
        "Bridge repairs to close river road for a month",
        "Museum returns disputed bronzes",
        "Rain delays harvest across the valley",
    ]
    return [_entry(headlines[i % len(headlines)] + f" ({i})",
                   link=f"https://paper.test/2026/08/{20 - i % 5}/story-{i}",
                   summary="Reporting from the scene, our correspondent writes.",
                   when=NOW - timedelta(hours=3 * i + 1))
            for i in range(n)]


def _tickets(n=12):
    """A ticketing calendar: templated titles, event paths, future dates."""
    return [_entry(f"Band Number {i} at The Venue — Live in Concert",
                   link=f"https://label.test/events/tickets/show-{i}",
                   summary="Buy tickets now. Doors open 19:00. Early bird $25.",
                   when=NOW + timedelta(days=5 + i))
            for i in range(n)]


# --- the feed we would want -----------------------------------------------

def test_a_newspaper_reads_as_news():
    verdict = feedkind.classify(_news(), now=NOW)
    assert verdict.is_news
    assert verdict.kind == "news"
    assert verdict.reasons == []


def test_a_newspaper_with_an_events_section_is_still_news():
    # A paper links into its own listings; what it does not do is publish a
    # feed where nine in ten links are tickets.
    entries = _news(10)
    entries[0]["link"] = "https://paper.test/events/summer-fair"
    entries[1]["link"] = "https://paper.test/jobs/board"
    assert feedkind.classify(entries, now=NOW).is_news


def test_a_headline_quoting_a_price_is_still_news():
    entries = _news(8)
    for i, entry in enumerate(entries):
        entry["title"] = f"Treasury commits $1.2bn to flood defences ({i})"
    assert feedkind.classify(entries, now=NOW).is_news


def test_a_feed_that_mislabels_its_timezone_is_still_news():
    # Writing local time and calling it UTC puts a feed up to fourteen hours
    # ahead. That is a bug in a real news feed, not an events calendar.
    entries = [_entry(f"Report from the coast {i}",
                      link=f"https://paper.test/2026/08/23/story-{i}",
                      when=NOW + timedelta(hours=13))
               for i in range(10)]
    assert feedkind.classify(entries, now=NOW).is_news


def test_an_empty_or_broken_feed_is_not_accused_of_anything():
    # "I could not tell" and "this is not news" are different answers, and only
    # one of them may switch a source off.
    assert feedkind.classify([]).is_news
    assert feedkind.classify(None).is_news
    assert feedkind.classify([object(), object()]).is_news


def test_a_short_feed_cannot_trip_a_proportion():
    assert feedkind.classify(_tickets(2), now=NOW).is_news


# --- the feeds we would not -----------------------------------------------

def test_a_ticketing_calendar_is_declined():
    verdict = feedkind.classify(_tickets(), now=NOW)
    assert not verdict.is_news
    assert verdict.kind == "events"
    assert verdict.reasons


def test_future_dating_alone_is_enough():
    # No vocabulary, no giveaway paths, varied titles — just dates that no
    # publication could have.
    entries = [_entry(f"Something entirely unremarkable number {i}",
                      link=f"https://site.test/page-{i}",
                      when=NOW + timedelta(days=10 + i))
               for i in range(8)]
    verdict = feedkind.classify(entries, now=NOW)
    assert not verdict.is_news
    assert "future" in verdict.summary()


def test_one_weak_signal_alone_is_not_enough():
    # Shop paths and nothing else: real, but not on its own worth overruling a
    # source. The bar is two independent signals.
    entries = _news(10)
    for entry in entries:
        # Same varied, written headlines; only the link shape changes.
        entry["link"] = entry["link"].replace("/2026/08/", "/products/2026/")
    verdict = feedkind.classify(entries, now=NOW)
    assert verdict.is_news
    assert verdict.score == 1
    assert "commerce pages" in verdict.reasons[0]


def test_a_storefront_is_declined_on_two_signals():
    entries = [_entry(f"Blue Widget Mark {i} — Now In Stock",
                      link=f"https://shop.test/products/widget-{i}",
                      summary="Add to cart. Free shipping over $50.",
                      when=NOW - timedelta(hours=i + 1))
               for i in range(10)]
    verdict = feedkind.classify(entries, now=NOW)
    assert not verdict.is_news
    assert verdict.kind == "commerce"


def test_a_jobs_board_is_declined():
    entries = [_entry(f"Senior Engineer — Remote Position {i}",
                      link=f"https://board.test/jobs/listing-{i}",
                      summary="Apply now. Salary range competitive. Full-time.",
                      when=NOW - timedelta(hours=i + 1))
               for i in range(10)]
    verdict = feedkind.classify(entries, now=NOW)
    assert not verdict.is_news
    assert verdict.kind == "jobs"


def test_generated_headlines_are_recognised_as_a_template():
    assert feedkind._template_similarity(_tickets()) >= feedkind.TEMPLATE_SIMILARITY
    assert feedkind._template_similarity(_news()) < feedkind.TEMPLATE_SIMILARITY


def test_the_reason_names_what_it_saw():
    summary = feedkind.classify(_tickets(), now=NOW).summary()
    assert summary.startswith("events:")
    # An operator has to be able to disagree with it, which means reading it.
    assert "%" in summary


# --- the sweep over sources already adopted -------------------------------

def _source(db, **kw):
    fields = dict(name="Somewhere", rss_url=f"https://x.test/{id(kw)}",
                  homepage="https://x.test", added_by="auto-discovered",
                  enabled=True)
    fields.update(kw)
    src = Source(**fields)
    db.add(src)
    db.flush()
    return src


def test_the_sweep_switches_off_an_adopted_ticket_calendar(db):
    src = _source(db)
    assert ingest.check_feed_kind(src, _tickets()) is True
    assert src.enabled is False
    assert src.feed_kind == "events"
    assert "not a news feed" in src.last_status


def test_the_sweep_leaves_a_newspaper_alone(db):
    src = _source(db)
    assert ingest.check_feed_kind(src, _news()) is False
    assert src.enabled is True
    assert src.feed_kind == "news"


def test_a_source_someone_added_by_hand_is_never_switched_off(db):
    src = _source(db, added_by="user")
    assert ingest.check_feed_kind(src, _tickets()) is False
    assert src.enabled is True
    # Still recorded, so an operator can see what Delphi thinks of it.
    assert src.feed_kind == "events"


def test_a_topic_tracker_is_never_switched_off(db):
    # The feed is a query its owner wrote, and a query about concert tickets
    # legitimately returns ticket pages.
    src = _source(db, added_by="topic-tracker")
    assert ingest.check_feed_kind(src, _tickets()) is False
    assert src.enabled is True


def test_the_seed_catalog_is_never_switched_off(db):
    src = _source(db, added_by="catalog")
    assert ingest.check_feed_kind(src, _tickets()) is False
    assert src.enabled is True


def test_a_verdict_stands_for_a_while_before_it_is_asked_again(db):
    src = _source(db)
    assert ingest._kind_check_due(src) is True
    ingest.check_feed_kind(src, _news())
    assert ingest._kind_check_due(src) is False
    src.feed_kind_at = datetime.utcnow() - timedelta(
        days=ingest.KIND_RECHECK_DAYS + 1)
    assert ingest._kind_check_due(src) is True


def test_a_paper_that_became_a_shop_is_caught_on_re_check(db):
    src = _source(db)
    ingest.check_feed_kind(src, _news())
    assert src.enabled is True
    src.feed_kind_at = datetime.utcnow() - timedelta(
        days=ingest.KIND_RECHECK_DAYS + 1)
    assert ingest._kind_check_due(src)
    assert ingest.check_feed_kind(src, _tickets()) is True
    assert src.enabled is False
