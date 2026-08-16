"""The Pantheons board should not cost more the more you are in.

Both endpoints behind the board were shaped per-row. `_pantheon_json` ran three
counting queries and an owner lookup for every Pantheon, and `list_pantheons`
called it in a loop after a `db.get` of its own — five statements a tile, plus
three per pending invitation. The public directory did the same, one count per
Pantheon, for up to 200 of them.

Nothing was slow: this is SQLite in the same process, and each of those is tens
of microseconds. It is the shape that is wrong — the page that shows the most
is the one that pays the most, and it is the page a new account lands on when
somebody invites them to five Pantheons at once.

These tests count statements rather than time, because time on an empty test
database would say everything is fine at any size. The rule they pin is not a
number but a slope: the cost of the board does not move when Pantheons are
added to it.
"""
import pytest
from sqlalchemy import event

from backend.app.database import engine


@pytest.fixture
def statements():
    """Counts SQL statements issued while the block runs."""
    seen = []

    def record(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


def _pantheon(client, headers, name, visibility="private"):
    return client.post("/api/pantheons", headers=headers,
                       json={"name": name, "visibility": visibility}).json()["id"]


def test_the_board_costs_the_same_at_one_pantheon_and_at_eight(
        client, register, statements):
    owner = register("costowner")
    _pantheon(client, owner, "First")

    statements.clear()
    client.get("/api/pantheons", headers=owner)
    one = len(statements)

    for n in range(2, 9):
        _pantheon(client, owner, f"Number {n}")

    statements.clear()
    client.get("/api/pantheons", headers=owner)
    eight = len(statements)

    assert eight == one, (
        f"{one} statements for one Pantheon, {eight} for eight — still per-row")


def test_the_public_directory_costs_the_same_at_one_and_at_eight(
        client, register, statements):
    owner = register("costowner")
    reader = register("costreader")
    _pantheon(client, owner, "First", "public")

    statements.clear()
    client.get("/api/pantheons/public", headers=reader)
    one = len(statements)

    for n in range(2, 9):
        _pantheon(client, owner, f"Number {n}", "public")

    statements.clear()
    client.get("/api/pantheons/public", headers=reader)
    eight = len(statements)

    assert eight == one, (
        f"{one} statements for one Pantheon, {eight} for eight — still per-row")


def test_pending_invitations_do_not_add_a_query_each(client, register, statements):
    """The other loop in the same endpoint: an invitation looked up its
    Pantheon, its inviter and a member count of its own."""
    owner = register("costowner")
    guest = register("costguest")
    for n in range(1, 6):
        pid = _pantheon(client, owner, f"Number {n}")
        client.post(f"/api/pantheons/{pid}/invite", headers=owner,
                    json={"user": "costguest"})

    statements.clear()
    body = client.get("/api/pantheons", headers=guest).json()
    with_five = len(statements)

    assert len(body["invites"]) == 5
    assert with_five <= 12, (
        f"{with_five} statements to list five invitations")


# ---------- and it still says the same things ----------

def test_the_board_reports_what_it_did_before(client, register):
    """The counts moved into one grouped query; they still have to be right,
    and a Pantheon with nothing in it still has to report zero rather than be
    missing from the result."""
    owner = register("countowner")
    mate = register("countmate")
    busy = _pantheon(client, owner, "Busy")
    empty = _pantheon(client, owner, "Empty")

    client.post(f"/api/pantheons/{busy}/invite", headers=owner, json={"user": "countmate"})
    inv = client.get("/api/pantheons", headers=mate).json()["invites"]
    client.post(f"/api/pantheons/invites/{inv[0]['id']}/accept", headers=mate)
    fid = client.post("/api/feeds", headers=owner, json={
        "name": "Energy", "criteria": {"keywords": ["energy"], "auto_coverage": False},
    }).json()["id"]
    client.post(f"/api/feeds/{fid}/share", headers=owner, json={"pantheon_id": busy})
    aid = client.post("/api/alerts", headers=owner, json={
        "name": "Quake", "criteria": {"keywords": ["earthquake"]},
    }).json()["id"]
    client.post(f"/api/alerts/{aid}/share", headers=owner, json={"pantheon_id": busy})

    mine = {p["name"]: p for p in client.get("/api/pantheons", headers=owner).json()["mine"]}
    assert mine["Busy"]["member_count"] == 2
    assert mine["Busy"]["feed_count"] == 1
    assert mine["Busy"]["alert_count"] == 1
    assert mine["Busy"]["owner_name"] == "countowner"
    assert mine["Busy"]["role"] == "owner"
    assert (mine["Empty"]["member_count"], mine["Empty"]["feed_count"],
            mine["Empty"]["alert_count"]) == (1, 0, 0)


def test_an_invitation_still_names_who_sent_it(client, register):
    owner = register("countowner")
    guest = register("countguest")
    pid = _pantheon(client, owner, "Newsroom")
    client.post(f"/api/pantheons/{pid}/invite", headers=owner, json={"user": "countguest"})

    invites = client.get("/api/pantheons", headers=guest).json()["invites"]
    assert [(i["name"], i["invited_by"], i["member_count"])
            for i in invites] == [("Newsroom", "countowner", 1)]


def test_the_public_directory_still_withholds_what_it_withheld(client, register):
    """Non-members see enough to decide to join and no more: no owner name, no
    feed or alert counts. Batching the counts must not have widened that."""
    owner = register("countowner")
    reader = register("countreader")
    _pantheon(client, owner, "Open house", "public")

    listed = client.get("/api/pantheons/public", headers=reader).json()
    assert len(listed) == 1
    assert set(listed[0]) == {"id", "name", "description", "visibility",
                              "member_count", "joined"}
    assert listed[0]["member_count"] == 1
    assert listed[0]["joined"] is False
