"""Every changelog entry has to survive being hashed and sent.

This exists because three entries did not, and nothing noticed. They were
written with `\\ud83d\\udcdd`-style escapes for their emoji, which in Python
source is not one character — it is two lone surrogates, and lone surrogates
cannot be encoded as UTF-8. `fingerprints()` hashes every entry on every
`/api/session/hello`, so the first page load after that deploy answered 500,
and kept doing it. The app carried on because the frontend tolerates a failed
hello, which is exactly why it went unseen.

No test called `fingerprints()` and no test called `/api/session/hello`. Both
are called here now, over the real changelog, so a malformed entry fails in CI
rather than on every page load.
"""
import json

import pytest

from backend.app import changelog


def _payload(entry: dict) -> str:
    """The same string _fingerprint hashes."""
    return json.dumps([entry["date"], entry["title"], entry["items"]],
                      ensure_ascii=False)


@pytest.mark.parametrize("index", range(len(changelog.CHANGELOG)))
def test_every_entry_can_be_encoded(index):
    """One case per entry, so a failure names the entry instead of the list."""
    entry = changelog.CHANGELOG[index]
    try:
        _payload(entry).encode("utf-8")
    except UnicodeEncodeError as exc:
        pytest.fail(
            f"entry {entry['date']} ({entry['title']!r}) cannot be encoded: "
            f"{exc}. Emoji belong in the source as themselves, not as "
            f"\\ud83d\\udxxx surrogate escapes.")


def test_no_entry_contains_a_lone_surrogate():
    """The specific defect, named. A surrogate can reach here without raising
    until something tries to encode it, so look for it directly."""
    offenders = []
    for entry in changelog.CHANGELOG:
        for ch in _payload(entry):
            if 0xD800 <= ord(ch) <= 0xDFFF:
                offenders.append((entry["date"], hex(ord(ch))))
                break
    assert offenders == [], f"lone surrogates in {offenders}"


def test_fingerprints_can_be_computed_for_the_whole_changelog():
    """What /api/session/hello does on every page load."""
    prints = changelog.fingerprints()
    assert len(prints) == len(changelog.CHANGELOG)
    assert len(set(prints)) == len(prints), "fingerprints must be distinct"


def test_the_whole_changelog_survives_a_json_round_trip():
    """It is serialized into a response body, not only hashed."""
    body = json.dumps(changelog.CHANGELOG, ensure_ascii=False).encode("utf-8")
    assert json.loads(body.decode("utf-8")) == changelog.CHANGELOG


def test_unseen_entries_works_over_the_real_changelog():
    assert changelog.unseen_entries([]) == changelog.CHANGELOG
    assert changelog.unseen_entries(changelog.fingerprints()) == []


def test_dates_are_iso_and_newest_first():
    """The popup and the legacy last-seen comparison both depend on it."""
    from datetime import date
    dates = [date.fromisoformat(e["date"]) for e in changelog.CHANGELOG]
    assert dates == sorted(dates, reverse=True), "entries are not newest-first"


# ---------- the endpoint that was answering 500 ----------

def test_session_hello_succeeds(client):
    """The load-bearing one. A 500 here is survivable in the browser, which is
    precisely why it needs a test rather than a bug report."""
    r = client.post("/api/auth/register", json={
        "username": "alice", "email": "alice@example.com",
        "password": "correct-horse-staple"})
    assert r.status_code == 201, r.text
    headers = {"Authorization": "Bearer " + r.json()["token"]}

    hello = client.post("/api/session/hello", headers=headers)
    assert hello.status_code == 200, hello.text


def test_session_hello_is_fine_the_second_time(client):
    """The first call is the one that writes the fingerprints, so it is the one
    that used to fail. The second proves they were written and can be read back
    — if the write had raised, every load would report every entry as new."""
    r = client.post("/api/auth/register", json={
        "username": "alice", "email": "alice@example.com",
        "password": "correct-horse-staple"})
    headers = {"Authorization": "Bearer " + r.json()["token"]}

    first = client.post("/api/session/hello", headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["first_visit"] is True

    again = client.post("/api/session/hello", headers=headers)
    assert again.status_code == 200, again.text
    assert again.json()["first_visit"] is False
    assert again.json()["updates"] == [], (
        "nothing shipped in between, so nothing is new")
