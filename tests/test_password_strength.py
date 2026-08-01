"""Refusing the passwords that get tried first.

Delphi's other defences assume the password is a secret. Rate limiting bounds
how fast someone guesses; revocable sessions bound what happens once they are
in. Neither touches the realistic case: an address and password reused from a
site that was breached, replayed here. Nothing is guessed, so nothing is slowed.

Two ways this could be worthless. It could miss what attackers actually try —
nobody submits "password", they submit "P@ssw0rd1". Or it could refuse things
that are fine, which teaches people to fight the form and pick something worse.
Both are measured here rather than asserted.
"""
import random
import string

import pytest

from backend.app import passwords as pw

CONTEXT = dict(username="reader", email="reader@example.com")


def _accepts(password, **kw):
    return pw.is_acceptable(password, **{**CONTEXT, **kw})


# ---------- what it has to refuse ----------

@pytest.mark.parametrize("password", [
    "password", "Password", "PASSWORD",          # the list, and case variants
    "password1", "Password123", "welcome1",      # padded with digits
    "sunshine2024!", "letmein!!", "trustno1!",   # padded with punctuation too
    "qwerty123", "monkey123", "baseball1",       # short stems, padded to length
    "football99", "dragon2024", "iloveyou1",
    "p@ssw0rd", "P@ssw0rd1", "P@ssw0rd1!",       # leetspeak
    "L3tm31n99", "s3cr3t123", "adm1n123",        # leet where 1 means i, not l
])
def test_the_common_ones_are_refused(password):
    assert not _accepts(password), f"{password!r} was accepted"


def test_padding_a_short_entry_does_not_get_past_it():
    """Most of the list is under eight characters and refused on length anyway.

    They are kept because they are the stems people pad, and this is the rule
    that makes them earn their place.
    """
    assert not _accepts("qwerty12")      # "qwerty" is six characters
    assert not _accepts("monkey1234")


def test_a_number_is_refused_however_long():
    """Padding a number with digits leaves no stem to look up, so it needs
    its own rule. Eight digits is one of a hundred million."""
    assert not _accepts("19850312")
    assert not _accepts("159357123")
    assert not _accepts("1029384756")


def test_a_single_run_is_refused():
    assert not _accepts("aaaaaaaa")
    assert not _accepts("abcdefgh")
    assert not _accepts("12345678")
    assert not _accepts("87654321")


def test_something_too_short_is_still_too_short():
    assert not _accepts("aB3!x")
    with pytest.raises(pw.WeakPassword, match="at least"):
        pw.check("short", **CONTEXT)


# ---------- the account's own name ----------

def test_a_password_built_from_the_username_is_refused():
    """No list can know this in advance, and anyone who sees the name tries it."""
    assert not _accepts("reader-reader-1")
    assert not _accepts("XxReaderxX99")


def test_a_password_built_from_the_email_name_is_refused():
    assert not pw.is_acceptable("jsmith-and-more", username="other",
                                email="jsmith@example.com")


def test_a_very_short_username_is_not_matched():
    """"jo" appears inside half the dictionary; finding it says nothing."""
    assert pw.is_acceptable("majolica-lantern-kiln", username="jo",
                            email="jo@example.com")


# ---------- what it must not refuse ----------

@pytest.mark.parametrize("password", [
    "correct-horse-battery-staple",
    "Th3 Quiet Aegean Desk",
    "brittle-lantern-99",
    "kx7Qm2vLp0zR",
    "my dog ate the newspaper",
    "Ravenous Tuesday Cartography",
    "zephyr-mandolin-tundra",
    "Aegean.Watch.Desk.2026",
    "s1lverBirchCanopy",
    "the 5th of Never!",
    "windward reef cartogram",
])
def test_good_passwords_are_accepted(password):
    assert _accepts(password), f"{password!r} was refused"


def test_a_password_manager_s_output_is_never_refused():
    """The failure that would matter most in practice.

    Refusing random passwords would push people towards ones they can retype,
    which is the opposite of the point.
    """
    rng = random.Random(20260821)
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    generated = ["".join(rng.choice(alphabet) for _ in range(rng.randint(12, 24)))
                 for _ in range(3000)]
    refused = [p for p in generated if not _accepts(p)]
    assert not refused, f"{len(refused)} generated passwords refused, e.g. {refused[:3]}"


def test_passphrases_of_unrelated_words_are_never_refused():
    """This is what the refusal message tells people to do, so it must work."""
    words = ["lantern", "otter", "quartz", "bramble", "heron", "tundra", "kiln",
             "mandolin", "zephyr", "haddock", "cartogram", "reef", "gazette"]
    rng = random.Random(7)
    phrases = ["-".join(rng.sample(words, 3)) for _ in range(1000)]
    refused = [p for p in phrases if not _accepts(p)]
    assert not refused, f"{len(refused)} passphrases refused, e.g. {refused[:3]}"


# ---------- the list itself ----------

def test_the_list_is_loaded():
    assert len(pw.COMMON) > 9000


def test_a_missing_list_does_not_stop_anyone_signing_in(monkeypatch, tmp_path):
    """Degrade, don't fail closed: the length and look-alike rules still hold.

    A packaging mistake that made the file unreadable should cost the list
    check, not the ability to have an account.
    """
    monkeypatch.setattr(pw, "LIST_PATH", tmp_path / "nothing-here.txt")
    assert pw._load() == frozenset()

    monkeypatch.setattr(pw, "COMMON", frozenset())
    assert pw.is_acceptable("bramble-otter-kiln", **CONTEXT)
    assert not pw.is_acceptable("short", **CONTEXT)
    assert not pw.is_acceptable("12345678", **CONTEXT)


def test_the_message_says_what_to_do_instead():
    """"Invalid password" teaches nothing and gets retried with "Password2"."""
    with pytest.raises(pw.WeakPassword) as exc:
        pw.check("Password123", **CONTEXT)
    said = str(exc.value).lower()
    assert "common" in said
    assert "words" in said          # points at the thing that actually works


# ---------- every place a password gets set ----------

COMMON_ONE = "Password123"
GOOD_ONE = "bramble-otter-kiln-42"


def test_registration_refuses_it(client):
    resp = client.post("/api/auth/register", json={
        "username": "reader", "email": "reader@example.com", "password": COMMON_ONE})
    assert resp.status_code == 422
    assert "common" in resp.json()["detail"].lower()


def test_registration_refuses_a_password_made_of_the_username(client):
    resp = client.post("/api/auth/register", json={
        "username": "reader", "email": "reader@example.com", "password": "reader-reader"})
    assert resp.status_code == 422
    assert "username" in resp.json()["detail"].lower()


def test_the_emailed_reset_refuses_it(client, db):
    from backend.app import auth
    from backend.app.models import User

    client.post("/api/auth/register", json={
        "username": "reader", "email": "reader@example.com", "password": GOOD_ONE})
    uid = db.query(User).filter_by(username="reader").one().id

    resp = client.post("/api/auth/reset", json={
        "token": auth.make_scoped_token("reset", uid, 3600), "password": COMMON_ONE})
    assert resp.status_code == 422
    assert "common" in resp.json()["detail"].lower()


def test_changing_it_while_signed_in_refuses_it(client):
    token = client.post("/api/auth/register", json={
        "username": "reader", "email": "reader@example.com",
        "password": GOOD_ONE}).json()["token"]

    resp = client.post("/api/auth/change-password",
                       json={"current": GOOD_ONE, "new": COMMON_ONE},
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 422
    assert "common" in resp.json()["detail"].lower()


def test_an_operator_setting_one_is_held_to_it_too(client, register, db):
    """A temporary password an operator picks is exactly where "Welcome123" goes."""
    from backend.app.models import User

    admin_headers = register("boss")
    db.query(User).filter_by(username="boss").one().is_admin = True
    db.commit()
    client.post("/api/auth/register", json={
        "username": "victim", "email": "victim@example.com", "password": GOOD_ONE})
    victim_id = db.query(User).filter_by(username="victim").one().id

    resp = client.post(f"/api/admin/users/{victim_id}/reset-password",
                       json={"password": COMMON_ONE}, headers=admin_headers)
    assert resp.status_code == 422
    assert "common" in resp.json()["detail"].lower()

    ok = client.post(f"/api/admin/users/{victim_id}/reset-password",
                     json={"password": "quartz-heron-lantern"}, headers=admin_headers)
    assert ok.status_code == 200


def test_a_good_password_still_gets_through_everywhere(client):
    resp = client.post("/api/auth/register", json={
        "username": "reader", "email": "reader@example.com", "password": GOOD_ONE})
    assert resp.status_code == 201
