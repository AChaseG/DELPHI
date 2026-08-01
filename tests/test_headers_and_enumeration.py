"""Two things the browser and the sign-up form should not give away.

The sign-up form used to answer "an account with that email already exists",
which let anyone with a list of addresses find out which of them read news
here. That is worth knowing about this tool in particular, and the asker never
had to own the address.

The headers are the other direction: telling the browser what the page is
allowed to do, so that a future injected `<script>` has nowhere to run even
though there is no way to inject one today.
"""
import pytest

from backend.app import mailer
from backend.app.main import CSP
from backend.app.models import User


@pytest.fixture
def with_mail(monkeypatch):
    """Pretend SMTP is configured, and collect what would have been sent."""
    sent = []
    monkeypatch.setattr(mailer, "HOST", "smtp.example.com")
    monkeypatch.setattr(mailer, "enabled", lambda: True)
    monkeypatch.setattr(mailer, "send", lambda to, subject, body: sent.append(
        {"to": to, "subject": subject, "body": body}) or True)
    return sent


def _register(client, username, email, password="correct-horse"):
    return client.post("/api/auth/register", json={
        "username": username, "email": email, "password": password})


# ---------- the sign-up form answers the same either way ----------

def test_a_taken_email_looks_exactly_like_a_free_one(client, with_mail):
    """The whole point: the response must not distinguish the two."""
    free = _register(client, "firstcomer", "taken@example.com")
    assert free.status_code == 201

    taken = _register(client, "someoneelse", "taken@example.com")
    fresh = _register(client, "thirdperson", "brand-new@example.com")

    assert taken.status_code == fresh.status_code == 201
    assert taken.json().keys() == fresh.json().keys()
    assert taken.json()["verification_sent"] is fresh.json()["verification_sent"] is True
    # ...and nothing in the body names the existing account.
    assert "firstcomer" not in taken.text


def test_no_second_account_is_created(client, with_mail, db):
    _register(client, "firstcomer", "taken@example.com")
    _register(client, "someoneelse", "taken@example.com")

    assert db.query(User).filter_by(email="taken@example.com").count() == 1
    assert db.query(User).filter_by(username="someoneelse").count() == 0


def test_the_owner_is_told_instead(client, with_mail):
    """The fact is not suppressed, just delivered where only its owner sees it."""
    _register(client, "firstcomer", "taken@example.com")
    with_mail.clear()

    _register(client, "someoneelse", "taken@example.com")

    assert len(with_mail) == 1
    note = with_mail[0]
    assert note["to"] == "taken@example.com"          # the owner, not the asker
    assert "already have" in note["subject"].lower()
    assert "firstcomer" in note["body"]               # their username, as a reminder
    assert "/reset/" in note["body"]                  # and a way back in


def test_the_asker_is_told_nothing(client, with_mail):
    """No mail goes to whoever typed the address in."""
    _register(client, "firstcomer", "taken@example.com")
    with_mail.clear()

    _register(client, "someoneelse", "taken@example.com")

    assert [m["to"] for m in with_mail] == ["taken@example.com"]


def test_the_existing_account_is_untouched(client, with_mail, db):
    token = _register(client, "firstcomer", "taken@example.com")
    before = db.query(User).filter_by(username="firstcomer").one()
    was = (before.password_hash, before.email_verified, before.token_version)

    _register(client, "someoneelse", "taken@example.com", password="attacker-picked")

    db.expire_all()
    after = db.query(User).filter_by(username="firstcomer").one()
    assert (after.password_hash, after.email_verified, after.token_version) == was


def test_a_taken_username_is_still_reported(client, with_mail):
    """Usernames must be unique and are shown on shared feeds — they are public.

    Hiding this would only stop people completing the form.
    """
    _register(client, "firstcomer", "one@example.com")
    again = _register(client, "firstcomer", "two@example.com")
    assert again.status_code == 409
    assert "username" in again.json()["detail"].lower()


def test_without_mail_the_duplicate_is_reported_plainly(client):
    """Nowhere to send the notice, so silence would just break sign-up.

    conftest leaves SMTP unconfigured, which is the self-hosted case.
    """
    _register(client, "firstcomer", "taken@example.com")
    again = _register(client, "someoneelse", "taken@example.com")
    assert again.status_code == 409
    assert "email" in again.json()["detail"].lower()


# ---------- what the browser is told the page may do ----------

def test_every_response_carries_the_policy(client):
    for path in ("/", "/api/meta", "/css/styles.css", "/js/app.js"):
        resp = client.get(path)
        assert resp.headers.get("Content-Security-Policy") == CSP, path


@pytest.mark.parametrize("directive", [
    "default-src 'self'",
    "script-src 'self'",       # no inline handlers, no CDN
    "style-src 'self'",        # no inline style= attributes
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
])
def test_the_policy_is_the_strict_one(directive):
    assert directive in CSP


def test_the_policy_has_no_unsafe_escape_hatches():
    """'unsafe-inline' or 'unsafe-eval' anywhere would undo most of this."""
    assert "unsafe-inline" not in CSP
    assert "unsafe-eval" not in CSP


def test_images_may_come_from_publishers_and_the_map():
    """Thumbnails are hotlinked and map tiles come from OpenStreetMap.

    This is the one directive that cannot be 'self'; it is images only.
    """
    assert "img-src 'self' data: https:" in CSP


def test_the_other_headers_are_set(client):
    resp = client.get("/")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in resp.headers["Permissions-Policy"]
    assert resp.headers["Strict-Transport-Security"].startswith("max-age=")


def test_the_markup_has_nothing_the_policy_would_refuse():
    """script-src/style-src 'self' break the page if either creeps back in.

    Both are easy to reintroduce — an onclick= is the obvious way to wire a
    button — and the symptom is a control that silently stops working in
    production while it works fine wherever the policy is not applied.
    """
    import re
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "frontend" / "index.html").read_text()
    assert not re.search(r'\son[a-z]+\s*=\s*"', html), "inline event handler in index.html"
    assert not re.search(r'\sstyle\s*=\s*"', html), "inline style attribute in index.html"
    assert "<style" not in html, "inline <style> block in index.html"


def test_no_script_reaches_for_eval():
    """script-src 'self' without 'unsafe-eval' refuses both of these."""
    from pathlib import Path

    js_dir = Path(__file__).resolve().parents[1] / "frontend" / "js"
    for path in js_dir.glob("*.js"):
        text = path.read_text()
        assert "eval(" not in text, path.name
        assert "new Function" not in text, path.name
