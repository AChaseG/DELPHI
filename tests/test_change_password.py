"""Changing a password while signed in.

Delphi could previously only reset a password by emailing a link — the flow for
somebody locked out. That left no way to simply choose a different one, and no
way at all on an instance with no SMTP configured.

The interesting part is not the swap, it is the two things around it: proving
who is asking, and deciding who gets signed out afterwards.
"""
import pytest

from backend.app import auth, ratelimit
from backend.app.models import User

CURRENT = "correct-horse"


@pytest.fixture
def account(client, db):
    """A signed-in account: returns (headers, token).

    Depends on `db` even though it does not use it, so that a test asking for
    both gets the schema reset *before* this registers rather than after —
    otherwise the reset deletes the account this just created.
    """
    resp = client.post("/api/auth/register", json={
        "username": "reader", "email": "reader@example.com", "password": CURRENT})
    assert resp.status_code == 201, resp.text
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}, token


def _change(client, headers, current=CURRENT, new="a-brand-new-one"):
    return client.post("/api/auth/change-password",
                       json={"current": current, "new": new}, headers=headers)


def _works(client, token):
    return client.get("/api/feeds",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 200


# ---------- it does the thing ----------

def test_the_new_password_works_and_the_old_one_stops(client, account):
    headers, _ = account
    assert _change(client, headers).status_code == 200

    assert client.post("/api/auth/login", json={
        "username": "reader", "password": "a-brand-new-one"}).status_code == 200
    assert client.post("/api/auth/login", json={
        "username": "reader", "password": CURRENT}).status_code == 401


def test_it_needs_no_email(client, account):
    """conftest leaves SMTP unconfigured — the case that had no route at all."""
    from backend.app import mailer
    assert not mailer.enabled()
    headers, _ = account
    assert _change(client, headers).status_code == 200


# ---------- proving who is asking ----------

def test_the_current_password_is_required(client, account):
    """A session is not proof of ownership: it can be an unlocked laptop.

    Without this, brief access would be enough to take the account for good.
    """
    headers, token = account
    resp = _change(client, headers, current="not-the-password")
    assert resp.status_code == 403
    assert _works(client, token)                       # nothing changed
    assert client.post("/api/auth/login", json={
        "username": "reader", "password": CURRENT}).status_code == 200


def test_a_wrong_password_does_not_sign_the_reader_out(client, account):
    """403 rather than 401, deliberately.

    The API layer treats 401 as "this session is over" and reloads the page,
    so answering a mistyped password with one would sign someone out for a typo.
    """
    headers, _ = account
    assert _change(client, headers, current="typo").status_code == 403


def test_it_needs_a_session_at_all(client, account):
    assert client.post("/api/auth/change-password",
                       json={"current": CURRENT, "new": "a-brand-new-one"}).status_code == 401


def test_an_ended_session_cannot_change_the_password(client, account):
    """/api/auth/* sits outside require_account, so this route checks for itself.

    Reading the token alone would have accepted one already revoked.
    """
    headers, token = account
    client.post("/api/auth/sign-out-everywhere", headers=headers)

    assert _change(client, headers).status_code == 401


def test_a_suspended_account_cannot_change_the_password(client, register, db):
    admin_headers = register("boss")
    db.query(User).filter_by(username="boss").one().is_admin = True
    db.commit()
    victim = client.post("/api/auth/register", json={
        "username": "victim", "email": "victim@example.com", "password": CURRENT}).json()
    victim_headers = {"Authorization": f"Bearer {victim['token']}"}
    uid = db.query(User).filter_by(username="victim").one().id

    client.post(f"/api/admin/users/{uid}/disable", json={"disabled": True},
                headers=admin_headers)

    assert _change(client, victim_headers).status_code == 403


# ---------- who gets signed out ----------

def test_other_devices_are_signed_out(client, account):
    """The usual reason to change a password is worry about another device."""
    headers, _ = account
    phone = client.post("/api/auth/login", json={
        "username": "reader", "password": CURRENT}).json()["token"]
    assert _works(client, phone)

    _change(client, headers)

    assert not _works(client, phone)


def test_this_browser_stays_signed_in(client, account):
    """It just proved it knows the password; throwing it out would be perverse.

    The new token comes back in the response, which is what keeps the tab alive.
    """
    headers, old_token = account
    resp = _change(client, headers)

    assert resp.json()["sessions_ended"] is True
    new_token = resp.json()["token"]
    assert new_token != old_token
    assert _works(client, new_token)
    assert not _works(client, old_token)      # the one it replaced is done


def test_the_response_carries_what_the_client_needs_to_stay_signed_in(client, account, db):
    headers, _ = account
    uid = db.query(User).filter_by(username="reader").one().id

    body = _change(client, headers).json()

    assert body["username"] == "reader"
    assert body["user_key"] == f"acct:{uid}"
    assert body["email"] == "reader@example.com"
    assert "is_admin" in body


def test_another_account_is_untouched(client, account):
    headers, _ = account
    other = client.post("/api/auth/register", json={
        "username": "other", "email": "other@example.com", "password": CURRENT}).json()["token"]

    _change(client, headers)

    assert _works(client, other)


# ---------- refusals that are not about identity ----------

def test_a_short_password_is_refused(client, account):
    headers, token = account
    assert _change(client, headers, new="short").status_code == 422
    assert _works(client, token)


def test_reusing_the_same_password_is_refused(client, account):
    """Otherwise a no-op change still signs every other device out."""
    headers, _ = account
    phone = client.post("/api/auth/login", json={
        "username": "reader", "password": CURRENT}).json()["token"]

    resp = _change(client, headers, new=CURRENT)

    assert resp.status_code == 422
    assert _works(client, phone)               # nobody was signed out for nothing


def test_guessing_the_current_password_here_is_limited(client, account, monkeypatch):
    headers, _ = account
    monkeypatch.setattr(ratelimit, "ENABLED", True)
    monkeypatch.setattr(ratelimit, "TRUSTED_PROXIES", 0)
    ratelimit._hits.clear()
    limit = ratelimit._LIMITS["change_password"][0]

    allowed = 0
    for i in range(limit + 5):
        if _change(client, headers, current=f"guess{i}").status_code == 429:
            break
        allowed += 1
    ratelimit._hits.clear()
    assert allowed == limit


def test_it_does_not_share_a_budget_with_signing_in(client, account, monkeypatch):
    """Mistyping here must not lock the account out of the sign-in page."""
    headers, _ = account
    monkeypatch.setattr(ratelimit, "ENABLED", True)
    monkeypatch.setattr(ratelimit, "TRUSTED_PROXIES", 0)
    ratelimit._hits.clear()

    for i in range(ratelimit._LIMITS["change_password"][0] + 2):
        _change(client, headers, current=f"guess{i}")

    signin = client.post("/api/auth/login",
                         json={"username": "reader", "password": CURRENT})
    ratelimit._hits.clear()
    assert signin.status_code == 200
