"""A filter may only read the headline and the article. Nothing else.

Reported as filters "picking up articles unrelated to keywords or Boolean
logic". A pass over every text-matching path in the app found no *extra field*
being searched — keywords, boolean queries, the FTS5 index and alert evaluation
all read title, summary and body and nothing more, and this file pins that down
so a fourth field cannot be added by accident.

What was wrong was the contents of those three fields. Two things reached them
that a reader would never call the article:

  * page furniture in the body — promo rails, "most read" boxes, consent
    notices, subscription pitches, whole navigation menus. See
    test_content_extraction.py for the extraction rules that now drop them.
  * the feed generator's trailer in the summary. WordPress ends every item with
    "The post <headline> appeared first on <Site>", which puts the
    publication's own name in every article's searchable text: a feed watching
    "energy" collected everything Energy Voice published, whatever it was
    about, and every term in it matched a place no reader can see.

Bodies already stored keep whatever they were stored with — the page they came
from is not kept, so there is nothing to re-extract from. The archive is bounded
by size rather than age and holds a bit over a week in practice, so the
pollution ages out on its own within days.
"""
import re

from backend.app import ingest, matching
from backend.app.boolean_query import compile_query
from backend.app.content import clean_summary
from backend.app.models import Article, Source


def article(**kw) -> Article:
    fields = {"title": "", "summary": "", "content": "", "importance": 0,
              "language": "en", "country": "", "categories": [], "places": [],
              "source_id": 1, "cluster_tokens": []}
    fields.update(kw)
    return Article(**fields)


# ---------- the three fields, and only those three ----------

def test_the_searched_text_is_headline_summary_and_body():
    text = matching.article_text(article(title="Head", summary="Sum",
                                        content="Body"))
    assert text.split("\n") == ["Head", "Sum", "Body"]


def test_nothing_else_on_the_article_is_searchable():
    """Every other column a filter could have read by mistake. Each is a real
    string on the row, and each one names things the article is not about — the
    outlet's name, a section slug, the words a clustering algorithm kept."""
    a = article(title="Council approves the bridge repair", summary="",
                content="Work begins in June.",
                url="https://example.com/tech/data-center/bridge-repair",
                guid="hyperscale-12345",
                categories=["technology", "colocation"],
                places=[{"name": "Hyperscale", "country": "US"}],
                cluster_tokens=["colocation", "hyperscale"],
                image_url="https://example.com/img/data-center.jpg")
    a.source = Source(name="Hyperscale Colocation Weekly", homepage="",
                      rss_url="", scope="national", country="US")
    text = matching.article_text(a)
    for word in ("data-center", "hyperscale", "colocation", "Weekly", ".jpg"):
        assert word.lower() not in text.lower(), (
            f"{word!r} is searchable and it is not part of the article")


def test_a_keyword_in_the_url_alone_does_not_match():
    m = matching.CriteriaMatcher({"keywords": ["data center"]})
    a = article(title="Council approves the bridge repair",
                content="Work begins in June.",
                url="https://example.com/tech/data-center/bridge")
    a.source = Source(name="X", homepage="", rss_url="", scope="national",
                      country="US")
    assert not m.matches(a)


def test_a_keyword_in_the_source_name_alone_does_not_match():
    m = matching.CriteriaMatcher({"keywords": ["energy"]})
    a = article(title="Council approves the bridge repair",
                content="Work begins in June.")
    a.source = Source(name="Energy Voice", homepage="", rss_url="",
                      scope="national", country="US")
    assert not m.matches(a)


def test_the_full_text_index_covers_the_same_three_columns():
    """Asked of the built table, not of the source. The index only ever offers
    candidates — the matcher decides — but an index over a fourth column would
    be a different candidate set for the same search, which is a difference
    nobody can explain."""
    from backend.app.database import engine
    with engine.begin() as conn:
        cols = [r[1] for r in
                conn.exec_driver_sql("PRAGMA table_info(articles_fts)")]
    assert cols == ["title", "summary", "content"]


def test_explaining_a_match_names_only_those_fields():
    """The explanation and the match have to be looking at the same text, or
    "your term is in the body" is said about a term that is somewhere else."""
    hits = matching.explain_text_match(
        {"keywords": ["bridge"]},
        article(title="Council approves the bridge repair"))
    assert [h["where"] for h in hits] == ["headline"]
    import inspect
    src = inspect.getsource(matching.explain_text_match)
    fields = set(re.findall(r'\("(\w+)", article\.(\w+)', src))
    assert fields == {("headline", "title"), ("summary", "summary"),
                      ("body", "content")}


def test_a_watched_place_reads_the_headline_and_summary_only():
    """Deliberately narrower than the rest: a story *about* somewhere names it
    up front, and a mention buried in a body is usually a dateline or a list."""
    assert matching.headline_text(
        article(title="A", summary="B", content="C")).split("\n") == ["A", "B"]


# ---------- the generator's trailer is not the article ----------

def test_the_wordpress_trailer_is_stripped():
    cleaned = clean_summary(
        "Ministers approved the scheme on Tuesday. The post Ministers approve "
        "the scheme appeared first on Energy Voice.")
    assert cleaned == "Ministers approved the scheme on Tuesday."


def test_the_publication_name_stops_matching_every_article_it_publishes():
    """The reported shape of it, end to end."""
    raw = ("The council rejected the bypass. The post Council rejects bypass "
           "appeared first on Energy Voice.")
    matches = compile_query("energy")
    assert matches(raw), "precondition: the trailer is why this matched"
    assert not matches(clean_summary(raw))


def test_share_and_read_more_furniture_is_stripped():
    assert clean_summary("Two clubs filed a complaint. Share this: Twitter "
                         "Facebook Reddit") == "Two clubs filed a complaint."
    assert clean_summary("Two clubs filed a complaint. Continue reading "
                         "at our site") == "Two clubs filed a complaint."
    assert clean_summary("Two clubs filed a complaint. Filed under: Sport, "
                         "Politics") == "Two clubs filed a complaint."


def test_a_story_that_happens_to_say_it_keeps_its_words():
    """Anchored at the end, where a generator puts its trailer, so prose that
    uses the same words survives."""
    text = ("The share this campaign, as it was branded, was filed under a "
            "protest licence and drew four thousand people to the square on "
            "Tuesday afternoon.")
    assert clean_summary(text) == text


def test_ingest_cleans_the_summary_it_stores():
    import inspect
    src = inspect.getsource(ingest.process_entries)
    assert "clean_summary(" in src, (
        "an uncleaned summary is stored, searched, and shown on the card")


def test_cleaning_a_summary_never_fails():
    for value in ("", None, "The post", "Share this:", "appeared first on"):
        assert isinstance(clean_summary(value), str)
