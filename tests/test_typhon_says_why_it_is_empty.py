"""An empty hazard layer has to say why it is empty.

Reported from a live instance: both Typhon layers switched on over Seattle,
both drawing nothing. They were behaving correctly — `NEWS_HAZARDS` had never
been set, so the poller had never fetched anything and the table was empty —
but nothing anywhere said so. A feature switched off at the server, a provider
that could not be reached, a missing API key, and a genuinely quiet corner of
the map all produced exactly the same thing: a ticked box and a blank map.

That is a design failure and not a configuration mistake. The fix has three
parts, and this file guards all of them:

  · the flag defaults to **on** now, because the off state was
    indistinguishable from the working one, and the real opt-in was always the
    map's own layer toggles;
  · `ingest.status["hazards"]` exists from import rather than from the first
    successful poll, so there is always something to read;
  · the map and Settings both read it, and their wording distinguishes
    "nothing here" from "nothing working".

The rule underneath all of it: **"nothing here" and "nothing working" must
never read the same.**
"""
import pathlib
import re

import pytest

from backend.app import hazards, ingest

FRONTEND = pathlib.Path(__file__).resolve().parent.parent / "frontend"
APP = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")


def _fn(name: str, end: str = "\n}\n") -> str:
    start = APP.index(name)
    return APP[start:APP.find(end, start)]


# ---------- there is always something to read ----------

def test_the_status_key_exists_before_any_poll_has_run():
    """It used to appear only after a poll succeeded, so /api/meta — which
    ships this dict to every client — said nothing at all about hazards on the
    instance where it mattered most."""
    assert "hazards" in ingest.status
    assert set(hazards.idle_status("x")) >= {"ok", "enabled", "reason", "no_key"}


def test_a_never_run_poller_reports_a_reason_rather_than_silence():
    idle = hazards.idle_status("no poll has run yet")
    assert idle["ok"] is False
    assert idle["reason"] == "no poll has run yet"


def test_the_flag_and_the_reason_never_disagree(monkeypatch):
    monkeypatch.setenv("NEWS_HAZARDS", "0")
    assert hazards.idle_status("switched off")["enabled"] is False
    monkeypatch.setenv("NEWS_HAZARDS", "1")
    assert hazards.idle_status("no poll has run yet")["enabled"] is True


def test_a_missing_key_is_named_not_swallowed(monkeypatch):
    """The one failure an operator can fix in a minute, and would otherwise
    never learn about — the air layer would simply be empty for good.

    Named one by one rather than as a count, because the two air providers
    cover different halves of the world: an operator who sets AirNow still has
    a blank layer everywhere outside the United States, and "1 key missing"
    would not tell them which half."""
    monkeypatch.delenv("AIRNOW_API_KEY", raising=False)
    monkeypatch.delenv("OPENAQ_API_KEY", raising=False)
    monkeypatch.delenv("PURPLEAIR_API_KEY", raising=False)
    assert set(hazards.idle_status("x")["no_key"]) == {"airnow", "openaq",
                                                       "purpleair"}
    monkeypatch.setenv("AIRNOW_API_KEY", "sk-whatever")
    assert "airnow" not in hazards.idle_status("x")["no_key"]
    monkeypatch.setenv("OPENAQ_API_KEY", "sk-whatever")
    monkeypatch.setenv("PURPLEAIR_API_KEY", "sk-whatever")
    assert hazards.idle_status("x")["no_key"] == []


def test_the_loop_reports_every_reason_it_skips_a_poll():
    """Off, and paused for disk, are different sentences — and neither may be
    silence."""
    import inspect
    src = inspect.getsource(ingest.ingest_loop)
    assert "NEWS_HAZARDS=0" in src
    assert "volume is nearly full" in src
    assert src.index("idle_status") < src.index("hazards.poll(db)"), (
        "the skip branches have to set a status, not fall through")


def test_the_client_is_told(client, register):
    """/api/meta already carries ingest.status; the point is that the hazard
    key is in it from the first request."""
    headers = register("statreader")
    meta = client.get("/api/meta", headers=headers).json()
    assert "hazards" in meta["ingest"]
    assert "enabled" in meta["ingest"]["hazards"]


# ---------- and it is said where somebody is looking ----------

NOTE = _fn("  hazardNote()", "\n  },\n")


@pytest.mark.parametrize("case", [
    "switched off", "no data right now", "AirNow API key", "Nothing reported",
])
def test_the_map_note_distinguishes_the_cases(case):
    """Four different empties, four different sentences. If any two collapsed
    into one, the reader would be back where they started."""
    assert case in NOTE


def test_the_note_only_appears_when_a_layer_is_on():
    assert "hasLayer(o.group)" in NOTE
    assert 'if (!on) return "";' in NOTE


def test_the_note_says_nothing_when_there_is_nothing_to_say():
    """A working layer over a busy map must not carry a caption."""
    assert NOTE.rstrip().endswith('return "";')


def test_markers_on_screen_beat_any_explanation():
    """Caught in a browser: a failed poll while the previous poll's rows were
    still inside their retention window produced "Typhon has no data right
    now" printed over three visible markers. Anything drawn means the map is
    already answering, and a caption contradicting it is worse than none."""
    assert "if (rows.length) return \"\";" in NOTE
    assert NOTE.index("rows.length") < NOTE.index("s.enabled === false")


def test_a_missing_key_is_still_said_when_fires_are_drawing():
    """The air layer can be empty for a reason of its own while wildfires draw
    perfectly well — and that reason is the one an operator can fix, so it is
    not allowed to be swallowed by the rule above."""
    assert NOTE.index("no_key") < NOTE.index("if (rows.length)")
    assert 'h.kind === "air_quality"' in NOTE, (
        "it has to check for air rows specifically, not rows in general")


def test_the_note_sits_under_the_switches_it_explains():
    """It began as a card floating over the map, which meant covering the very
    thing it was explaining. Now that the layer toggles are a panel of their
    own, the explanation belongs directly beneath them — beside the checkbox
    somebody has just ticked, which is where they are looking."""
    assert 'role="status"' in HTML
    panel = HTML[HTML.index('id="map-layers"'):HTML.index("</aside>",
                                                          HTML.index('id="map-layers"'))]
    assert 'id="map-note"' in panel
    assert panel.index('id="layer-overlays"') < panel.index('id="map-note"')


def test_a_refreshed_status_updates_the_map_too():
    """META is refreshed in one place — renderStats, off the stream's cycle
    event. Updating only Settings there would leave the map's caption saying
    "no data" for as long as the reader stayed on it, which is the same
    staleness bug in a new place."""
    src = _fn("function renderStats")
    assert "MapBoard.renderHazardNote()" in src


def test_settings_carries_the_same_story():
    """Where an operator already goes to ask how Delphi is doing."""
    assert 'id="stat-hazards"' in HTML
    src = _fn("function renderHazardStatus")
    assert "NEWS_HAZARDS=0" in src
    assert "AIRNOW_API_KEY" in src
    assert "META.ingest.hazards" in src


# ---------- the first-load fetch ----------

def test_a_remembered_layer_fetches_without_waiting_for_a_pan():
    """Separate bug found while fixing the above. `_init` calls setView on its
    first line and registers `moveend` thirty lines later, and Leaflet only
    fires `overlayadd` from the control's own checkbox — never from addTo. So
    neither route ran on a fresh load and a remembered layer stayed blank until
    the reader happened to pan.

    Every browser probe called setView after opening the board, which fired
    moveend and hid this completely.
    """
    init = _fn("  async _init()", "\n  },\n")
    assert "this.refreshHazards();" in init
    assert init.index("L.control.layers") < init.index("this.refreshHazards();")
