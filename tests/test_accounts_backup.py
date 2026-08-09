"""The few megabytes that losing the volume would actually cost.

Automatic snapshots are off because copying the whole 6.7 GB every night took
the site down for hours. That is only a sane trade because the 6.7 GB is news
and the feeds will hand it back. What they will never hand back is people:
accounts, the columns they built, the alerts they are waiting on, the
organisations they share with, the places they watch, the outlets they added.

A backup nobody has restored from is a rumour, so the centre of this file is a
round trip: fill a database, export it, empty it, restore it, and check that
what comes back is the same — including the references between the parts, which
is where a restore actually fails. Feeds address their owner as "acct:<id>",
pantheon membership is by numeric id, and a watched place points at a feed and
a source. Regenerate those ids and everything still imports, cleanly, wrong.
"""
import json
from datetime import timedelta

import pytest

from backend.app import accounts_backup
from backend.app.accounts_backup import RestoreRefused
from backend.app.models import (Alert, FavoriteLocation, Feed, Pantheon,
                                PantheonInvite, PantheonMember, Source, User,
                                utcnow)


def _populate(db):
    """One of everything, wired together the way the app wires it."""
    owner = User(username="ada", email="ada@example.com",
                 password_hash="hash-ada", is_admin=True, token_version=3,
                 device_limit=2, settings=json.dumps({"lang": "en"}),
                 changelog_seen=json.dumps(["2026-09-01:abc"]))
    guest = User(username="grace", email="grace@example.com",
                 password_hash="hash-grace")
    db.add_all([owner, guest])
    db.commit()

    pantheon = Pantheon(name="Newsroom", description="the desk",
                        owner_id=owner.id, settings={"who_can_invite": "admins"})
    db.add(pantheon)
    db.commit()
    db.add_all([
        PantheonMember(pantheon_id=pantheon.id, user_id=owner.id, role="owner"),
        PantheonMember(pantheon_id=pantheon.id, user_id=guest.id, role="member"),
        PantheonInvite(pantheon_id=pantheon.id, user_id=guest.id,
                       invited_by=owner.id),
    ])

    hand_added = Source(name="Hand added", rss_url="http://hand.example/feed",
                        added_by="user", country="gb")
    place_source = Source(name="Reading news",
                          rss_url="http://news.example/reading",
                          added_by="topic-tracker")
    catalog = Source(name="Catalog wire", rss_url="http://catalog.example/feed",
                     added_by="catalog")
    db.add_all([hand_added, place_source, catalog])
    db.commit()

    feed = Feed(user_id=f"acct:{owner.id}", name="Energy",
                criteria={"query": "solar OR wind"}, position=2, width=2)
    shared = Feed(user_id=f"acct:{owner.id}", pantheon_id=pantheon.id,
                  shared_by="ada", name="Shared column", criteria={})
    db.add_all([feed, shared])
    alert = Alert(user_id=f"acct:{owner.id}", name="Grid failures",
                  criteria={"keywords": ["blackout"]}, notify_email=True,
                  webhook_url="https://hooks.example/secret-path",
                  last_triggered_at=utcnow() - timedelta(hours=3))
    db.add(alert)
    db.commit()

    db.add(FavoriteLocation(
        user_id=f"acct:{owner.id}", name="Dad's house", place_name="Reading",
        country="gb", lat=51.45, lon=-0.97, radius_km=30.0, color="teal",
        feed_id=feed.id, source_id=place_source.id))
    db.commit()
    return {"owner": owner, "guest": guest, "pantheon": pantheon,
            "feed": feed, "alert": alert, "hand_added": hand_added,
            "place_source": place_source, "catalog": catalog}


def _clear(db):
    """Empty the account tables the way a lost volume would."""
    for model in (FavoriteLocation, Alert, Feed, PantheonInvite,
                  PantheonMember, Pantheon, User):
        db.query(model).delete(synchronize_session=False)
    db.commit()
    # A lost volume takes the session's memory of these rows with it; this
    # session would otherwise still hold them and treat a restore of the same
    # ids as a conflict.
    db.expunge_all()


# ---------- what it takes ----------

def test_it_takes_the_things_that_cannot_be_refetched(db):
    _populate(db)
    doc = accounts_backup.build(db)

    assert doc["counts"]["users"] == 2
    assert doc["counts"]["pantheons"] == 1
    assert doc["counts"]["pantheon_members"] == 2
    assert doc["counts"]["pantheon_invites"] == 1
    assert doc["counts"]["feeds"] == 2
    assert doc["counts"]["alerts"] == 1
    assert doc["counts"]["locations"] == 1


def test_it_leaves_the_catalog_out(db):
    """The seeded catalog is in the repository. Copying it into every backup
    would make the file large for no recoverable value."""
    _populate(db)
    urls = {s["rss_url"] for s in accounts_backup.build(db)["sources"]}

    assert "http://hand.example/feed" in urls, "a hand-added source is not replaceable"
    assert "http://news.example/reading" in urls, "a watched place owns this one"
    assert "http://catalog.example/feed" not in urls


def test_it_does_not_take_the_news(db):
    """The whole reason this is small enough to be worth having."""
    _populate(db)
    doc = accounts_backup.build(db)
    assert "articles" not in doc
    assert "events" not in doc
    assert "translations" not in doc


def test_it_does_not_carry_stale_polling_state(db):
    """Restoring an etag or a failure count would have the poller act on what
    it believed about a server weeks ago."""
    _populate(db)
    source = accounts_backup.build(db)["sources"][0]
    for stale in ("etag", "last_modified", "last_fetched_at",
                  "consecutive_failures", "idle_polls", "last_status"):
        assert stale not in source


def test_it_is_json_and_names_itself_by_date(db):
    _populate(db)
    doc = accounts_backup.build(db)
    body = accounts_backup.to_json(doc)

    assert json.loads(body.decode("utf-8"))["format"] == accounts_backup.FORMAT
    assert accounts_backup.filename(doc).startswith("delphi-accounts-")
    assert accounts_backup.filename(doc).endswith(".json")


# ---------- the round trip ----------

def test_a_full_round_trip_restores_the_accounts(db):
    _populate(db)
    doc = json.loads(accounts_backup.to_json(accounts_backup.build(db)))
    _clear(db)
    assert db.query(User).count() == 0

    accounts_backup.restore(db, doc)

    assert db.query(Feed).filter_by(name="Energy").one() is not None
    users = {u.username: u for u in db.query(User).all()}
    assert set(users) == {"ada", "grace"}
    assert users["ada"].password_hash == "hash-ada"
    assert users["ada"].is_admin is True
    assert users["ada"].device_limit == 2
    assert json.loads(users["ada"].settings)["lang"] == "en"


def test_the_owner_of_a_feed_survives_the_trip(db):
    """The reference that would break silently: user_id is the string
    "acct:<id>", so a reassigned id leaves feeds pointing at nobody — or worse,
    at somebody else."""
    made = _populate(db)
    owner_id = made["owner"].id
    doc = json.loads(accounts_backup.to_json(accounts_backup.build(db)))
    _clear(db)

    accounts_backup.restore(db, doc)

    ada = db.query(User).filter_by(username="ada").one()
    assert ada.id == owner_id, "the id a feed names must come back unchanged"
    feed = db.query(Feed).filter_by(name="Energy").one()
    assert feed.user_id == f"acct:{ada.id}"
    assert db.query(Feed).filter_by(name="Energy").one().criteria == {
        "query": "solar OR wind"}


def test_pantheon_membership_survives_the_trip(db):
    _populate(db)
    doc = json.loads(accounts_backup.to_json(accounts_backup.build(db)))
    _clear(db)

    accounts_backup.restore(db, doc)

    pantheon = db.query(Pantheon).one()
    owner = db.query(User).filter_by(username="ada").one()
    guest = db.query(User).filter_by(username="grace").one()
    assert pantheon.owner_id == owner.id
    roles = {m.user_id: m.role for m in db.query(PantheonMember).all()}
    assert roles == {owner.id: "owner", guest.id: "member"}
    invite = db.query(PantheonInvite).one()
    assert (invite.user_id, invite.invited_by) == (guest.id, owner.id)
    assert db.query(Feed).filter_by(name="Shared column").one().pantheon_id == \
        pantheon.id


def test_a_watched_place_still_points_at_its_feed_and_source(db):
    """Both references at once, and the source one is remapped rather than
    preserved because the catalog re-seed takes the low ids."""
    _populate(db)
    doc = json.loads(accounts_backup.to_json(accounts_backup.build(db)))
    _clear(db)
    db.query(Source).delete(synchronize_session=False)
    db.commit()
    # Stand in for the re-seed: the catalog claims ids first.
    for i in range(4):
        db.add(Source(name=f"seeded {i}", rss_url=f"http://seed.example/{i}"))
    db.commit()

    accounts_backup.restore(db, doc)

    loc = db.query(FavoriteLocation).one()
    assert loc.name == "Dad's house"
    assert loc.place_name == "Reading"
    assert loc.radius_km == 30.0
    assert loc.feed_id == db.query(Feed).filter_by(name="Energy").one().id
    assert loc.source_id is not None
    assert db.get(Source, loc.source_id).rss_url == "http://news.example/reading"


def test_a_source_already_present_is_matched_not_duplicated(db):
    """rss_url is unique, so a second insert would fail outright — but the
    quieter failure is two rows for one feed if it ever stopped being."""
    _populate(db)
    doc = json.loads(accounts_backup.to_json(accounts_backup.build(db)))
    _clear(db)

    result = accounts_backup.restore(db, doc)

    assert result["sources_added"] == 0, "both sources were already there"
    assert db.query(Source).filter_by(
        rss_url="http://hand.example/feed").count() == 1


def test_a_missing_source_is_recreated(db):
    _populate(db)
    doc = json.loads(accounts_backup.to_json(accounts_backup.build(db)))
    _clear(db)
    db.query(Source).delete(synchronize_session=False)
    db.commit()

    result = accounts_backup.restore(db, doc)

    assert result["sources_added"] == 2
    hand = db.query(Source).filter_by(rss_url="http://hand.example/feed").one()
    assert hand.added_by == "user"
    assert hand.country == "gb"


def test_a_place_whose_source_is_gone_still_restores(db):
    """The place works off the gazetteer; it simply loses its search feed. A
    restore that refused over this would strand the whole backup."""
    _populate(db)
    doc = json.loads(accounts_backup.to_json(accounts_backup.build(db)))
    doc["locations"][0]["source_url"] = None
    _clear(db)

    accounts_backup.restore(db, doc)
    assert db.query(FavoriteLocation).one().source_id is None


def test_token_version_is_carried_not_reset(db):
    """Lowering it would make tokens that were already refused valid again."""
    _populate(db)
    doc = json.loads(accounts_backup.to_json(accounts_backup.build(db)))
    _clear(db)

    accounts_backup.restore(db, doc)
    assert db.query(User).filter_by(username="ada").one().token_version == 3


# ---------- it refuses rather than half-works ----------

def test_it_refuses_to_restore_over_existing_accounts(db):
    _populate(db)
    doc = json.loads(accounts_backup.to_json(accounts_backup.build(db)))

    with pytest.raises(RestoreRefused) as caught:
        accounts_backup.restore(db, doc)
    assert "already has accounts" in str(caught.value)


def test_a_refused_restore_changes_nothing(db):
    made = _populate(db)
    doc = json.loads(accounts_backup.to_json(accounts_backup.build(db)))
    doc["users"].append({"id": 999, "username": "intruder",
                         "password_hash": "x"})

    with pytest.raises(RestoreRefused):
        accounts_backup.restore(db, doc)

    assert db.query(User).count() == 2
    assert db.query(User).filter_by(username="intruder").count() == 0
    assert db.query(Feed).filter_by(name="Energy").one().id == made["feed"].id


def test_an_unknown_format_is_refused(db):
    """A wrong guess about the shape of a backup is discovered at the worst
    possible moment, so it is checked at the first."""
    with pytest.raises(RestoreRefused) as caught:
        accounts_backup.restore(db, {"format": 99, "users": []})
    assert "format" in str(caught.value)


def test_a_backup_with_no_format_is_refused(db):
    with pytest.raises(RestoreRefused):
        accounts_backup.restore(db, {"users": []})


def test_replace_clears_first_and_reports_what_it_cleared(db):
    _populate(db)
    doc = json.loads(accounts_backup.to_json(accounts_backup.build(db)))
    # A different set of people on this machine now.
    _clear(db)
    db.add(User(username="bootstrap", email="op@example.com",
                password_hash="hash-bootstrap"))
    db.commit()

    result = accounts_backup.restore(db, doc, replace=True)

    assert "users" in result["replaced"]
    assert db.query(User).filter_by(username="bootstrap").count() == 0
    assert {u.username for u in db.query(User).all()} == {"ada", "grace"}


def test_dry_run_reports_the_plan_and_writes_nothing(db):
    _populate(db)
    doc = json.loads(accounts_backup.to_json(accounts_backup.build(db)))
    _clear(db)

    plan = accounts_backup.restore(db, doc, dry_run=True)

    assert plan["dry_run"] is True
    assert plan["users"] == 2
    assert plan["feeds"] == 2
    assert db.query(User).count() == 0, "a dry run wrote something"


def test_occupied_names_the_tables_in_the_way(db):
    assert accounts_backup.occupied(db) == []
    _populate(db)
    busy = accounts_backup.occupied(db)
    assert "users" in busy and "feeds" in busy


# ---------- the endpoint ----------

def _register(client, username):
    r = client.post("/api/auth/register", json={
        "username": username, "email": f"{username}@example.com",
        "password": "correct-horse-staple"})
    assert r.status_code == 201, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


@pytest.fixture
def admin_env(monkeypatch):
    from backend.app import main
    monkeypatch.setattr(main, "_ADMIN_HANDLES", frozenset({"boss"}))


def test_an_operator_can_download_it(client, admin_env):
    headers = _register(client, "boss")
    res = client.get("/api/admin/backup/accounts", headers=headers)
    assert res.status_code == 200, res.text
    assert res.headers["content-disposition"].startswith("attachment;")
    body = json.loads(res.content)
    assert body["format"] == accounts_backup.FORMAT
    assert body["counts"]["users"] == 1, "the operator's own account is in it"


def test_the_download_is_never_cached(client, admin_env):
    """It holds password hashes; no shared cache's business, nor this
    browser's."""
    headers = _register(client, "boss")
    res = client.get("/api/admin/backup/accounts", headers=headers)
    assert res.headers["cache-control"] == "no-store"


def test_the_downloaded_file_is_what_restore_accepts(client, admin_env):
    """The two halves have to agree, and nothing else in this file proves the
    bytes the browser receives are the bytes a restore can read."""
    headers = _register(client, "boss")
    doc = json.loads(client.get("/api/admin/backup/accounts",
                                headers=headers).content)

    from backend.app.database import SessionLocal
    session = SessionLocal()
    try:
        # replace=True because this machine already has the operator account
        # that authorised the download — which is exactly the situation a real
        # restore-onto-a-rebuilt-machine is in.
        plan = accounts_backup.restore(session, doc, dry_run=True, replace=True)
    finally:
        session.close()
    assert plan["users"] == 1


def test_an_ordinary_account_cannot_download_it(client):
    headers = _register(client, "alice")
    res = client.get("/api/admin/backup/accounts", headers=headers)
    assert res.status_code == 403


def test_a_stranger_cannot_download_it(client):
    res = client.get("/api/admin/backup/accounts")
    assert res.status_code in (401, 403, 422)


# ---------- it is restorable without an account to sign in with ----------

def test_there_is_a_command_line_way_in():
    """The moment a backup is needed is the moment the database is empty, and
    an operator-only endpoint on an empty database has nobody to authorise it."""
    assert callable(accounts_backup._main)
    doc = accounts_backup._main.__doc__ or ""
    assert "restore" in doc


def test_the_module_warns_that_the_file_holds_secrets():
    """Password hashes and webhook URLs. Someone will read this docstring
    before deciding where to put the file."""
    assert "SECRET" in (accounts_backup.__doc__ or "").upper()
