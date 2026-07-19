"""User-facing release notes, newest first.

Each entry summarizes what shipped on one calendar date. /api/session/hello
compares entry dates against the account's last_seen_at to build the
"what's new while you were away" popup, so keep `date` in ISO YYYY-MM-DD and
add a new entry (or extend today's) whenever user-visible behavior changes.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

CHANGELOG: list[dict] = [
    {
        "date": "2026-07-12",
        "title": "Self-healing sources, one creation flow, guided starts",
        "items": [
            "Alerts can now reach you out-of-app: tick ✉️ Email me and/or add a "
            "🔗 webhook URL when building an alert, and matching hits are "
            "delivered (batched per firing) even when the dashboard tab is "
            "closed — email needs server SMTP; the webhook POSTs JSON for "
            "Slack/Discord/your own service.",
            "Local coverage for ~500 major cities across 170 countries: every "
            "city now has a local-news source in its own language, so filtering "
            "to a place — or drawing it on the map — surfaces local reporting. "
            "Auto-discovery grows each city's real outlets from these over time.",
            "Rebuilt ingestion as a continuous rolling poller: each source "
            "refreshes on its own cadence (wires every few minutes, city feeds "
            "hourly, quiet ones less often) with per-host pacing, replacing the "
            "big serialized cycle that had grown slow and skipped sources at "
            "this catalog size.",
            "Non-English articles now categorize and score properly: category "
            "detection and breaking-signal scoring gained a multilingual core "
            "(disaster/conflict/health/politics terms across ~10 languages, "
            "incl. CJK) plus the outlet's own RSS section tags — so foreign "
            "coverage populates the category columns and ranks by importance "
            "instead of all landing in “world”.",
            "Fixed foreign-language articles not translating: each article's "
            "language is now detected from its text instead of trusting the "
            "source's tag (aggregators carry many languages; auto-discovered "
            "outlets defaulted to English), so they translate to your reading "
            "language correctly.",
            "Paywalled sources: mark an outlet 🔒 paywalled and Delphi ingests "
            "its RSS headlines and summaries (enough to match feeds and alerts) "
            "without fetching the locked article body, and every story gets a "
            "🔓 archive.ph link to a readable version.",
            "Settings now follow your account: theme, time format, volume, "
            "notifications, staleness threshold, and reading language are "
            "saved server-side and applied wherever you sign in — previously "
            "they lived only in one browser and were lost when the app's URL "
            "changed (new Codespace, new domain) or on another device.",
            "Performance pass: the database no longer blocks readers during "
            "ingestion (WAL), boards skip loading full article bodies unless a "
            "search actually needs them, dashboard stats got an index, and "
            "articles older than 30 days are pruned automatically "
            "(NEWS_RETENTION_DAYS) so the system stays fast as it runs forever.",
            "Security hardening: Pantheon names and server messages are now "
            "rendered as inert text (no cross-account script injection), "
            "emailed verification/reset links use a fixed origin instead of a "
            "spoofable Host header (set NEWS_PUBLIC_URL when behind a proxy), "
            "and sign-in / password-reset endpoints are rate-limited.",
            "Pantheons — organizations inside Delphi. Create one from the new "
            "🏛 panel, invite accounts (or make it public so anyone can join), "
            "and share feeds and alerts with the whole group: every Pantheon "
            "gets its own board beside Home and My feeds, shared alerts fire "
            "for all members, and owner/admin roles plus who-can-invite / "
            "who-can-share settings control access.",
            "Boolean searching leveled up: alongside AND (spaces work too), "
            "OR, NOT/-term, \"exact phrases\", and (grouping), searches now "
            "support strik* wildcards (? = one character) and NEAR/n "
            "proximity (earthquake NEAR/5 tokyo). The 🪄 wizard covers every "
            "operator from boxes — requirement rows that narrow, OR terms "
            "that broaden, proximity pairs, exclusions — with a built-in "
            "operator guide and live match count.",
            "The source catalog now grows itself: when Google News coverage "
            "names an outlet Delphi doesn't have, its own feed is discovered, "
            "validated, and added automatically (tagged auto-discovered in "
            "Sources; NEWS_AUTO_DISCOVER=0 disables).",
            "Broken sources now repair themselves: after repeated 403/404/"
            "not-a-feed errors, Delphi rediscovers the outlet's real feed "
            "(homepage autodiscovery, common paths, Google News fallback), "
            "validates it, and switches over — original URL kept for reference. "
            "A 🔧 button in Sources forces an attempt on demand.",
            "The + Feed and + Alert buttons merged into a single + Create button: "
            "a 📋 Feed / 🔔 Alert toggle inside the wizard picks what you're making.",
            "Convert any existing feed into an alert (or alert into a feed) by "
            "flipping that toggle while editing — every criterion carries over.",
            "The FAQ & tutorial now opens automatically on your first visit, and "
            "again if you've been away a week or more.",
            "This What's-new popup recaps everything that shipped while you were "
            "away, grouped by date — and appears live in sessions that are "
            "already open, within minutes of an update landing. Reopen it any "
            "time from ⚙ Settings → Help.",
        ],
    },
    {
        "date": "2026-07-11",
        "title": "Accounts, Event Focus everywhere, and a Greek revival",
        "items": [
            "Accounts are now required: register with an email, verify it, and "
            "recover access with password reset — your feeds and alerts are private.",
            "Selecting any story opens Event Focus — synopsis, map, cross-source "
            "timeline, and related events — even for single-source stories; viewed "
            "events dim so you can see what you've triaged.",
            "Feeds match against full article content, not just headlines, and can "
            "carry multiple boolean queries (Google-style syntax welcome) plus an "
            "optional guided query builder.",
            "New feed powers: date ranges, auto-hiding of stale events (threshold "
            "in Settings), automatic worldwide coverage of your queries, and a "
            "guided four-step creation wizard.",
            "Home (curated columns) vs 📋 My feeds views; the board scrolls "
            "laterally so no feed ever hides below another.",
            "D.E.L.P.H.I. rebrand: green & gold theme, the official logo, Greek "
            "columns and meander frieze, plus a Settings panel (time formats "
            "including military DTG, language, notifications) and this FAQ.",
        ],
    },
    {
        "date": "2026-07-10",
        "title": "Alerts you can see, sources you can shape",
        "items": [
            "Alert hits now plot on a map alongside your geofences, with article "
            "thumbnails in the hit list.",
            "Social media ingestion (Reddit, Bluesky, Mastodon, YouTube) with a "
            "platform filter in the wizard, and full manual source management — "
            "add, edit, or retire any source.",
            "Feed headers show compact criteria badges (first country + how many "
            "more) so long selections never crowd the title.",
        ],
    },
    {
        "date": "2026-07-08",
        "title": "Ingest reliability",
        "items": [
            "Fixed a failure where two sources syndicating the same article URL "
            "could roll back an entire ingest cycle — cycles now dedupe globally "
            "and commit per source.",
        ],
    },
    {
        "date": "2026-07-07",
        "title": "Delphi is born",
        "items": [
            "The system is named Delphi, with event clustering (cross-source "
            "stories grouped into events) and automatic translation into your "
            "preferred reading language.",
            "One-click launch in GitHub Codespaces and Docker/Fly.io configs for "
            "24/7 hosting.",
        ],
    },
]


def updates_since(seen: datetime) -> list[dict]:
    """Entries shipped after the calendar day the user was last seen.
    Legacy fallback for accounts that predate fingerprint tracking."""
    return [e for e in CHANGELOG
            if datetime.fromisoformat(e["date"]).date() > seen.date()]


def _fingerprint(entry: dict) -> str:
    """Stable id for one entry's exact content — extending today's entry with
    a new item changes its fingerprint, so live sessions get told about it."""
    payload = json.dumps([entry["date"], entry["title"], entry["items"]],
                         ensure_ascii=False)
    return entry["date"] + ":" + hashlib.sha1(payload.encode()).hexdigest()[:10]


def fingerprints() -> list[str]:
    return [_fingerprint(e) for e in CHANGELOG]


def unseen_entries(seen: list[str]) -> list[dict]:
    """Entries (new or changed) whose fingerprint the user hasn't seen yet."""
    seen_set = set(seen)
    return [e for e in CHANGELOG if _fingerprint(e) not in seen_set]
