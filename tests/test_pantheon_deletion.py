"""Deleting a Pantheon has to leave nothing behind.

Reported as "deletion isn't actually deleting the Pantheon, it comes back on
every load". Driving the real flow — API and then a browser — showed the
Pantheon itself does go, populated or empty. What came back on every load was a
*shared location*: sharing a pin into a Pantheon makes a copy of it, and
deletion cleaned up shared feeds, shared alerts, members and invitations while
leaving that copy in the database. It stayed on the sharer's list, badged as
shared with a Pantheon that no longer existed, and no amount of deleting could
remove it because the thing it belonged to was already gone.

So: this file deletes a Pantheon that is fully in use and asserts nothing
survives it, and it covers the two things that were wrong around the edges —
a delete that reports success while the row is still there, and the shared copy
losing the place name it matches on.
"""
from backend.app.models import FavoriteLocation, Pantheon, PantheonInvite, PantheonMember


def _pantheon_in_use(client, owner, mate, register):
    """A Pantheon with two members, a pending invite, a shared feed, a shared
    alert and a shared location — one of each thing deletion has to unpick."""
    pid = client.post("/api/pantheons", headers=owner,
                      json={"name": "Newsroom", "visibility": "public"}).json()["id"]
    client.post(f"/api/pantheons/{pid}/invite", headers=owner, json={"user": "delmate"})
    inv = client.get("/api/pantheons", headers=mate).json()["invites"]
    client.post(f"/api/pantheons/invites/{inv[0]['id']}/accept", headers=mate)
    client.post(f"/api/pantheons/{pid}/invite", headers=owner, json={"user": "delthird"})

    fid = client.post("/api/feeds", headers=owner, json={
        "name": "Energy", "criteria": {"keywords": ["energy"], "auto_coverage": False},
    }).json()["id"]
    client.post(f"/api/feeds/{fid}/share", headers=owner, json={"pantheon_id": pid})
    aid = client.post("/api/alerts", headers=owner, json={
        "name": "Quake", "criteria": {"keywords": ["earthquake"]},
    }).json()["id"]
    client.post(f"/api/alerts/{aid}/share", headers=owner, json={"pantheon_id": pid})
    lid = client.post("/api/locations", headers=owner, json={
        "name": "Office", "place_name": "Reading", "country": "GB",
        "lat": 51.45, "lon": -0.97, "radius_km": 25,
    }).json()["id"]
    client.post(f"/api/locations/{lid}/share", headers=owner, json={"pantheon_id": pid})
    return pid


def test_a_pantheon_in_use_deletes_completely(client, register, db):
    owner = register("delowner")
    mate = register("delmate")
    register("delthird")
    pid = _pantheon_in_use(client, owner, mate, register)

    assert client.delete(f"/api/pantheons/{pid}", headers=owner).status_code == 204

    assert db.get(Pantheon, pid) is None
    for model in (PantheonMember, PantheonInvite):
        assert db.query(model).filter(model.pantheon_id == pid).count() == 0, model
    assert db.query(FavoriteLocation).filter(
        FavoriteLocation.pantheon_id == pid).count() == 0, (
        "a location shared into the Pantheon outlived it")
    assert client.get("/api/pantheons", headers=owner).json()["mine"] == []
    assert client.get("/api/pantheons", headers=mate).json()["mine"] == []


def test_the_sharers_own_list_is_clean_afterwards(client, register, db):
    """What the reader actually saw: two "Office" pins, one of them shared with
    nothing, on every load, forever."""
    owner = register("delowner")
    mate = register("delmate")
    register("delthird")
    pid = _pantheon_in_use(client, owner, mate, register)
    client.delete(f"/api/pantheons/{pid}", headers=owner)

    locs = client.get("/api/locations", headers=owner).json()
    assert [(l["name"], l.get("pantheon_id")) for l in locs] == [("Office", None)]


def test_the_personal_pin_survives_its_shared_copy(client, register, db):
    """Deleting the Pantheon must not take the original with it — the copy is
    the Pantheon's, the pin is the reader's."""
    owner = register("delowner")
    mate = register("delmate")
    register("delthird")
    pid = _pantheon_in_use(client, owner, mate, register)
    before = client.get("/api/locations", headers=owner).json()
    original = next(l for l in before if not l.get("pantheon_id"))

    client.delete(f"/api/pantheons/{pid}", headers=owner)
    after = client.get("/api/locations", headers=owner).json()
    assert [l["id"] for l in after] == [original["id"]]
    assert after[0]["place_name"] == "Reading"


def test_a_shared_location_keeps_the_name_it_matches_on(client, register):
    """A watched place is matched by name as well as by coordinates, and only
    articles the gazetteer recognised have coordinates at all. The copy used to
    be created without place_name or country, so the same pin found much less
    news on the Pantheon's board than on the sharer's own."""
    owner = register("delowner")
    pid = client.post("/api/pantheons", headers=owner,
                      json={"name": "Newsroom"}).json()["id"]
    lid = client.post("/api/locations", headers=owner, json={
        "name": "Office", "place_name": "Reading", "country": "GB",
        "lat": 51.45, "lon": -0.97, "radius_km": 25,
    }).json()["id"]
    copy = client.post(f"/api/locations/{lid}/share", headers=owner,
                       json={"pantheon_id": pid}).json()
    assert copy["place_name"] == "Reading"
    assert copy["country"] == "GB"


def test_only_the_owner_can_delete(client, register):
    owner = register("delowner")
    mate = register("delmate")
    pid = client.post("/api/pantheons", headers=owner,
                      json={"name": "Newsroom"}).json()["id"]
    client.post(f"/api/pantheons/{pid}/invite", headers=owner, json={"user": "delmate"})
    inv = client.get("/api/pantheons", headers=mate).json()["invites"]
    client.post(f"/api/pantheons/invites/{inv[0]['id']}/accept", headers=mate)
    assert client.delete(f"/api/pantheons/{pid}", headers=mate).status_code == 403
    assert client.get("/api/pantheons", headers=owner).json()["mine"][0]["id"] == pid


def test_deleting_it_twice_is_not_a_success_the_second_time(client, register):
    """The client's own check for "did that work" is the refreshed list, and a
    second delete answering 204 would make a stale card look deletable."""
    owner = register("delowner")
    pid = client.post("/api/pantheons", headers=owner,
                      json={"name": "Newsroom"}).json()["id"]
    assert client.delete(f"/api/pantheons/{pid}", headers=owner).status_code == 204
    assert client.delete(f"/api/pantheons/{pid}", headers=owner).status_code == 404


# ---------- the client says what happened ----------

def _app_js() -> str:
    import pathlib
    return (pathlib.Path(__file__).resolve().parent.parent / "frontend" / "js"
            / "app.js").read_text(encoding="utf-8")


def test_the_delete_button_does_not_depend_on_a_loaded_detail():
    """It read the Pantheon's name out of the detail fetch and returned early
    when that had not arrived — no request, no error, nothing to see. The id is
    known the moment the modal opens."""
    src = _app_js()
    handler = src[src.index('el("btn-pn-delete").onclick'):]
    handler = handler[:handler.index('el("btn-pn-leave").onclick')]
    assert "PantheonModal.id" in handler
    assert "API.deletePantheon(id)" in handler


def test_the_client_verifies_the_deletion_before_saying_it_worked():
    src = _app_js()
    handler = src[src.index('el("btn-pn-delete").onclick'):]
    handler = handler[:handler.index('el("btn-pn-leave").onclick')]
    assert "await refreshPantheons()" in handler
    assert "PANTHEONS.some(p => p.id === id)" in handler, (
        "closing the modal is the only thing that used to mark success")
    assert handler.index("refreshPantheons") < handler.index("PantheonModal.close()")


def test_the_server_checks_the_row_is_gone():
    import inspect

    from backend.app import main
    src = inspect.getsource(main.delete_pantheon)
    assert "db.get(Pantheon, pantheon_id) is not None" in src
    assert "FavoriteLocation" in src
