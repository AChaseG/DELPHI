"""Every way a Pantheon or an account ends has to clean up after itself.

`delete_pantheon` was fixed once already, for a shared location that outlived
the Pantheon it was shared with and came back on the sharer's map at every
load (see test_pantheon_deletion.py). An audit pass found the same rows being
left behind on the other two paths out, neither of which goes through that
endpoint:

    _close_pantheon   the last member walks out, so the Pantheon closes itself
    _delete_user      an operator deletes an account

and `_delete_user` was additionally leaving that account's own saved locations,
its read history, and its device registrations in the database — none of them
reachable afterwards, so nothing looked wrong, which is exactly why it went
unnoticed. SQLite is not enforcing the foreign keys here (see database.py), so
the devices row survived a dangling reference in silence rather than erroring.

The saved locations are the one that matters beyond tidiness. A location keeps
a "Local: …" news source pointed at it, and a source nothing points at is still
polled every few minutes for as long as the server runs; and the nightly
accounts backup copies every FavoriteLocation row, so a deleted account's list
of where it lives would ride along in the operator's inbox indefinitely.
"""
from backend.app.models import (Device, FavoriteLocation, Pantheon,
                                PantheonMember, Source, User, ViewedArticle)


def _place(client, headers, name, place, lat, lon):
    return client.post("/api/locations", headers=headers, json={
        "name": name, "place_name": place, "country": "GB",
        "lat": lat, "lon": lon, "radius_km": 25,
    }).json()["id"]


def _join(client, owner, mate, pid, username):
    client.post(f"/api/pantheons/{pid}/invite", headers=owner, json={"user": username})
    inv = client.get("/api/pantheons", headers=mate).json()["invites"]
    client.post(f"/api/pantheons/invites/{inv[0]['id']}/accept", headers=mate)


# ---------- the Pantheon that closes because nobody is left ----------

def test_the_last_member_leaving_takes_the_shared_locations_with_them(
        client, register, db):
    """The owner's account is deleted, there is no heir, so the Pantheon closes.
    The shared copy has to go with it — otherwise the sharer is left with a pin
    badged against a Pantheon that no longer exists, which is the reported bug
    arriving by a different door."""
    owner = register("closeowner")
    pid = client.post("/api/pantheons", headers=owner,
                      json={"name": "Newsroom"}).json()["id"]
    lid = _place(client, owner, "Office", "Reading", 51.45, -0.97)
    client.post(f"/api/locations/{lid}/share", headers=owner, json={"pantheon_id": pid})
    assert db.query(FavoriteLocation).filter(
        FavoriteLocation.pantheon_id == pid).count() == 1

    from backend.app.main import _delete_user
    _delete_user(db, db.query(User).filter(User.username == "closeowner").one())
    db.commit()

    assert db.get(Pantheon, pid) is None
    assert db.query(FavoriteLocation).filter(
        FavoriteLocation.pantheon_id == pid).count() == 0
    assert db.query(PantheonMember).filter(
        PantheonMember.pantheon_id == pid).count() == 0


def test_a_pantheon_with_an_heir_keeps_its_shared_locations(client, register, db):
    """The other half of the rule: closing is what removes them, not leaving.
    A Pantheon that survives its owner keeps everything shared into it."""
    owner = register("heirowner")
    mate = register("heirmate")
    pid = client.post("/api/pantheons", headers=owner,
                      json={"name": "Newsroom"}).json()["id"]
    _join(client, owner, mate, pid, "heirmate")
    lid = _place(client, owner, "Office", "Reading", 51.45, -0.97)
    client.post(f"/api/locations/{lid}/share", headers=owner, json={"pantheon_id": pid})

    from backend.app.main import _delete_user
    _delete_user(db, db.query(User).filter(User.username == "heirowner").one())
    db.commit()

    assert db.get(Pantheon, pid) is not None, "the group outlives one member"
    shared = db.query(FavoriteLocation).filter(
        FavoriteLocation.pantheon_id == pid).all()
    assert len(shared) == 1
    # Handed to the heir, the way an inherited feed or alert is — it carries the
    # sharer's user_id, so without that it would be swept up with their personal
    # pins and the group would lose a watched place.
    heir = db.query(User).filter(User.username == "heirmate").one()
    assert shared[0].user_id == f"acct:{heir.id}"
    assert shared[0].shared_by == "heirowner", "who put it there is still true"


# ---------- the account that is deleted ----------

def test_a_deleted_account_leaves_no_saved_locations(client, register, db):
    owner = register("goneuser")
    _place(client, owner, "Home", "Reading", 51.45, -0.97)
    _place(client, owner, "Work", "Bristol", 51.45, -2.58)
    user = db.query(User).filter(User.username == "goneuser").one()
    acct = f"acct:{user.id}"
    assert db.query(FavoriteLocation).filter(
        FavoriteLocation.user_id == acct).count() == 2

    from backend.app.main import _delete_user
    _delete_user(db, user)
    db.commit()

    assert db.query(FavoriteLocation).filter(
        FavoriteLocation.user_id == acct).count() == 0


def test_the_news_source_a_deleted_location_was_watching_stops_being_polled(
        client, register, db):
    """Each saved place gets a source of its own, and a source nothing points at
    is polled forever. Deleting one pin already cleaned that up; deleting the
    whole account did not."""
    owner = register("srcuser")
    _place(client, owner, "Home", "Reading", 51.45, -0.97)
    user = db.query(User).filter(User.username == "srcuser").one()
    loc = db.query(FavoriteLocation).filter(
        FavoriteLocation.user_id == f"acct:{user.id}").one()
    assert loc.source_id, "the location should have minted a source"
    source_id = loc.source_id

    from backend.app.main import _delete_user
    _delete_user(db, user)
    db.commit()

    assert db.get(Source, source_id) is None


def test_a_source_two_people_watch_survives_one_of_them_leaving(
        client, register, db):
    """One source serves every location pointed at the same place. Deleting an
    account must not take it away from whoever else is watching."""
    a = register("shareda")
    b = register("sharedb")
    _place(client, a, "Home", "Reading", 51.45, -0.97)
    _place(client, b, "Office", "Reading", 51.45, -0.97)
    left = db.query(User).filter(User.username == "sharedb").one()
    stays = db.query(User).filter(User.username == "shareda").one()
    kept = db.query(FavoriteLocation).filter(
        FavoriteLocation.user_id == f"acct:{stays.id}").one().source_id
    assert kept

    from backend.app.main import _delete_user
    _delete_user(db, left)
    db.commit()

    assert db.get(Source, kept) is not None


def test_a_deleted_account_leaves_no_read_history(client, register, db):
    owner = register("readuser")
    user = db.query(User).filter(User.username == "readuser").one()
    acct = f"acct:{user.id}"
    db.add(ViewedArticle(user_id=acct, article_id=1))
    db.commit()

    from backend.app.main import _delete_user
    _delete_user(db, user)
    db.commit()

    assert db.query(ViewedArticle).filter(ViewedArticle.user_id == acct).count() == 0


def test_a_deleted_account_leaves_no_devices(client, register, db):
    """This one is a foreign key to users.id, and SQLite is not enforcing them
    here — so the row would have outlived the account it points at without a
    word from the database."""
    owner = register("devuser")
    user = db.query(User).filter(User.username == "devuser").one()
    db.add(Device(user_id=user.id, device_key="k1"))
    db.commit()
    uid = user.id

    from backend.app.main import _delete_user
    _delete_user(db, user)
    db.commit()

    assert db.query(Device).filter(Device.user_id == uid).count() == 0


# ---------- and all three paths agree ----------

def test_every_way_a_pantheon_ends_clears_its_locations():
    """Three call sites, one helper. Written down because the bug was fixed on
    one of them and stayed on the other two for a month."""
    import inspect

    from backend.app import main
    for fn in (main.delete_pantheon, main._close_pantheon, main._delete_user):
        assert "_drop_locations" in inspect.getsource(fn), fn.__name__
