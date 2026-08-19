"""Athena — what a Pantheon has actually covered.

A group writing weekly intelligence reports accumulates a question its own
reports cannot answer: *what have we been covering, and how often?* Twenty
documents hold that answer between them and none of them state it. Athena is
where the group files what it wrote, tagged against a vocabulary of its own, so
the pattern over months becomes something to look at.

Four things this file exists to hold, and they are the four that would hurt:

  · **the documents are never stored.** A .docx is opened, read and discarded
    in the member's own browser. These are a group's intelligence products;
    Delphi has no business holding the originals and the volume has no business
    carrying them.
  · **what arrives is client-supplied JSON and is treated as such.** It came
    from a parser we wrote, which is not the same as coming from us.
  · **a theme the Pantheon does not have is never invented by an upload.** The
    moment one careless file can add vocabulary, every coverage figure on the
    board stops meaning anything.
  · **nothing is left behind.** Not when a document goes, not when a theme
    goes, and not when the Pantheon itself ends.
"""
import pathlib

import pytest
from sqlalchemy import select

from backend.app import athena
from backend.app.models import (AthenaDocument, AthenaDomain, AthenaEntry,
                                AthenaTheme, Pantheon, PantheonMember)

FRONTEND = pathlib.Path(__file__).resolve().parent.parent / "frontend"
HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
APP = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
ATH = (FRONTEND / "js" / "athena.js").read_text(encoding="utf-8")
CSS = (FRONTEND / "css" / "styles.css").read_text(encoding="utf-8")


def _fn(src: str, name: str, end: str = "\n}\n") -> str:
    start = src.index(name)
    return src[start:src.find(end, start)]


@pytest.fixture()
def group(client, register):
    """A Pantheon with an owner, and a second account that is not a member."""
    owner = register("athenaowner")
    pid = client.post("/api/pantheons", headers=owner,
                      json={"name": "Watch Desk"}).json()["id"]
    return {"pid": pid, "owner": owner}


def _theme(client, group, name, domain="", keywords=None):
    r = client.post(f"/api/pantheons/{group['pid']}/athena/themes",
                    headers=group["owner"],
                    json={"name": name, "domain": domain,
                          "keywords": keywords or []})
    assert r.status_code == 201, r.text
    return r.json()["slug"]


def _file(client, group, **kw):
    body = {"kind": "report", "date": "2026-03-02", "label": "Week 9",
            "filename": "week9.docx",
            "entries": [{"title": "A topic", "body": "about something",
                         "themes": [], "links": []}]}
    body.update(kw)
    return client.post(f"/api/pantheons/{group['pid']}/athena/documents",
                       headers=group["owner"], json=body)


# ---------- slugs ----------

def test_a_theme_named_twice_is_two_themes_not_a_collision(db):
    """Renaming into a name already taken is a mistake worth surviving. An
    error dialog mid-thought loses whatever the group was doing."""
    assert athena.slugify("Supply chain & tariffs") == "supply-chain-tariffs"


def test_accents_fold_rather_than_disappear():
    """"Réputation" and "Reputation" as two rows would look identical in the
    list and split the group's own coverage figures between them."""
    assert athena.slugify("Réputation") == athena.slugify("Reputation")


def test_a_nameless_theme_still_gets_a_usable_slug():
    assert athena.slugify("") == "untitled"
    assert athena.slugify("!!!") == "untitled"


# ---------- what a browser is allowed to file ----------

def test_only_http_links_survive():
    """Word writes file:// hrefs quite innocently when somebody links a network
    share, and neither that nor javascript: belongs in a list this board
    renders for every member of the group."""
    kept = athena.clean_links([
        {"t": "ok", "u": "https://example.com/a"},
        {"t": "share", "u": "file:///mnt/secret"},
        {"t": "bad", "u": "javascript:alert(1)"},
        {"t": "plain", "u": "http://example.org"},
    ])
    assert [l["u"] for l in kept] == ["https://example.com/a", "http://example.org"]


def test_a_link_with_no_title_falls_back_to_its_url():
    assert athena.clean_links([{"u": "https://x.test/a"}])[0]["t"] == "https://x.test/a"


def test_an_upload_cannot_invent_a_theme():
    """The load-bearing rule. The moment one careless file can add vocabulary,
    every coverage figure on the board stops meaning anything."""
    entries = athena.clean_entries(
        [{"title": "T", "themes": ["known", "smuggled-in"]}], {"known"})
    assert entries[0]["themes"] == ["known"]


def test_a_theme_listed_twice_counts_once():
    entries = athena.clean_entries([{"title": "T", "themes": ["a", "a", "a"]}], {"a"})
    assert entries[0]["themes"] == ["a"]


def test_an_entry_with_nothing_in_it_is_not_filed():
    assert athena.clean_entries([{"title": "  ", "body": ""}], set()) == []


@pytest.mark.parametrize("field,cap", [("title", athena.MAX_TITLE),
                                       ("body", athena.MAX_BODY)])
def test_long_fields_are_bounded(field, cap):
    entries = athena.clean_entries([{field: "x" * (cap + 500), "title": "t"}], set())
    assert len(entries[0][field]) <= cap


def test_the_number_of_entries_and_links_is_bounded():
    raw = [{"title": f"t{i}"} for i in range(athena.MAX_ENTRIES + 50)]
    assert len(athena.clean_entries(raw, set())) == athena.MAX_ENTRIES
    links = [{"u": f"https://x.test/{i}"} for i in range(athena.MAX_LINKS + 20)]
    assert len(athena.clean_links(links)) == athena.MAX_LINKS


def test_rubbish_in_place_of_a_list_is_survived():
    assert athena.clean_entries("not a list", set()) == []
    assert athena.clean_entries([None, 3, "x"], set()) == []
    assert athena.clean_links({"u": "https://x"}) == []


# ---------- the endpoints ----------

def test_a_member_can_file_and_read_back(client, group):
    slug = _theme(client, group, "Supply chain", domain="Operations",
                  keywords=["tariff"])
    r = _file(client, group, entries=[
        {"title": "Tariffs bite", "body": "…", "themes": [slug],
         "links": [{"t": "FT", "u": "https://ft.example/1"}]}])
    assert r.status_code == 201, r.text
    board = client.get(f"/api/pantheons/{group['pid']}/athena",
                       headers=group["owner"]).json()
    assert len(board["documents"]) == 1
    assert board["documents"][0]["entries"][0]["themes"] == [slug]
    assert board["themes"][0]["name"] == "Supply chain"
    assert board["domains"][0]["name"] == "Operations"


def test_a_domain_appears_the_first_time_a_theme_is_put_in_one(client, group):
    """Managing domains as their own list would be a screen of setup before the
    group can file anything, and the only thing a domain carries beyond a name
    is a colour — which has a sensible default."""
    _theme(client, group, "Cyber", domain="Security threats")
    _theme(client, group, "Extremism", domain="Security threats")
    board = client.get(f"/api/pantheons/{group['pid']}/athena",
                       headers=group["owner"]).json()
    assert len(board["domains"]) == 1
    assert board["domains"][0]["color"].startswith("#")


def test_a_document_with_no_usable_entries_is_refused(client, group):
    r = _file(client, group, entries=[])
    assert r.status_code == 422
    assert "nothing in that document" in r.json()["detail"].lower()


@pytest.mark.parametrize("bad", ["2026-3-2", "March 2nd", "", "20260302"])
def test_a_document_needs_a_real_date(client, group, bad):
    """A weekly report is filed by the week it covers, and a bad date puts a
    whole column of the grid in the wrong place."""
    assert _file(client, group, date=bad).status_code == 422


def test_a_document_is_a_report_or_notes_and_nothing_else(client, group):
    assert _file(client, group, kind="memo").status_code == 422


def test_a_non_member_cannot_see_or_file(client, group, register):
    """A Pantheon's filed intelligence is its own. Membership is the whole of
    the access rule and it is checked before anything else."""
    stranger = register("athenastranger")
    pid = group["pid"]
    assert client.get(f"/api/pantheons/{pid}/athena", headers=stranger).status_code == 403
    assert client.post(f"/api/pantheons/{pid}/athena/documents", headers=stranger,
                       json={"kind": "report", "date": "2026-03-02",
                             "entries": [{"title": "x"}]}).status_code == 403


def test_only_an_admin_may_change_the_vocabulary(client, register):
    """A theme is what the group's figures are counted in, and renaming one
    silently changes every number on the board — so a plain member files
    against the vocabulary but does not get to move it."""
    owner = register("vocabowner")
    pid = client.post("/api/pantheons", headers=owner,
                      json={"name": "Open Desk", "visibility": "public"}).json()["id"]
    member = register("vocabmember")
    assert client.post(f"/api/pantheons/{pid}/join", headers=member).status_code == 200

    # A member reads the board and may file against it.
    assert client.get(f"/api/pantheons/{pid}/athena", headers=member).status_code == 200
    assert client.post(f"/api/pantheons/{pid}/athena/documents", headers=member,
                       json={"kind": "report", "date": "2026-03-02",
                             "entries": [{"title": "Something"}]}).status_code == 201
    # But not change what the figures are counted in.
    assert client.post(f"/api/pantheons/{pid}/athena/themes", headers=member,
                       json={"name": "Nope"}).status_code == 403


def test_filing_is_attributed(client, group):
    _file(client, group)
    board = client.get(f"/api/pantheons/{group['pid']}/athena",
                       headers=group["owner"]).json()
    assert board["documents"][0]["uploaded_by"] == "athenaowner"


def test_the_board_says_whether_you_may_manage_it(client, group):
    board = client.get(f"/api/pantheons/{group['pid']}/athena",
                       headers=group["owner"]).json()
    assert board["can_manage"] is True


# ---------- nothing is left behind ----------

def test_removing_a_document_takes_its_entries(db, client, group):
    """SQLite is not enforcing the foreign key that would do this for us, so
    the entries would sit in the table for as long as the instance runs,
    counted by nothing and reachable by nothing."""
    _file(client, group)
    doc = db.scalars(select(AthenaDocument)).first()
    assert db.scalar(select(AthenaEntry).where(AthenaEntry.document_id == doc.id))
    client.delete(f"/api/pantheons/{group['pid']}/athena/documents/{doc.id}",
                  headers=group["owner"])
    db.expire_all()
    assert db.query(AthenaEntry).count() == 0
    assert db.query(AthenaDocument).count() == 0


def test_deleting_a_theme_strips_it_from_every_entry(db, client, group):
    """A JSON list of slugs buys a great deal and owes exactly this. A slug
    left behind after its theme is gone renders as nothing and counts towards
    no column — a tag that is invisible and still there."""
    slug = _theme(client, group, "Cyber")
    other = _theme(client, group, "Policy")
    _file(client, group, entries=[{"title": "Both", "themes": [slug, other]}])
    r = client.delete(f"/api/pantheons/{group['pid']}/athena/themes/{slug}",
                      headers=group["owner"])
    assert r.status_code == 200
    assert r.json()["entries_untagged"] == 1
    db.expire_all()
    entry = db.scalars(select(AthenaEntry)).first()
    assert entry.themes == [other]


def test_closing_a_pantheon_purges_everything_athena_holds(db, client, group):
    _theme(client, group, "Cyber", domain="Security")
    _file(client, group)
    client.delete(f"/api/pantheons/{group['pid']}", headers=group["owner"])
    db.expire_all()
    for model in (AthenaDocument, AthenaEntry, AthenaTheme, AthenaDomain):
        assert db.query(model).count() == 0, f"{model.__name__} rows survived"


def test_both_endings_use_the_same_purge():
    """The same class of bug has been fixed on one deletion path and left on
    another twice in this codebase. A second copy of these four deletes is a
    second place to forget one."""
    import inspect
    from backend.app import main
    assert "athena.purge_pantheon" in inspect.getsource(main._close_pantheon)
    assert "athena.purge_pantheon" in inspect.getsource(main.delete_pantheon)


# ---------- the board ----------

def test_athena_is_a_board_not_a_panel():
    """The coverage grid is themes down one side and weeks across the other. At
    a year of reporting it is wider than any dialog, and it is the whole point
    of the feature."""
    assert 'id="athena-view"' in HTML
    assert 'el("athena-view").hidden = !athena;' in APP
    assert ".athena-view" in CSS


def test_leaving_the_board_lets_go_of_its_data():
    assert "if (!athena) AthenaBoard.close();" in APP


def test_it_opens_from_the_pantheon_it_belongs_to():
    src = _fn(APP, "function renderBoardHeader")
    assert "athena:" in src
    assert "🦉 Athena" in src


def test_the_pantheons_tab_stays_lit_inside_it():
    src = _fn(APP, "function updateViewButtons")
    assert 'VIEW.startsWith("athena:")' in src


def test_the_grid_scrolls_sideways_inside_its_own_box():
    """A year of columns must not push the page about."""
    assert ".ath-scroll" in CSS
    block = CSS[CSS.index(".ath-scroll"):]
    assert "overflow-x: auto" in block[:120]


def test_theme_names_stay_put_while_the_grid_scrolls():
    """A theme name is useless once it has scrolled off the left, and reading
    along a row is the whole point."""
    block = CSS[CSS.index(".ath-corner, .ath-rowhead"):]
    assert "position: sticky" in block[:200]


def test_the_explainer_is_shown_once_and_says_what_the_board_is_for():
    """The board starts genuinely empty — no taxonomy ships with it, because a
    default would be one group's vocabulary imposed on every other. So it has
    to explain itself instead."""
    assert 'id="athena-intro-backdrop"' in HTML
    start = HTML.index('id="athena-intro-backdrop"')
    intro = HTML[start:HTML.index('id="pn-backdrop"', start)]
    assert "never leave your browser" in intro
    assert "starts empty" in intro
    assert "confirm every one" in intro
    assert "athSeen(" in ATH, "shown once per Pantheon, not on every open"


def test_the_explainer_can_be_reopened():
    """Once is right for something nobody asked for, and never again is wrong
    for the only page that explains the feature."""
    assert 'id="btn-athena-help"' in HTML
    assert 'el("btn-athena-help").onclick' in ATH


# ---------- the parser, and what it refuses to do ----------

def test_the_document_never_leaves_the_browser():
    """Stated in the code because it is the reason the parser is here at all
    rather than on the server, where it would have been less work."""
    assert "never leaves the browser" in ATH
    assert "DecompressionStream" in ATH
    # Nothing in the read path uploads: the only API call is in file(), after
    # a person has confirmed the review.
    read = _fn(ATH, "  async read(file)", "\n  },\n")
    assert "API." not in read


def test_only_confirmed_entries_are_sent():
    src = _fn(ATH, "  async file()", "\n  },\n")
    assert "filter((e) => e.keep)" in src


def test_themes_are_suggested_and_never_applied_on_their_own():
    above = ATH[:ATH.index("function athSuggestThemes")]
    assert "A suggestion and nothing more" in above
    review = _fn(ATH, "  renderReview()", "\n  },\n")
    assert "ath-chip-btn" in review, "every suggested theme is a toggle a person presses"


def test_the_review_shows_what_it_would_drop_rather_than_hiding_it():
    assert ".ath-dropped" in CSS
    assert "ath-dropped" in ATH


def test_third_party_text_never_reaches_innerhtml():
    """Every string on this board came out of somebody's Word document."""
    for name in ("function athEntryCard", "  renderThemes()", "  renderSources(v)"):
        src = _fn(ATH, name, "\n  },\n" if name.startswith("  ") else "\n}\n")
        assert "innerHTML" not in src, f"{name} builds markup from a string"


def test_links_are_run_through_the_same_guard_as_every_other_url():
    assert ATH.count("safeUrl(") >= 2


def test_a_browser_that_cannot_unzip_says_so_rather_than_failing_oddly():
    src = _fn(ATH, "  async read(file)", "\n  },\n")
    assert "cannot unpack" in src


def test_markdown_and_plain_text_reach_the_same_shape():
    """So everything downstream has one input to understand."""
    assert "function athTextToParas" in ATH
    src = _fn(ATH, "  async read(file)", "\n  },\n")
    assert "athTextToParas" in src and "athExtractDocx" in src
