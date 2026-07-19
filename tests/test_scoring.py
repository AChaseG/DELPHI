"""Multilingual categorization + breaking-signal scoring."""
from backend.app.scoring import breaking_signal, classify_categories, score_importance


def cat(text, tags=None):
    return classify_categories(text, None, tags)


def test_english_categories_unchanged():
    assert "sports" in cat("Champions League final ends in a dramatic penalty shootout")
    assert "disaster" in cat("Major earthquake triggers tsunami warning")


def test_multilingual_categories():
    assert "disaster" in cat("Fuerte terremoto sacude la capital y provoca evacuación")   # es
    assert "conflict" in cat("Guerre : une attaque fait plusieurs morts")                  # fr
    assert "disaster" in cat("Schwere Überschwemmung nach Unwetter in der Region")          # de
    assert "politics" in cat("Eleições: apuração aponta virada da oposição")                # pt
    assert "conflict" in cat("Война: взрыв в центре города, есть погибшие")                 # ru
    assert "disaster" in cat("زلزال قوي يضرب المدينة ويسبب دمارا")                          # ar


def test_cjk_substring_categories():
    assert "disaster" in cat("東京で大きな地震、津波警報が発令")   # ja
    assert "conflict" in cat("市中心发生爆炸 造成多人死亡")         # zh
    assert "politics" in cat("정부는 오늘 선거 결과를 발표했다")   # ko


def test_rss_section_tags():
    assert "sports" in cat("Headline with no keywords", ["Sports"])
    assert "politics" in cat("Un titre neutre", ["Politique"])
    assert cat("A neutral headline about nothing", ["Weekend Reads"]) == ["world"]


def test_breaking_signal_multilingual_and_capped():
    assert breaking_signal("Explosión y ataque dejan muertos") > 0
    assert breaking_signal("地震と津波の警報が発令された") > 0
    assert breaking_signal("Un día tranquilo en el parque de la ciudad") == 0
    assert breaking_signal("guerra ataque explosión terremoto tsunami incendio") <= 30


def test_importance_scoring_bounds():
    s = score_importance("Breaking: massive earthquake, tsunami warning", "international", 1,
                         [{"country": "JP"}, {"country": "KR"}], corroborating_sources=3)
    assert 5 <= s <= 100 and s >= 80  # critical
    low = score_importance("local council approves parking rules", "local", 3, [])
    assert low < 40
