"""Attaching a published reliability judgement to a publisher.

The parse is deliberately keyed on what a reader sees — the five status
phrases and the row's outbound links — rather than on the wiki template that
draws the table, because a template change would otherwise return fewer rows
instead of failing, and a rating that silently disappears looks exactly like an
outlet nobody has assessed.
"""
import pytest

from backend.app import reputation
from backend.app.models import SourceRating

ROW = ('<tr><td><a href="https://{dom}/">{name}</a></td>'
       '<td>{status}</td>'
       '<td><a href="https://en.wikipedia.org/wiki/talk">2019 RfC</a></td></tr>')


def _page(rows, pad=True):
    html = ["<table>"]
    html += [ROW.format(**r) for r in rows]
    if pad:
        # The real list carries well over a thousand entries; the parser
        # refuses anything implausibly short, so a fixture has to clear it.
        html += [ROW.format(dom=f"filler{i}.test", name=f"Filler {i}",
                            status="Generally reliable")
                 for i in range(reputation.MIN_PLAUSIBLE_ROWS + 5)]
    html.append("</table>")
    return "".join(html)


# --- the parse ------------------------------------------------------------

def test_it_reads_a_status_and_a_domain_from_a_row():
    out = reputation.parse_page(_page([
        {"dom": "example-news.test", "name": "Example News",
         "status": "Generally reliable"}]))
    assert out["example-news.test"]["status"] == "Generally reliable"
    assert out["example-news.test"]["rank"] == 1


@pytest.mark.parametrize("phrase,rank", [
    ("Generally reliable", 1),
    ("No consensus", 2),
    ("Generally unreliable", 3),
    ("Deprecated", 4),
    ("Blacklisted", 5),
])
def test_every_status_maps_to_a_rank(phrase, rank):
    out = reputation.parse_page(_page([
        {"dom": "graded.test", "name": "Graded", "status": phrase}]))
    assert out["graded.test"]["rank"] == rank
    assert out["graded.test"]["status"] == phrase


def test_wikipedias_own_links_are_not_rated():
    out = reputation.parse_page(_page([
        {"dom": "outlet.test", "name": "Outlet", "status": "Deprecated"}]))
    assert "wikipedia.org" not in out
    assert "en.wikipedia.org" not in out
    assert "outlet.test" in out


def test_the_worse_of_two_entries_wins():
    # An outlet listed twice should show the warning, not hide behind its
    # better entry.
    out = reputation.parse_page(_page([
        {"dom": "twice.test", "name": "Twice A", "status": "Generally reliable"},
        {"dom": "twice.test", "name": "Twice B", "status": "Deprecated"}]))
    assert out["twice.test"]["status"] == "Deprecated"


def test_a_page_that_stopped_parsing_is_a_failure_not_an_empty_world():
    # The rule every provider here follows: None keeps what is stored, an
    # empty dict would delete all of it.
    assert reputation.parse_page("<table><tr><td>nothing useful</td></tr></table>") is None
    assert reputation.parse_page("") is None
    assert reputation.parse_page(_page([], pad=False)) is None


def test_a_row_with_no_status_is_skipped():
    out = reputation.parse_page(_page([
        {"dom": "unrated.test", "name": "Unrated", "status": "see the talk page"}]))
    assert "unrated.test" not in out


# --- storing --------------------------------------------------------------

def _stored(db):
    return {r.domain: r for r in db.query(SourceRating).all()}


def test_storing_adds_rows(db):
    result = reputation.store(db, {
        "a.test": {"status": "Generally reliable", "rank": 1, "label": "A", "url": "u"}})
    assert result["added"] == 1
    assert _stored(db)["a.test"].rank == 1


def test_a_changed_rating_is_updated_in_place(db):
    reputation.store(db, {
        "a.test": {"status": "Generally reliable", "rank": 1, "label": "A", "url": "u"}})
    result = reputation.store(db, {
        "a.test": {"status": "Deprecated", "rank": 4, "label": "A", "url": "u"}})
    assert result["changed"] == 1 and result["added"] == 0
    assert _stored(db)["a.test"].status == "Deprecated"


def test_an_outlet_dropped_from_the_list_loses_its_rating(db):
    reputation.store(db, {
        "a.test": {"status": "Deprecated", "rank": 4, "label": "A", "url": "u"},
        "b.test": {"status": "Deprecated", "rank": 4, "label": "B", "url": "u"}})
    result = reputation.store(db, {
        "a.test": {"status": "Deprecated", "rank": 4, "label": "A", "url": "u"}})
    assert result["dropped"] == 1
    assert "b.test" not in _stored(db)


# --- looking one up -------------------------------------------------------

def test_a_rated_publisher_is_found(db):
    reputation.store(db, {
        "paper.test": {"status": "Generally reliable", "rank": 1,
                       "label": "Paper", "url": "u"}})
    got = reputation.lookup(db, {"paper.test"})
    assert got["paper.test"]["status"] == "Generally reliable"
    # The chip has to be able to say whose judgement it is.
    assert got["paper.test"]["provider"]
    assert got["paper.test"]["licence"]


def test_a_section_of_a_site_inherits_the_publishers_rating(db):
    reputation.store(db, {
        "paper.test": {"status": "Deprecated", "rank": 4, "label": "P", "url": "u"}})
    assert reputation.lookup(db, {"news.paper.test"})["news.paper.test"]["rank"] == 4


def test_a_subdomain_with_its_own_entry_beats_its_parent(db):
    reputation.store(db, {
        "paper.test": {"status": "Generally reliable", "rank": 1, "label": "P", "url": "u"},
        "blogs.paper.test": {"status": "Generally unreliable", "rank": 3,
                             "label": "P blogs", "url": "u"}})
    got = reputation.lookup(db, {"blogs.paper.test"})
    assert got["blogs.paper.test"]["status"] == "Generally unreliable"


def test_an_unrated_publisher_gets_nothing_rather_than_a_guess(db):
    reputation.store(db, {
        "paper.test": {"status": "Deprecated", "rank": 4, "label": "P", "url": "u"}})
    assert reputation.lookup(db, {"somewhere-else.test"}) == {}


def test_looking_up_nothing_asks_nothing(db):
    assert reputation.lookup(db, set()) == {}
