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
        "date": "2026-07-30",
        "title": "Columns you can resize, and feeds you can point at one outlet",
        "items": [
            "↔ Feed columns resize: drag either edge to make a column as wide or "
            "narrow as you like, double-click an edge to reset it, or use ⇔ to "
            "flip between the standard and wide widths. Widths save to your "
            "account and follow you between devices — and a column you resize on "
            "a Pantheon board changes only for you, not for the group.",
            "📡 Feeds and alerts can be restricted to particular outlets. The "
            "picker in the wizard's Refine step is searchable — by name, country, "
            "language, platform or scope — with a “select shown” that takes "
            "everything currently matching, so you can grab a whole group at once. "
            "A restricted feed says so in its header.",
            "➕ Adding a source by hand is now its own dialog instead of a strip "
            "squeezed into the Sources panel, with room for the language and "
            "categories fields, and it explains why a save was rejected instead of "
            "failing quietly.",
            "Switching between Home, My feeds, and a Pantheon no longer blanks the "
            "columns after the first few: every column paints the news it already "
            "has straight away and refreshes behind it.",
            "The board's horizontal scrollbar now disappears the moment there are "
            "too few columns to scroll, instead of lingering until the board had "
            "finished reloading.",
        ],
    },
    {
        "date": "2026-07-29",
        "title": "Favourite locations, and feeds that watch several areas",
        "items": [
            "📍 Locations: keep a list of places you care about. Find one by "
            "name or drop a pin on the map, set a radius, and anything reported "
            "inside it is flagged 📍 wherever it appears — in every feed and "
            "alert, not just its own. Each location also gets a feed of its "
            "own automatically, and you can share a location with a Pantheon "
            "so it flags news for the whole group. Place lookup uses Delphi's "
            "built-in gazetteer, so nothing about the places you watch is sent "
            "to an outside service.",
            "Feeds and alerts can now hold several map areas instead of one — "
            "draw as many as you need and they match as OR, so a single feed "
            "can watch three cities at once. Existing feeds keep working "
            "unchanged.",
            "Every feed column gained a ⟳ to refresh just that column, instead "
            "of re-polling every source.",
        ],
    },
    {
        "date": "2026-07-21",
        "title": "Pantheons outlive their founders",
        "items": [
            "Deleting an account no longer dissolves the Pantheons it owned. "
            "Ownership passes to the most senior remaining member — an existing "
            "admin first, otherwise the longest-standing member — and the feeds "
            "and alerts that person had shared stay on the group's board under "
            "the new owner. Only a Pantheon with nobody else left in it is "
            "closed. Inherited alerts keep firing for the group but drop the "
            "departed member's email/webhook delivery, so notifications never "
            "get redirected to someone who didn't set them up.",
        ],
    },
    {
        "date": "2026-07-20",
        "title": "Operator console",
        "items": [
            "Operators can now manage every account from a new 🛠 Admin panel: "
            "search all users, force-verify an email, grant or revoke operator "
            "access, suspend or reinstate an account, reset a locked-out user's "
            "password, and delete an account with all its feeds, alerts, and "
            "pantheons. Operators are designated with the NEWS_ADMIN_USERS "
            "secret (a built-in operator that survives losing the database) or "
            "promoted from within the console — no admin password is baked into "
            "the code, and guards prevent removing the last operator or locking "
            "yourself out.",
        ],
    },
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
            "Mobile & accessibility pass: the layout now fits phone screens "
            "(toolbar wraps, one feed column fills the screen and you swipe "
            "between them, panels and dialogs go full-width) with no sideways "
            "scrolling, larger tap targets, visible keyboard focus, and screen-"
            "reader labels on icon-only buttons.",
            "Faster, more complete search: keyword and boolean feeds/searches "
            "now use a full-text index, so they scan the whole retention window "
            "efficiently instead of only the newest couple-thousand articles — "
            "rare terms in older stories are no longer missed.",
            "Geotagging and maps now cover ~480 major world cities (up from "
            "170), including native-script names — so filtering or drawing a "
            "box around Porto, Surabaya, 東京 or القاهرة surfaces their coverage.",
            "Cross-language event clustering: a story's coverage in its own "
            "language now groups with the English coverage instead of forming a "
            "separate event, using language-invariant anchors (canonical city "
            "names + significant numbers).",
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
