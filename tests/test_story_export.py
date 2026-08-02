"""Taking a Focus away as a file.

A column is a standing interest; a Focus is a finding — one thing that
happened, with the coverage of it already gathered. That is the unit somebody
actually wants to hand on, so it exports as a document rather than being
retyped out of the modal.
"""
import itertools
import zipfile
from datetime import timedelta
from io import BytesIO

import pytest

from backend.app.models import Article, Event, Source, utcnow

SEQ = itertools.count(1)


@pytest.fixture
def wire(db):
    s = Source(name="The Wire", rss_url="http://wire.example/feed", scope="international")
    db.add(s)
    db.commit()
    return s


def _report(db, wire, title, *, event=None, minutes_ago=1):
    a = Article(source_id=wire.id, title=title, url=f"http://wire.example/{next(SEQ)}",
                summary=f"{title} — what happened.", content="",
                published_at=utcnow() - timedelta(minutes=minutes_ago),
                fetched_at=utcnow(), importance=64, country="GR",
                event_id=event.id if event else None)
    db.add(a)
    db.commit()
    return a


@pytest.fixture
def covered_story(db, wire):
    """One happening, carried by three outlets."""
    event = Event(title="Quake off the coast", updated_at=utcnow())
    db.add(event)
    db.commit()
    picked = _report(db, wire, "Quake off the coast", event=event, minutes_ago=30)
    _report(db, wire, "Tremor felt across the islands", event=event, minutes_ago=10)
    _report(db, wire, "Coastguard assessing damage", event=event, minutes_ago=5)
    return picked


def test_a_focus_exports_every_outlet_carrying_it(client, register, covered_story):
    headers = register("reader")
    resp = client.get(f"/api/story/{covered_story.id}/export?format=csv", headers=headers)

    assert resp.status_code == 200
    assert resp.headers["X-Export-Rows"] == "3", "only part of the coverage came out"


def test_the_picked_report_leads(client, register, covered_story):
    """It is the one the reader chose, whatever its timestamp.

    Sorting the file newest-first would bury it under coverage that came later.
    """
    headers = register("reader")
    body = client.get(f"/api/story/{covered_story.id}/export?format=csv",
                      headers=headers).text

    rows = [ln for ln in body.splitlines() if ln.strip()]
    assert "Quake off the coast" in rows[1], "the chosen report was not first"


def test_a_report_nobody_else_carried_exports_as_itself(client, register, db, wire):
    """An unclustered story is simply one nobody else has yet — not an error."""
    alone = _report(db, wire, "Council approves the bypass")
    headers = register("reader")

    resp = client.get(f"/api/story/{alone.id}/export?format=csv", headers=headers)

    assert resp.status_code == 200
    assert resp.headers["X-Export-Rows"] == "1"


def test_it_defaults_to_a_readable_document(client, register, covered_story):
    """The other exports feed a spreadsheet; this one gets read."""
    headers = register("reader")
    resp = client.get(f"/api/story/{covered_story.id}/export", headers=headers)

    assert resp.status_code == 200
    assert "wordprocessingml" in resp.headers["content-type"]


def test_the_file_is_named_after_the_story(client, register, covered_story):
    headers = register("reader")
    disp = client.get(f"/api/story/{covered_story.id}/export?format=csv",
                      headers=headers).headers["Content-Disposition"]
    assert "quake" in disp.lower()


@pytest.mark.parametrize("fmt", ["csv", "md", "json", "xlsx", "docx"])
def test_every_format_builds(client, register, covered_story, fmt):
    headers = register("reader")
    resp = client.get(f"/api/story/{covered_story.id}/export?format={fmt}",
                      headers=headers)
    assert resp.status_code == 200
    assert len(resp.content) > 0


@pytest.mark.parametrize("fmt", ["xlsx", "docx"])
def test_the_office_formats_are_real_files(client, register, covered_story, fmt):
    """They are ZIPs of XML; a plausible-looking blob is not enough."""
    headers = register("reader")
    body = client.get(f"/api/story/{covered_story.id}/export?format={fmt}",
                      headers=headers).content

    with zipfile.ZipFile(BytesIO(body)) as z:
        assert z.testzip() is None
        names = z.namelist()
    assert "[Content_Types].xml" in names


def test_an_unknown_format_is_refused(client, register, covered_story):
    headers = register("reader")
    resp = client.get(f"/api/story/{covered_story.id}/export?format=pdf", headers=headers)
    assert resp.status_code == 422


def test_a_story_that_does_not_exist_is_a_404(client, register):
    headers = register("reader")
    assert client.get("/api/story/999999/export?format=csv",
                      headers=headers).status_code == 404


def test_it_needs_a_session(client, covered_story):
    assert client.get(f"/api/story/{covered_story.id}/export?format=csv").status_code == 401


def test_it_spends_the_export_budget(client, register, covered_story, monkeypatch):
    """Same rate limit as the other exports — it builds files the same way."""
    from backend.app import ratelimit
    headers = register("reader")
    monkeypatch.setattr(ratelimit, "ENABLED", True)
    monkeypatch.setattr(ratelimit, "TRUSTED_PROXIES", 0)
    ratelimit._hits.clear()

    allowed = 0
    for _ in range(ratelimit._LIMITS["export"][0] + 3):
        if client.get(f"/api/story/{covered_story.id}/export?format=csv",
                      headers=headers).status_code == 429:
            break
        allowed += 1
    ratelimit._hits.clear()
    assert allowed == ratelimit._LIMITS["export"][0]
