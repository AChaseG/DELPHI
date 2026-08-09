"""The backup has to happen without anyone remembering to click.

Fly's nightly volume snapshots are off, because copying six gigabytes off the
volume every night took the site down for hours. A button in the operator
console replaces them only in the sense that a fire extinguisher in a locked
cupboard replaces a sprinkler, so the small copy is now taken on a schedule and
mailed off the machine.

Two properties matter more than the schedule itself.

*It leaves.* A backup written to the volume protects against nothing when the
volume is what was lost. So the test for delivery is that it went to an address,
and that the address list is operators only — this file is every account's
password hash, and mailing it to every account would be a breach dressed as a
backup.

*It cannot go quiet.* Every backup that has ever mattered failed by stopping
without saying so. A failed send must not be recorded as success, must not
poison the "nothing changed" shortcut, and must be visible in the operator
console rather than only in a log.
"""
import json

import pytest

from backend.app import accounts_backup, ingest, mailer
from backend.app.models import Feed, User


@pytest.fixture(autouse=True)
def _clean_status(monkeypatch):
    monkeypatch.setattr(accounts_backup, "status", {
        "last_sent_at": None, "last_sent_to": 0, "last_digest": None,
        "last_skipped_reason": None, "sent_total": 0})
    monkeypatch.setattr(accounts_backup, "TO", "")
    yield


@pytest.fixture
def sent(monkeypatch):
    """Capture what would have been mailed instead of mailing it."""
    box = []

    def fake(to, subject, body, *, filename, data, mime="application/json"):
        box.append({"to": to, "subject": subject, "body": body,
                    "filename": filename, "data": data})
        return True

    monkeypatch.setattr(accounts_backup.mailer, "send_attachment", fake)
    return box


def _operator(db, username="boss", email="boss@example.com"):
    u = User(username=username, email=email, password_hash="h", is_admin=True)
    db.add(u)
    db.commit()
    return u


# ---------- who it goes to ----------

def test_it_goes_to_operators(db):
    _operator(db)
    assert accounts_backup.recipients(db) == ["boss@example.com"]


def test_it_does_not_go_to_ordinary_accounts(db):
    """The file is every account's password hash."""
    _operator(db)
    db.add(User(username="alice", email="alice@example.com", password_hash="h"))
    db.commit()
    assert accounts_backup.recipients(db) == ["boss@example.com"]


def test_an_operator_with_no_email_is_not_a_recipient(db):
    db.add(User(username="boss", email="", password_hash="h", is_admin=True))
    db.commit()
    assert accounts_backup.recipients(db) == []


def test_an_explicit_address_wins(db, monkeypatch):
    _operator(db)
    monkeypatch.setattr(accounts_backup, "TO", "vault@example.com, two@example.com")
    assert accounts_backup.recipients(db) == ["vault@example.com", "two@example.com"]


# ---------- it leaves the machine ----------

def test_it_mails_a_copy(db, sent):
    _operator(db)
    result = accounts_backup.send_scheduled(db)

    assert result["sent"] == 1
    assert len(sent) == 1
    assert sent[0]["to"] == "boss@example.com"
    assert sent[0]["filename"].startswith("delphi-accounts-")


def test_the_attachment_is_a_restorable_backup(db, sent):
    """Not merely "a file was attached": the bytes have to be the thing."""
    _operator(db)
    accounts_backup.send_scheduled(db)

    doc = json.loads(sent[0]["data"].decode("utf-8"))
    assert doc["format"] == accounts_backup.FORMAT
    assert doc["counts"]["users"] == 1
    plan = accounts_backup.restore(db, doc, dry_run=True, replace=True)
    assert plan["users"] == 1


def test_the_message_says_the_file_is_sensitive(db, sent):
    """It carries password hashes into somebody's inbox; it has to say so."""
    _operator(db)
    accounts_backup.send_scheduled(db)
    assert "password hashes" in sent[0]["body"]


def test_the_message_says_how_to_restore(db, sent):
    """The person reading it will be having a bad day."""
    _operator(db)
    accounts_backup.send_scheduled(db)
    assert "accounts_backup restore" in sent[0]["body"]


def test_every_operator_gets_one(db, sent):
    _operator(db)
    db.add(User(username="second", email="second@example.com",
                password_hash="h", is_admin=True))
    db.commit()

    assert accounts_backup.send_scheduled(db)["sent"] == 2
    assert {m["to"] for m in sent} == {"boss@example.com", "second@example.com"}


# ---------- it does not become noise ----------

def test_an_unchanged_system_is_not_mailed_twice(db, sent):
    _operator(db)
    assert accounts_backup.send_scheduled(db)["sent"] == 1
    second = accounts_backup.send_scheduled(db)

    assert second["sent"] == 0
    assert "nothing changed" in second["reason"]
    assert len(sent) == 1


def test_a_change_is_mailed(db, sent):
    owner = _operator(db)
    accounts_backup.send_scheduled(db)
    db.add(Feed(user_id=f"acct:{owner.id}", name="Energy", criteria={}))
    db.commit()

    assert accounts_backup.send_scheduled(db)["sent"] == 1
    assert len(sent) == 2


def test_the_digest_ignores_when_it_was_taken(db):
    """Otherwise every copy looks new and the daily mail never stops."""
    _operator(db)
    first = accounts_backup.build(db)
    second = dict(accounts_backup.build(db))
    # Force the difference rather than hoping the clock ticked between builds.
    second["taken_at"] = "1999-01-01T00:00:00Z"
    assert accounts_backup.digest(first) == accounts_backup.digest(second)


def test_the_digest_notices_a_real_change(db):
    owner = _operator(db)
    before = accounts_backup.digest(accounts_backup.build(db))
    db.add(Feed(user_id=f"acct:{owner.id}", name="Energy", criteria={}))
    db.commit()
    assert accounts_backup.digest(accounts_backup.build(db)) != before


# ---------- it cannot go quiet ----------

def test_a_failed_send_is_not_recorded_as_success(db, monkeypatch):
    _operator(db)
    monkeypatch.setattr(accounts_backup.mailer, "send_attachment",
                        lambda *a, **k: False)

    result = accounts_backup.send_scheduled(db)

    assert result["sent"] == 0
    assert accounts_backup.status["last_sent_at"] is None
    assert accounts_backup.status["last_skipped_reason"]


def test_a_failed_send_is_retried_next_time(db, monkeypatch):
    """The trap: remembering the digest on a failed send would make the next
    attempt say "nothing changed" and never try again."""
    _operator(db)
    monkeypatch.setattr(accounts_backup.mailer, "send_attachment",
                        lambda *a, **k: False)
    accounts_backup.send_scheduled(db)
    assert accounts_backup.status["last_digest"] is None

    box = []
    monkeypatch.setattr(accounts_backup.mailer, "send_attachment",
                        lambda *a, **k: box.append(a) or True)
    assert accounts_backup.send_scheduled(db)["sent"] == 1


def test_no_recipient_is_reported_not_swallowed(db, sent):
    db.add(User(username="alice", email="alice@example.com", password_hash="h"))
    db.commit()

    result = accounts_backup.send_scheduled(db)

    assert result["sent"] == 0
    assert "no operator account" in result["reason"]
    assert accounts_backup.status["last_skipped_reason"]


def test_it_never_raises_into_the_poll_loop(db, monkeypatch):
    """It runs inside the cycle that collects the news. A mail relay having a
    bad day must not stop the news."""
    _operator(db)

    def explode(*a, **k):
        raise RuntimeError("relay on fire")

    monkeypatch.setattr(accounts_backup.mailer, "send_attachment", explode)
    result = accounts_backup.send_scheduled(db)

    assert result["sent"] == 0
    assert "RuntimeError" in accounts_backup.status["last_skipped_reason"]


def test_a_successful_send_is_recorded(db, sent):
    _operator(db)
    accounts_backup.send_scheduled(db)

    assert accounts_backup.status["last_sent_at"]
    assert accounts_backup.status["last_sent_to"] == 1
    assert accounts_backup.status["sent_total"] == 1
    assert accounts_backup.status["last_digest"]
    assert accounts_backup.status["last_skipped_reason"] is None


def test_the_operator_console_can_see_it(client):
    reg = client.post("/api/auth/register", json={
        "username": "boss", "email": "boss@example.com",
        "password": "correct-horse-staple"})
    assert reg.status_code == 201, reg.text
    headers = {"Authorization": "Bearer " + reg.json()["token"]}

    res = client.get("/api/ingest/status", headers=headers)
    assert res.status_code == 200, res.text
    backup = res.json()["account_backup"]
    assert "last_sent_at" in backup
    assert "every_s" in backup
    assert "recipients" in backup


# ---------- the schedule ----------

def test_the_poller_runs_it():
    import inspect
    body = inspect.getsource(ingest.ingest_loop)
    assert "accounts_backup.send_scheduled" in body
    assert "accounts_backup.EVERY_SECONDS" in body


def test_it_runs_off_the_event_loop():
    """build() is several queries and send_attachment opens an SMTP connection
    with a 30-second timeout. On the loop that is the site not answering."""
    import inspect
    body = inspect.getsource(ingest.ingest_loop)
    assert "asyncio.to_thread(accounts_backup.send_scheduled" in body


def test_it_can_be_switched_off():
    """Zero disables it, and the loop has to honour that rather than send
    anyway and rely on the interval."""
    import inspect
    body = inspect.getsource(ingest.ingest_loop)
    assert "accounts_backup.EVERY_SECONDS > 0" in body


def test_the_first_copy_is_not_sent_at_startup():
    """A deploy loop would otherwise mail one copy per deploy, and a restart is
    not a reason to back up."""
    assert ingest._next_account_backup_at > 0


def test_the_default_interval_is_daily():
    assert accounts_backup.EVERY_SECONDS == 24 * 3600


# ---------- the mailer half ----------

def test_send_attachment_is_a_no_op_when_mail_is_off(monkeypatch):
    monkeypatch.setattr(mailer, "HOST", "")
    assert mailer.send_attachment("a@example.com", "s", "b",
                                  filename="f.json", data=b"{}") is False


def test_send_attachment_builds_a_message_with_the_file(monkeypatch):
    """Exercised through smtplib rather than asserted about, so a malformed
    message would fail here rather than in somebody's inbox."""
    captured = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=0): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, u, p): pass
        def send_message(self, msg): captured["msg"] = msg

    monkeypatch.setattr(mailer, "HOST", "smtp.example.com")
    monkeypatch.setattr(mailer, "USER", "")
    monkeypatch.setattr(mailer, "TLS", "none")
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)

    ok = mailer.send_attachment("a@example.com", "subject", "body text",
                                filename="accounts.json", data=b'{"a": 1}')

    assert ok is True
    msg = captured["msg"]
    assert msg["To"] == "a@example.com"
    attachments = list(msg.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "accounts.json"
    assert attachments[0].get_payload(decode=True) == b'{"a": 1}'
