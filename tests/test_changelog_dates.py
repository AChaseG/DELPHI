"""A changelog entry cannot have shipped on a day that has not happened.

Every date in `changelog.py` is typed by hand, and nothing checked them. The
convention drifted the obvious way: a session adding an entry read the newest
date in the file and wrote the day after it, rather than the day it actually
was. Repeated across sixty-odd sessions the dates marched a day at a time
regardless of the calendar and finished six weeks into the future, which is
what a reader saw at the top of the what's-new popup.

Nothing about the clock was wrong, and no amount of reading the code would
have shown it — only comparing the file against today does, which is what
these do.
"""
from datetime import date, datetime, timedelta

from backend.app import changelog


def _dates():
    return [date.fromisoformat(e["date"]) for e in changelog.CHANGELOG]


def test_no_entry_is_dated_in_the_future():
    today = datetime.utcnow().date()
    # A day of slack: the entry may be written just before midnight UTC by
    # somebody whose calendar has already turned over.
    limit = today + timedelta(days=1)
    ahead = [e["date"] + " — " + e["title"]
             for e in changelog.CHANGELOG
             if date.fromisoformat(e["date"]) > limit]
    assert not ahead, (
        "changelog entries dated after today. Date a new entry with the day it "
        "actually is, not the day after the entry above it:\n  "
        + "\n  ".join(ahead))


def test_entries_run_newest_first():
    dates = _dates()
    assert dates == sorted(dates, reverse=True)


def test_every_date_parses_as_a_calendar_date():
    for entry in changelog.CHANGELOG:
        date.fromisoformat(entry["date"])   # raises if it does not


def test_every_entry_has_a_title_and_items():
    for entry in changelog.CHANGELOG:
        assert entry["title"].strip()
        assert entry["items"] and all(i.strip() for i in entry["items"])


def test_fingerprints_are_unique():
    prints = changelog.fingerprints()
    assert len(prints) == len(set(prints))


# --- the one-time correction ---------------------------------------------

def test_an_entry_seen_under_its_old_date_still_counts_as_seen():
    # Re-dating changes an entry's fingerprint, so without this every reader
    # would be greeted by a popup containing the whole history of the project.
    unchanged = [changelog._fingerprint(e) for e in changelog.CHANGELOG
                 if changelog._fingerprint(e) not in changelog._REDATED.values()]
    stored = list(changelog._REDATED) + unchanged
    assert changelog.unseen_entries(stored) == []


def test_the_map_points_at_entries_that_exist():
    current = set(changelog.fingerprints())
    assert set(changelog._REDATED.values()) <= current


def test_the_map_never_hides_a_genuinely_new_entry():
    # Deleting the map is meant to be safe, and it must never suppress
    # something the reader has not seen.
    assert len(changelog.unseen_entries([])) == len(changelog.CHANGELOG)
