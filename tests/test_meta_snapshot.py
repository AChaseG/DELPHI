"""/api/meta must not hand the response serializer the poller's own dict.

`ingest.status` starts with four keys and grows to about twenty as a cycle
reports what it did — new articles, new events, content fetched, sources
retired, stall records, audit results. /api/meta returned that live object, so
serializing a response meant walking a dict another task was adding keys to.
Python raises RuntimeError("dictionary changed size during iteration") for
exactly that, and the request becomes a 500 with a reference nobody can look up.

It looks random, and it is likeliest when the machine is busiest, because that
is when requests queue into the moment a cycle finishes and writes its results.
/api/ingest/status already took a copy; /api/meta did not.

The other half is the reference. It used to identify one line in a log that
holds about a minute, which meant three faults were chased without anyone ever
seeing the exception. The last few are kept so a quoted reference can be
answered.
"""
import json

import pytest
from starlette.responses import JSONResponse

from backend.app import ingest, main


def _register(client, username="alice"):
    r = client.post("/api/auth/register", json={
        "username": username, "email": f"{username}@example.com",
        "password": "correct-horse-staple"})
    assert r.status_code == 201, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


# ---------- the snapshot ----------

def test_meta_does_not_hand_out_the_live_status_object(db):
    """The defect is aliasing, so the test has to be about identity.

    Deliberately *not* through the HTTP client: a response body is parsed back
    into a fresh dict, so no assertion made after the round trip can tell a copy
    from the original. The first two versions of this test passed against the
    broken code for exactly that reason.
    """
    from backend.app.models import User
    user = User(username="alice", email="a@example.com", password_hash="h")
    db.add(user)
    db.commit()

    result = main.meta(user_id=f"acct:{user.id}", db=db)

    assert result["ingest"] is not ingest.status, (
        "meta returned the poller's own dict; the response serializer would "
        "iterate it while a cycle adds keys to it")
    assert result["ingest"] == ingest.status, "the snapshot must be faithful"


def test_a_growing_dict_really_does_break_the_serializer():
    """The premise, checked rather than assumed — otherwise the test above
    passes for the wrong reason and would keep passing if the copy were
    removed."""
    d = {"a": 1}

    def render_while_growing():
        for i, _ in enumerate(d.items()):
            d[f"new_{i}"] = i

    with pytest.raises(RuntimeError, match="changed size"):
        render_while_growing()


def test_the_serializer_rejects_what_a_snapshot_cannot_fix():
    """Worth pinning: a *value* the encoder refuses is a different fault from
    the race, and a copy does not help. NaN and lone surrogates both fail,
    because Starlette renders with ensure_ascii=False and allow_nan=False."""
    for payload in ({"x": float("nan")}, {"x": "lone \ud83d surrogate"}):
        with pytest.raises((ValueError, UnicodeEncodeError)):
            JSONResponse(payload).render(payload)


def test_meta_still_reports_what_the_poller_did(client, monkeypatch):
    """The copy must not have narrowed the payload."""
    headers = _register(client)
    monkeypatch.setitem(ingest.status, "cycles", 7)
    monkeypatch.setitem(ingest.status, "last_new_articles", 325)

    body = client.get("/api/meta", headers=headers).json()
    assert body["ingest"]["cycles"] == 7
    assert body["ingest"]["last_new_articles"] == 325


# ---------- the reference can be looked up ----------

@pytest.fixture(autouse=True)
def _clear_failures():
    main.recent_failures.clear()
    yield
    main.recent_failures.clear()


@pytest.fixture
def failing_client(client):
    """A client that lets the app answer a crash instead of re-raising it.

    TestClient's default is to re-raise a server exception into the test, which
    is usually what you want and is exactly wrong here: the behaviour under test
    *is* the response the handler builds.
    """
    from fastapi.testclient import TestClient
    from backend.app.main import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_an_unhandled_error_is_recorded_with_its_reference(failing_client, monkeypatch):
    headers = _register(failing_client)

    def boom(db):
        raise RuntimeError("the specific thing that went wrong")

    monkeypatch.setattr(main, "_stats", boom)

    res = failing_client.get("/api/meta", headers=headers)
    assert res.status_code == 500
    reference = res.json()["reference"]

    (record,) = main.recent_failures
    assert record["reference"] == reference
    assert record["error"] == "RuntimeError"
    assert "the specific thing" in record["detail"]
    assert record["path"] == "/api/meta"
    assert record["method"] == "GET"


def test_the_record_is_served_to_operators(failing_client, monkeypatch):
    headers = _register(failing_client)
    monkeypatch.setattr(main, "_stats", lambda db: (_ for _ in ()).throw(
        RuntimeError("boom")))
    reference = failing_client.get("/api/meta", headers=headers).json()["reference"]
    monkeypatch.undo()

    failures = failing_client.get(
        "/api/ingest/status", headers=headers).json()["failures"]
    assert [f["reference"] for f in failures] == [reference]


def test_the_newest_failure_is_first():
    """The question is always about the one just seen."""
    for i in range(3):
        main.recent_failures.appendleft({"reference": f"ref{i}"})
    assert [f["reference"] for f in main.recent_failures] == ["ref2", "ref1", "ref0"]


def test_the_record_is_bounded():
    for i in range(main.MAX_RECENT_FAILURES * 3):
        main.recent_failures.appendleft({"reference": str(i)})
    assert len(main.recent_failures) == main.MAX_RECENT_FAILURES


def test_no_traceback_reaches_the_browser(failing_client, monkeypatch):
    """Unchanged behaviour, worth holding: file paths stay in the log."""
    headers = _register(failing_client)
    monkeypatch.setattr(main, "_stats", lambda db: (_ for _ in ()).throw(
        RuntimeError("secret /app/path detail")))

    body = failing_client.get("/api/meta", headers=headers).json()
    assert "secret" not in json.dumps(body)
    assert "reference" in body
