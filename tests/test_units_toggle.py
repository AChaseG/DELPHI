"""Miles or kilometres, and the line that keeps it from becoming a bug.

Distances appear in a dozen places — the radius slider, the fire ring, the air
reading, hazard popups, toasts, the locations pane, the feed builder's summary
of a drawn circle, and the wildfire email. A reader who thinks in miles should
see miles in all of them.

The discipline that makes this safe is one sentence: **kilometres are the only
unit that exists below the point of printing.** Everything is stored, sent,
compared and geofenced in km — the API, the database, the haversine, the
`fire_km` on a saved place — and the conversion happens on the way to the
screen and nowhere else. There is therefore no path by which a converted number
gets written back, or compared against an unconverted one.

That is not fussiness. Unit bugs are never arithmetic mistakes; they are the
same number meaning two things in two places, and the way you avoid them is to
make sure it only ever means one.

The email is the interesting case: it is the one surface the reader cannot flip
a toggle to reinterpret, so it has to leave the server already in the unit the
account chose.
"""
import pathlib
import re

import pytest

from backend.app import hazards, mailer
from backend.app.models import User

FRONTEND = pathlib.Path(__file__).resolve().parent.parent / "frontend"
APP = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
API_JS = (FRONTEND / "js" / "api.js").read_text(encoding="utf-8")
BUILDER = (FRONTEND / "js" / "builder.js").read_text(encoding="utf-8")
HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")


# ---------- the preference ----------

def test_it_is_an_account_preference_not_a_device_one():
    """It rides in the same Settings object as the theme, which writes through
    to the account — so it follows the reader to their phone rather than
    resetting on every new browser."""
    assert re.search(r'units:\s*"km"', API_JS), "no default in Settings"
    assert 'id="set-units"' in HTML
    assert 'Settings.set("units"' in APP


def test_changing_it_repaints_without_refetching():
    """The numbers are already in the browser; only their wording changed."""
    src = APP[APP.index('units.onchange'):]
    src = src[:src.index("\n  };")]
    assert "renderBoard()" in src
    assert "LocationsPanel.refresh()" in src
    assert "API." not in src, "nothing here needs the server again"


# ---------- the conversion happens once, at the edge ----------

def test_there_is_one_converter():
    assert "function fmtKm(" in APP
    assert re.search(r"KM_PER_MILE\s*=\s*1\.609344", APP), (
        "the exact factor, not 1.6")


def test_no_display_string_still_hardcodes_km():
    """Every one of these was a place a mile-reader would have been shown a
    kilometre. The check is mechanical because the list will grow."""
    for name, src in (("app.js", APP), ("builder.js", BUILDER)):
        if "function fmtKm(" in src:
            # The converter is the one place allowed to write the word, since
            # writing it is its job. Everywhere else must go through it.
            start = src.index("function fmtKm(")
            src = src[:start] + src[src.index("\n}\n", start):]
        code = "\n".join(line for line in src.splitlines()
                         if not line.strip().startswith(("*", "//", "/*")))
        leftovers = re.findall(r"`[^`]*\}\s*km[^`]*`", code)
        assert not leftovers, f"{name} still prints km directly: {leftovers}"


def test_the_maths_never_sees_a_mile():
    """The haversine, the ring test and the stored radius are all kilometres.
    If a converted value could reach any of them, the conversion would stop
    being a display concern and start being a correctness one."""
    hit = APP[APP.index("function fireRingHit"):APP.index("\n}\n", APP.index("function fireRingHit"))]
    assert "fmtKm" not in hit
    assert "KM_PER_MILE" not in hit
    hav = APP[APP.index("function haversineKm"):APP.index("\n}\n", APP.index("function haversineKm"))]
    assert "KM_PER_MILE" not in hav


def test_the_sliders_still_hold_kilometres():
    """Only their labels convert. A slider whose *value* changed unit would be
    sending miles to an API that reads kilometres."""
    for fn in ("radiusKm()", "fireKm()"):
        src = APP[APP.index(f"  {fn} {{"):]
        src = src[:src.index("\n")]
        assert "KM_PER_MILE" not in src and "fmtKm" not in src, src


# ---------- the server half ----------

@pytest.mark.parametrize("km,units,expected", [
    (100.0, "km", "100.0 km"),
    (100.0, "mi", "62.1 miles"),
    (0.0, "km", "0.0 km"),
    (1.609344, "mi", "1.0 miles"),
])
def test_the_mailer_converts(km, units, expected):
    assert mailer.format_km(km, units) == expected


def test_the_mailer_defaults_to_kilometres():
    assert mailer.format_km(50.0) == "50.0 km"


def test_an_email_arrives_in_the_unit_the_account_chose():
    """The one surface with no toggle on it."""
    sent = {}
    real_send = mailer.send
    mailer.send = lambda to, subject, body: sent.update(body=body) or True
    try:
        mailer.send_hazard_digest("a@example.com", "Home", [{
            "name": "Sawtooth", "distance_km": 80.4, "acres": 5000,
            "containment": 10, "again": False}], "mi")
    finally:
        mailer.send = real_send
    assert "50.0 miles" in sent["body"]
    assert " km" not in sent["body"]


def test_units_are_read_off_the_account():
    assert hazards._units_of(User(settings='{"units": "mi"}')) == "mi"
    assert hazards._units_of(User(settings='{"units": "km"}')) == "km"


@pytest.mark.parametrize("settings", [None, "", "not json", "[]", "{}"])
def test_an_unreadable_preference_falls_back_to_kilometres(settings):
    """Settings is a free-form JSON blob written by the client. A malformed one
    must not stop an email that says a fire is near somebody's house."""
    assert hazards._units_of(User(settings=settings)) == "km"
