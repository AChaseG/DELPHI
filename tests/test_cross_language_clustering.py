"""Cross-language event clustering via language-invariant anchors (Gap B)."""
from backend.app.clustering import _best_match
from backend.app.geo import extract_places
from backend.app.models import Article, Event
from backend.app.scoring import cluster_tokens, invariant_anchors


def _tokens(title, summary=""):
    text = f"{title}\n{summary}"
    return cluster_tokens(title, text, extract_places(text))


def _article(title, summary=""):
    a = Article(title=title, summary=summary)
    a.cluster_tokens = _tokens(title, summary)
    return a


def _event(title, summary=""):
    e = Event(title=title)
    e.cluster_tokens = _tokens(title, summary)
    return e


def test_anchors_extract_city_and_significant_numbers():
    anchors = set(invariant_anchors(
        "Magnitude 6.8 earthquake strikes Tokyo, 200 evacuated",
        extract_places("Magnitude 6.8 earthquake strikes Tokyo")))
    assert "tokyo" in anchors and "6.8" in anchors and "200" in anchors


def test_anchors_skip_years_and_lone_digits():
    anchors = set(invariant_anchors("The 2024 summit opened with 5 leaders", []))
    assert "2024" not in anchors and "5" not in anchors


def test_numbers_extracted_next_to_cjk():
    # \b fails against CJK; the lookaround regex still finds the magnitude.
    anchors = set(invariant_anchors("東京でマグニチュード6.8の地震", extract_places("東京でマグニチュード6.8の地震")))
    assert "6.8" in anchors and "tokyo" in anchors


def test_japanese_coverage_clusters_with_english_event():
    quake = _event("Magnitude 6.8 earthquake strikes Tokyo, tsunami warning issued")
    jp = _article("東京でマグニチュード6.8の地震、津波警報")
    assert _best_match(jp, [quake]) is quake


def test_unrelated_same_city_story_does_not_merge():
    quake = _event("Magnitude 6.8 earthquake strikes Tokyo, tsunami warning issued")
    museum = _article("Tokyo art museum unveils a major new exhibition")
    assert _best_match(museum, [quake]) is None


def test_single_shared_token_is_not_enough():
    # A lone city token must not collapse into an unrelated same-city event.
    stock = _event("Tokyo stock exchange climbs to a record high on tech shares")
    sparse = _article("東京")  # yields only {tokyo}
    assert _best_match(sparse, [stock]) is None
