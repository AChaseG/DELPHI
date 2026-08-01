"""Can a session be ended, and does the credential stay out of the URL?

Two properties, one story. A token that is signed and unexpired used to be
enough on its own, which meant nothing anybody did could end a session already
issued: reset your password and the person holding your token kept it for the
rest of its thirty days. And the credential that opened the event stream rode
in the query string, so every page load wrote a thirty-day token into the
access log of every proxy in between.

Fixing one without the other leaves the story half-done — a leak you cannot
respond to, or a response to a leak that keeps happening.
"""
import time

import pytest

from backend.app import auth
from backend.app.models import User


def _token_of(resp):
    return resp.json()["token"]


def _sign_up(client, name="reader", password="correct-horse"):
    resp = client.post("/api/auth/register", json={
        "username": name, "email": f"{name}@example.com", "password": password})
    assert resp.status_code == 201, resp.text
    return _token_of(resp)


def _works(client, token):
    """Is this token still accepted?"""
    return client.get("/api/feeds",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 200


# ---------- the token says which generation of the account it belongs to ----------

def test_token_carries_the_account_generation():
    token = auth.make_token(7, 3)
    assert auth.parse_token(token) == auth.Claim(user_id=7, token_version=3)


def test_a_tampered_token_is_refused():
    token = auth.make_token(7, 3)
    payload, sig = token.split(".", 1)
    assert auth.parse_token(f"{payload}.{'0' * len(sig)}") is None


def test_an_expired_token_is_refused(monkeypatch):
    monkeypatch.setattr(auth, "TOKEN_TTL_SECONDS", -1)
    assert auth.parse_token(auth.make_token(7, 0)) is None


def test_tokens_from_before_versioning_are_refused():
    """Reading them as version 0 would exempt every already-stolen token.

    Those are the tokens revocation exists for, so they are refused and their
    holders sign in once. The upgrade is itself the clean sweep the account
    never had a way to perform.
    """
    import base64
    import hashlib
    import hmac
    legacy_payload = f"7:{int(time.time()) + 3600}"          # the old two-field shape
    b64 = base64.urlsafe_b64encode(legacy_payload.encode()).decode().rstrip("=")
    sig = hmac.new(auth._secret(), b64.encode(), hashlib.sha256).hexdigest()
    assert auth.parse_token(f"{b64}.{sig}") is None           # correctly signed, still refused


# ---------- ending sessions ----------

def test_signing_out_everywhere_ends_this_session(client):
    token = _sign_up(client)
    assert _works(client, token)

    resp = client.post("/api/auth/sign-out-everywhere",
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200 and resp.json()["sessions_ended"]

    assert not _works(client, token)


def test_signing_out_everywhere_ends_the_other_devices_too(client):
    """The whole point: a session you are not holding is the one to end."""
    _sign_up(client)
    phone = _token_of(client.post("/api/auth/login",
                                  json={"username": "reader", "password": "correct-horse"}))
    laptop = _token_of(client.post("/api/auth/login",
                                   json={"username": "reader", "password": "correct-horse"}))
    assert _works(client, phone) and _works(client, laptop)

    client.post("/api/auth/sign-out-everywhere",
                headers={"Authorization": f"Bearer {laptop}"})

    assert not _works(client, phone)


def test_signing_in_again_afterwards_works(client):
    token = _sign_up(client)
    client.post("/api/auth/sign-out-everywhere",
                headers={"Authorization": f"Bearer {token}"})

    fresh = _token_of(client.post("/api/auth/login",
                                  json={"username": "reader", "password": "correct-horse"}))
    assert _works(client, fresh)


def test_one_account_signing_out_leaves_others_alone(client):
    mine = _sign_up(client, "reader")
    yours = _sign_up(client, "other")

    client.post("/api/auth/sign-out-everywhere",
                headers={"Authorization": f"Bearer {mine}"})

    assert _works(client, yours)


def test_the_reason_is_explained_rather_than_a_bare_401(client):
    """"Signed out" with no reason reads as a bug, and people report it as one."""
    token = _sign_up(client)
    client.post("/api/auth/sign-out-everywhere",
                headers={"Authorization": f"Bearer {token}"})

    resp = client.get("/api/feeds", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    detail = resp.json()["detail"].lower()
    assert "password was changed" in detail or "signed it out everywhere" in detail


def test_password_reset_ends_existing_sessions(client, db):
    """The reason to reset a password is usually that someone else has it."""
    stolen = _sign_up(client)
    user = db.query(User).filter_by(username="reader").one()

    resp = client.post("/api/auth/reset", json={
        "token": auth.make_scoped_token("reset", user.id, 3600),
        "password": "a-brand-new-one"})
    assert resp.status_code == 200 and resp.json()["sessions_ended"]

    assert not _works(client, stolen)


def test_operator_resetting_a_password_ends_that_account_s_sessions(client, register, db):
    """Operators reset passwords in response to a compromise, too."""
    admin_headers = register("boss")
    db.query(User).filter_by(username="boss").one().is_admin = True
    db.commit()

    victim_token = _sign_up(client, "victim")
    victim_id = db.query(User).filter_by(username="victim").one().id

    resp = client.post(f"/api/admin/users/{victim_id}/reset-password",
                       json={"password": "operator-chosen"}, headers=admin_headers)
    assert resp.status_code == 200, resp.text

    assert not _works(client, victim_token)


def test_me_also_refuses_an_ended_session(client):
    """It reads the token itself rather than going through the middleware."""
    token = _sign_up(client)
    client.post("/api/auth/sign-out-everywhere",
                headers={"Authorization": f"Bearer {token}"})

    assert client.get("/api/auth/me",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 401


# ---------- the session token no longer travels in a URL ----------

def test_a_session_token_in_the_query_string_is_not_accepted(client):
    """The regression this half exists for.

    ?token= used to authenticate any API route, so the stream put a thirty-day
    credential in the URL — and therefore in every proxy's access log — on each
    page load. Nothing reads it from there now.
    """
    token = _sign_up(client)
    assert _works(client, token)                                    # header: fine

    assert client.get(f"/api/feeds?token={token}").status_code == 401
    assert client.get(f"/api/meta?token={token}").status_code == 401
    assert client.get(f"/api/stream?token={token}").status_code == 401


def test_a_ticket_is_minted_from_a_header_authenticated_call(client):
    token = _sign_up(client)
    resp = client.post("/api/stream/ticket", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["expires_in"] == auth.STREAM_TICKET_TTL_SECONDS
    assert auth.parse_stream_ticket(resp.json()["ticket"]) is not None


def test_minting_a_ticket_needs_a_session(client):
    assert client.post("/api/stream/ticket").status_code == 401


@pytest.fixture
def stream_route_answers_immediately():
    """Swap the endless SSE body for a one-line reply, keeping the middleware.

    The refusals above only prove the door stays shut; something has to prove
    it opens. But an event stream never ends, and every in-process client here
    waits for a response to finish before handing one back — TestClient's
    portal blocks, and httpx's ASGITransport buffers the body — so requesting
    the real endpoint hangs the run rather than testing it.

    What changed is the authentication decision, and that lives in the
    middleware, which runs before any of this. So the route body is replaced
    and the middleware left alone. The streaming itself is verified against a
    real server, where it belongs.
    """
    from starlette.responses import PlainTextResponse
    from starlette.routing import request_response

    from backend.app.main import app

    route = next(r for r in app.routes if getattr(r, "path", "") == "/api/stream")
    original = route.app
    route.app = request_response(lambda request: PlainTextResponse("event: hello"))
    try:
        yield
    finally:
        route.app = original


def test_the_stream_accepts_a_ticket(client, stream_route_answers_immediately):
    token = _sign_up(client)
    ticket = client.post("/api/stream/ticket",
                         headers={"Authorization": f"Bearer {token}"}).json()["ticket"]

    resp = client.get(f"/api/stream?ticket={ticket}")
    assert resp.status_code == 200
    assert "hello" in resp.text


def test_the_stream_refuses_a_missing_or_junk_ticket(client):
    assert client.get("/api/stream").status_code == 401
    assert client.get("/api/stream?ticket=not-a-ticket").status_code == 401


def test_a_ticket_opens_the_stream_and_nothing_else(client):
    """A leaked ticket must not be a session — that would be the old bug back."""
    token = _sign_up(client)
    ticket = client.post("/api/stream/ticket",
                         headers={"Authorization": f"Bearer {token}"}).json()["ticket"]

    assert client.get(f"/api/feeds?ticket={ticket}").status_code == 401
    assert client.get("/api/feeds",
                      headers={"Authorization": f"Bearer {ticket}"}).status_code == 401


def test_a_session_token_is_not_usable_as_a_ticket(client):
    """The two are separately scoped, so neither substitutes for the other."""
    token = _sign_up(client)
    assert client.get(f"/api/stream?ticket={token}").status_code == 401


def test_a_ticket_expires(client, monkeypatch):
    token = _sign_up(client)
    monkeypatch.setattr(auth, "STREAM_TICKET_TTL_SECONDS", -1)
    ticket = client.post("/api/stream/ticket",
                         headers={"Authorization": f"Bearer {token}"}).json()["ticket"]

    assert client.get(f"/api/stream?ticket={ticket}").status_code == 401


def test_a_ticket_stops_working_when_the_account_is_suspended(client, register, db):
    """The stream is checked against the account like every other route."""
    admin_headers = register("boss")
    db.query(User).filter_by(username="boss").one().is_admin = True
    db.commit()

    token = _sign_up(client, "victim")
    ticket = client.post("/api/stream/ticket",
                         headers={"Authorization": f"Bearer {token}"}).json()["ticket"]
    victim_id = db.query(User).filter_by(username="victim").one().id

    client.post(f"/api/admin/users/{victim_id}/disable", json={"disabled": True},
                headers=admin_headers)

    assert client.get(f"/api/stream?ticket={ticket}").status_code == 403
