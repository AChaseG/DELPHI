"""Gazetteer reconciliation + multilingual place extraction (Gap A)."""
from backend.app import geo


def names(text):
    return [p["name"] for p in geo.extract_places(text)]


def test_catalog_cities_are_geotaggable():
    # Cities present in the source catalog but not the original 170-city
    # gazetteer are now recognized, placed in the right country.
    merged = {(c["name"], c["country"]) for c in geo._merged_cities()}
    assert len(geo._merged_cities()) > 400
    for city, cc in [("Porto", "PT"), ("Surabaya", "ID"), ("Medellín", "CO"),
                     ("Nairobi", "KE"), ("Bulawayo", "ZW")]:
        assert (city, cc) in merged
        got = geo.extract_places(f"News broke in {city} overnight.")
        assert any(p["name"] == city and p["country"] == cc for p in got)


def test_centroid_fallback_is_country_correct():
    # A catalog city without exact coordinates sits at its country centroid —
    # approximate, but never in the wrong country for geofencing.
    porto = next(c for c in geo._merged_cities() if c["name"] == "Porto")
    pt = geo.countries_index()["PT"]
    assert (porto["lat"], porto["lon"]) == (pt["lat"], pt["lon"])


def test_native_script_aliases_cjk():
    assert "Tokyo" in names("東京で大きな地震が発生した")
    assert "Beijing" in names("北京で会議が開かれた")
    assert "Seoul" in names("서울에서 시위가 열렸다")


def test_native_script_aliases_other_scripts():
    assert "Athens" in names("Πυρκαγιά ξέσπασε στην Αθήνα")     # Greek
    assert "Baghdad" in names("وقع انفجار في بغداد اليوم")       # Arabic


def test_cyrillic_declension_matches_inflected_forms():
    # Russian/Ukrainian decline place names; the stem match catches cases.
    assert "Moscow" in names("Взрыв произошёл в Москве")         # prepositional
    assert "Moscow" in names("Президент прибыл в Москву")        # accusative
    assert "Kyiv" in names("Бої тривають під Києвом")            # Ukrainian instrumental


def test_no_false_positive_on_short_tokens():
    # Two-letter country aliases stay case-sensitive; ordinary words don't tag.
    assert "India" not in names("the meeting is in an hour")
