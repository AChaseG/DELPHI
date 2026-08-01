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
        "date": "2026-08-19",
        "title": "You can end a session now — and one just ended",
        "items": [
            "🔒 Settings has a new “Sign out everywhere”. Signing out from the "
            "rail only forgets this browser, which is no help if you have lost "
            "a phone or think somebody else has been in your account. This one "
            "ends every signed-in device, including this one.",
            "🔑 Changing your password now signs out every device too. It "
            "didn't before: a password reset left anyone already signed in as "
            "you still signed in, for up to thirty days — which is precisely "
            "the situation people reset a password to end.",
            "📋 Live updates no longer carry your sign-in credential in the "
            "web address. It used to sit in the URL of the updates connection, "
            "where every server between you and Delphi writes it into a log; "
            "now that address holds a one-minute pass that opens the updates "
            "feed and nothing else.",
            "👋 Because of those changes everyone is signed out once. Sign back "
            "in as usual — nothing of yours is affected.",
        ],
    },
    {
        "date": "2026-08-18",
        "title": "The limit on password guessing now actually holds",
        "items": [
            "🔑 Delphi caps how many sign-in attempts one caller can make, to "
            "stop somebody working through passwords against your account. The "
            "cap was being read off a piece of the request the caller writes "
            "themselves, so anyone could change it each time and get an "
            "unlimited number of tries. It now identifies callers by something "
            "they cannot forge, and the cap holds.",
            "🧮 The same flaw undid every other limit — new accounts, password "
            "reset emails, address lookups, and manual refreshes. Those all "
            "hold again too.",
            "🙂 If you share an office or a VPN, you still get your own "
            "allowance: the fix distinguishes people behind a proxy rather "
            "than lumping them together.",
        ],
    },
    {
        "date": "2026-08-17",
        "title": "Two ways an account could reach too far, closed",
        "items": [
            "🛡 Delphi now only fetches from public internet addresses. Adding "
            "a source or an alert webhook asks the server to fetch a URL for "
            "you, and a server can reach places your browser cannot — the "
            "machine it runs on, and anything else on its private network. "
            "Those are refused now, with a message saying why, and the check "
            "is repeated on every redirect rather than only when you save.",
            "🔐 A handful of actions are operators-only: rebuilding events, "
            "re-classifying or re-detecting languages across the archive, "
            "backfilling article text, seeding the city catalog, and deleting "
            "a source. Each of those touches every article or every reader on "
            "the server, and any signed-in account could start them.",
            "⏸ Deleting a source is now an operator's job because it removes "
            "the outlet for everybody and takes its articles with it. "
            "Disabling one is still open to anyone, does the same thing to "
            "your board, and can be undone.",
            "⟳ Refresh stays open to everyone — it is the one thing you can do "
            "about a feed that looks stale. It is rate-limited instead.",
        ],
    },
    {
        "date": "2026-08-16",
        "title": "Delphi reads more of the world, more often",
        "items": [
            "🌍 The source catalog has gone from 86 feeds in 4 languages to "
            "148 in 23, across 59 countries. Delphi now reads the news in "
            "Arabic, Spanish, Portuguese, German, French, Russian, Ukrainian, "
            "Chinese, Japanese, Hindi, Indonesian, Vietnamese, Turkish, "
            "Persian, Swahili, Hausa, Dutch, Swedish, Norwegian, Danish and "
            "Finnish — where before it read almost everything in English and "
            "translated the rest.",
            "🗞️ Brazil, Colombia, Chile, Taiwan, the Philippines, Indonesia, "
            "Vietnam, Malaysia, Bangladesh, Nepal, Kenya, Tanzania, Ethiopia, "
            "Ghana, the Netherlands, Sweden, Norway, Denmark, Finland, "
            "Ireland, Greece, Austria, Portugal and Lebanon all have national "
            "outlets of their own now, rather than being seen only through an "
            "aggregator.",
            "⏱️ Wires refresh every 3 minutes instead of 5, and city feeds "
            "every hour instead of every 90 minutes. That is affordable "
            "because polling is now conditional: Delphi remembers what each "
            "publisher last sent and asks only for what has changed, so an "
            "unchanged feed answers in a few hundred bytes with nothing to "
            "read. Ten polls of an unchanged feed moved 4.9 KB where they used "
            "to move 48.8 KB.",
            "📄 Article bodies now catch up. A busy minute brings more stories "
            "than one cycle can fetch the full text of, and the rest used to "
            "stay headline-only forever — which quietly cost you matches, "
            "since alerts and searches read the body. The leftovers are picked "
            "up on the quiet minutes that follow, newest first.",
            "🔎 Delphi hunts harder for outlets' own feeds. When it sees a "
            "publisher it doesn't know, it goes looking for their feed rather "
            "than continuing to read them second-hand — now eight at a time "
            "per cycle instead of three, and in parallel rather than one after "
            "another.",
        ],
    },
    {
        "date": "2026-08-15",
        "title": "Take a column with you, and read means read",
        "items": [
            "⤓ Every column can now leave as a file. <b>📊 Excel</b> gives a "
            "workbook with the header frozen and filters already on; <b>📄 "
            "Word</b> gives a readable brief with a heading per story, so "
            "Word's navigation pane lists the headlines. There is also CSV for "
            "anything, Markdown for a wiki or a report, and JSON for a script. "
            "The button is ⤓ in a column's header, and it works on Home's "
            "columns and a Pantheon's too.",
            "An export reaches back further than the column shows — up to 500 "
            "articles against the forty on screen — and it takes what the feed "
            "matches now, in your reading language.",
            "🔅 Articles that had been read were coming back bright. The "
            "reading was recorded on the server and on the row you clicked, "
            "but not on the same story sitting in other columns, and not in "
            "the saved contents of any column — so a reload, or switching "
            "between boards, undid it. One article turned out to be cached in "
            "five columns with only one of them dimmed. Reading a story now "
            "dims every copy of it, everywhere, and it stays that way.",
            "An article that belongs to no story could never be marked read at "
            "all, however many times you opened it. Now it can.",
        ],
    },
    {
        "date": "2026-08-14",
        "title": "Errors that tell you what went wrong",
        "items": [
            "⚠ Every failure now names the thing that failed. \"Request failed\" "
            "has become \"Couldn't load a feed's articles\", \"Couldn't save your "
            "settings\", \"Couldn't open the story\" — and each status the server "
            "can return says what to do about it, so a refusal, a rate limit "
            "and a restart no longer read identically.",
            "🔖 When Delphi hits a bug, it writes the failure to the server log "
            "with a short code and shows you the same code. Quote it and "
            "whoever runs the server can find that exact failure with its full "
            "stack trace. A message like that always means nothing was saved.",
            "⚡ If the live connection drops, a banner now says so. While it is "
            "down alerts cannot reach your tab and the board stops refreshing "
            "itself — which used to look exactly like a quiet news day. Brief "
            "drops stay silent; the banner appears only when reconnection keeps "
            "failing, and clears itself the moment it works.",
            "🔓 Being signed out mid-session now says why — an expired session, "
            "a suspended account, or a deleted one are three different things "
            "and used to look like one.",
            "✉️ A password reset that fails to send says so, instead of "
            "reporting that a link is on its way. Operators get a Service "
            "health panel in the console naming which mail setting the relay "
            "objected to, and whether address lookup is working.",
            "📍 A failed place search explains itself in the suggestion list "
            "rather than showing nothing, which was indistinguishable from "
            "\"no such place\".",
        ],
    },
    {
        "date": "2026-08-13",
        "title": "A tidier rail, and a Troubleshooting tab",
        "items": [
            "🛠 Help has a third tab. <b>Troubleshooting</b> starts from the "
            "symptom — an empty column, an alert that never fired, a source "
            "with a red dot, a search finding less than you expected — because "
            "when something is wrong you don't yet know whether you have a "
            "how-to question or a what-is-it one. It opens straight from "
            "⚙ Settings.",
            "⟳ <b>Refresh</b> has moved off the rail and into that tab. It "
            "polls every wire at once, which is worth doing when a story is "
            "breaking — but sitting among the everyday buttons it read as "
            "something you had to press for news to arrive, and you never did: "
            "Delphi polls on its own, continuously.",
            "🏛 The rail is in a more sensible order — Pantheons, Locations, "
            "Alerts, then Sources — and its ‹ button now sits at the panel's "
            "edge instead of floating in the middle of it.",
            "🛠 The <b>Operator console</b> has moved into ⚙ Settings, under its "
            "own heading. It was on the rail among things everyone uses, "
            "despite almost nobody being able to see it.",
            "↔ Columns are now resized by their <b>right edge only</b>. With a "
            "handle on each edge, the line between two columns carried two of "
            "them doing opposite things, and grabbing the wrong one moved the "
            "neighbour instead.",
        ],
    },
    {
        "date": "2026-08-12",
        "title": "A speed pass over the whole of Delphi",
        "items": [
            "📦 Everything Delphi sends is now compressed on the way to your "
            "browser. Opening the dashboard moved 689 KB and now moves 177 KB — "
            "the same board, a quarter of the data. Away from wi-fi that is most "
            "of the wait. Live alerts are deliberately left uncompressed so they "
            "still arrive the instant they fire.",
            "📰 Grouped columns — the ones that gather several outlets into one "
            "story — were asking for 200 articles and being answered by a search "
            "that only ever looked at the newest 400. An earthquake feed's event "
            "view went from 220ms to 52ms, and a whole six-column board now "
            "answers in 141ms.",
            "⏱️ The poller keeps up with a busy news minute. Working out which "
            "other outlets are carrying a story, and which story an article "
            "belongs to, both meant comparing each new headline against every "
            "recent one — about seven seconds of work when 800 articles land at "
            "once, on the same machine that was trying to serve you pages. Both "
            "now look only at headlines sharing a word with the new one: the "
            "same answers, in about a second.",
            "🔍 Searching for an everyday word — earthquake, strike, election — "
            "took well over a second on a full archive, while an unusual word "
            "came back instantly. That was backwards, and it was the search "
            "index's doing: asked for a word a third of the archive contains, it "
            "gathered every single one before sorting them by date. Delphi now "
            "checks how much the index would actually narrow things down, and "
            "when the answer is \"barely\", it reads the newest articles "
            "directly instead. Measured on 244,000 articles, a search for "
            "\"earthquake\" went from 1.4 seconds to 10 milliseconds.",
            "🎯 A written search keeps its shape when it reaches the index. "
            "(tokyo OR osaka) AND earthquake used to be handed over as just "
            "\"earthquake\" — technically enough, but it meant sifting 78,562 "
            "articles instead of the 5,174 that could possibly match.",
            "Nothing about what a search finds has changed. Both routes hand the "
            "same candidates to the same matcher, and a match older than "
            "anything the quick route reads still falls back to the index, so "
            "everything that was findable stays findable.",
        ],
    },
    {
        "date": "2026-08-11",
        "title": "Alert hits open reliably, and every button answers",
        "items": [
            "🔔 Clicking a hit in the alerts panel sometimes did nothing at all. "
            "Every alert that fires rebuilds that panel, and if the rebuild "
            "landed between pressing a hit and letting go, the row was no longer "
            "there — at which point the browser fires no click whatsoever, so "
            "nothing could act on it. The panel now leaves its rows alone when "
            "nothing about them has changed, and a press whose row is replaced "
            "mid-click still opens the story it was aimed at.",
            "👆 Every button, chip and row now answers a press immediately, "
            "before whatever it does has begun — a click that starts work on the "
            "same tick could otherwise leave the browser no chance to show "
            "anything. Buttons that go on to wait for the server keep the "
            "spinner they had, and a few that were missing it (Pantheons, the "
            "operator console, signing out, saving a source) have it now.",
            "🪄 The Boolean search builder has been removed. Writing a search by "
            "hand is unchanged, and every row still tells you whether what you "
            "have typed is valid.",
        ],
    },
    {
        "date": "2026-08-10",
        "title": "The location search finds addresses now",
        "items": [
            "📍 Typing in the Favourite Locations search suggests places as you "
            "go. Delphi's own list of about 480 cities and 154 countries answers "
            "first, instantly — and anything it doesn't know is now looked up "
            "through OpenStreetMap and listed underneath, marked 📍. A street "
            "address, a town, a district or a neighbourhood all find something "
            "where they used to find nothing.",
            "Each suggestion says where it is, so the two Springfields are told "
            "apart at a glance. ↓ and ↑ move through the list, Enter takes one, "
            "Esc puts it away, and clicking the map still works for a spot with "
            "no name at all.",
            "The lookup is made by Delphi's server, never by your browser, so the "
            "outside service sees Delphi rather than you — and it only happens "
            "when Delphi's own list has no good answer, so typing a city name "
            "still never leaves this server.",
        ],
    },
    {
        "date": "2026-08-09",
        "title": "Stories open the moment you click them",
        "items": [
            "⚡ A headline used to wait on the server before anything appeared — "
            "on a 300ms connection, a third of a second of nothing. The view now "
            "opens from what the column already has, so the headline, the "
            "summary, the outlet and the time are on screen immediately and the "
            "rest of the story fills in behind them. Measured with 300ms of "
            "latency: 333–354ms before a story was readable, now 4–10ms. "
            "Pointing at a headline starts fetching it, and a story you reopen "
            "comes straight back.",
            "🔔 Alert hits sometimes did nothing when clicked. Every alert that "
            "fires re-renders the alerts panel, and the panel emptied itself "
            "before asking the server for its hits — so for as long as those "
            "requests took, there was nothing under the pointer. Measured "
            "directly: six hits on screen, zero during a re-render. The panel now "
            "keeps what it is showing until the new list is ready to replace it "
            "in one go.",
        ],
    },
    {
        "date": "2026-08-08",
        "title": "A headline opens the story, not the outlet",
        "items": [
            "📖 Clicking a headline used to send you straight to the publisher's "
            "website. It now opens the story inside Delphi: the headline, which "
            "outlet published it and exactly when, the summary in full rather "
            "than the truncation a column has room for, an extract of the "
            "article itself, the places it names, and its importance. Going to "
            "the outlet is a marked button at the bottom — so a stray click can "
            "no longer take you somewhere you had not decided to go.",
            "🧵 A story several outlets are carrying is not a different kind of "
            "thing, so it is no longer a different view. The same view carries "
            "the map, every report on the story newest-first with the one you "
            "are reading marked, the outlets covering it and related stories — "
            "and clicking another report moves to it in place. A grouped card "
            "opens the same view at the latest report.",
            "This applies everywhere a headline appears: the boards, search "
            "results and the alerts panel. Esc closes it, and rows now answer "
            "Enter and Space as well as the mouse.",
            "🔒 A paywalled outlet is marked on the row, and the way through — "
            "🔓 archive.ph — sits beside the outlet's own link in the story "
            "view, rather than being a second link inside the row.",
        ],
    },
    {
        "date": "2026-08-07",
        "title": "Home is ready before you open it",
        "items": [
            "🏠 The columns on Home are the same for every reader — fixed subjects "
            "over the same pool of news — but they were worked out from scratch "
            "each time somebody asked for them, so the first person to open "
            "Delphi paid for the whole board. They are now worked out as the "
            "news arrives, by the same background job that collects it, and "
            "simply handed over when you sign in. On a database of 250,000 "
            "articles the Home board went from 772ms to 56ms.",
            "This changes nothing about what you see: your language, which "
            "stories you have already opened, and your staleness setting are "
            "still applied to your request alone. Only the search itself is "
            "shared, and it is the same search for everybody.",
        ],
    },
    {
        "date": "2026-08-06",
        "title": "Delphi opens about four times faster",
        "items": [
            "⚡ Startup used to be a queue: settings, then the source catalog, "
            "then the country list, then your places, then your Pantheons, then "
            "your feeds — six round trips deep before a single column could be "
            "drawn. Delphi now asks for all of it at once and paints your board "
            "from the copy on your own computer before any answer comes back. "
            "On a connection with 150ms of latency, the first column appeared "
            "after 1,083ms and now appears after 274ms.",
            "📚 The catalog of every source Delphi polls — close to half a "
            "megabyte — is no longer downloaded when you sign in. Startup takes "
            "just the outlet names it needs to label a feed, and the full "
            "catalog arrives the first time you open the Sources panel or the "
            "wizard's source picker. Opening Delphi now transfers 54 KB of data "
            "instead of 489 KB.",
            "🗺 The map library is fetched the first time you actually open a "
            "map — drawing an area in the wizard, favourite locations, the "
            "alerts map, or an event's map — rather than on every visit. That is "
            "another 225 KB nobody has to wait for.",
            "📱 On a phone, every dialog now takes the height of the screen and "
            "no more: the title bar and the button along the bottom stay put "
            "while the text between them scrolls. What's-new used to put its "
            "“Got it” button below the fold, where you could only reach it by "
            "scrolling past the whole release note. The sign-in card scrolls "
            "too when a phone is held sideways, and buttons everywhere are "
            "finger-sized whichever way the phone is turned.",
        ],
    },
    {
        "date": "2026-08-05",
        "title": "The board stops freezing while it draws",
        "items": [
            "Putting a full board on screen locked the interface for about "
            "four tenths of a second on a mid-range laptop — thirteen columns "
            "of forty articles built in one go, with clicks and scrolling "
            "ignored until it finished. The rows a reader can actually see now "
            "go in immediately and the rest follow in small batches, so nothing "
            "blocks long enough to feel. Measured on a throttled CPU: the "
            "longest frozen moment fell from 440ms to 160ms, and a board "
            "settles in 141ms instead of 715ms.",
        ],
    },
    {
        "date": "2026-08-04",
        "title": "Delphi remembers your board between visits",
        "items": [
            "💾 The columns you have read are now kept on your own computer, so "
            "reopening Delphi paints the whole board from local storage before it "
            "asks the server for anything. Measured on an eight-feed board: "
            "reloading used to cost fifteen requests and now costs none. The news "
            "you last saw also stays readable when the server is unreachable.",
            "That copy is bounded, belongs to your account alone — signing in as "
            "someone else on a shared computer never shows your news — and is "
            "erased when you sign out.",
            "📍 Your browser now works out which articles are near a favourite "
            "location, instead of the server doing it for every request. Adding a "
            "location badges the articles already on screen with nothing fetched, "
            "and grouped (event) feeds get the badges too, which they never did "
            "before.",
            "🕰 The staleness threshold is applied in the browser as well, so "
            "changing it re-filters the board instantly rather than re-querying "
            "every column. A feed that hides everything it matched now says so, "
            "instead of claiming it matched nothing.",
        ],
    },
    {
        "date": "2026-08-03",
        "title": "Keyword feeds stop timing out",
        "items": [
            "⚡ Feeds and searches that match on words are 5–7× faster. Serving a "
            "page of forty articles was making the database sort twenty thousand "
            "rows first, even though only forty were ever read — the cost of a "
            "page had nothing to do with the page and everything to do with a "
            "limit set far too high. The search now widens only when a page "
            "can't be filled, so nothing that used to be findable stops being "
            "findable. Measured on 250,000 articles: a common keyword went from "
            "1.9s to 0.37s, a phrase from 2.0s to 0.38s, and a boolean search "
            "from 1.9s to 0.35s.",
            "This is what was behind “Failed to load” on a column: keyword feeds "
            "cost roughly a hundred times more server time than any other kind, "
            "so on a busy machine they were the ones that ran past the "
            "thirty-second limit.",
            "Every response now reports how long the server took, and the server "
            "logs any request over three seconds. One slow path means an "
            "expensive feed; all of them means the machine needs more CPU.",
        ],
    },
    {
        "date": "2026-08-02",
        "title": "One column for every place you watch",
        "items": [
            "📍 Favourite locations now share a single feed instead of each "
            "creating one. Watching a dozen places used to mean a dozen columns "
            "and no room for anything else; they are now one “📍 Favourite "
            "Locations” column carrying news near any of them. Existing "
            "per-location feeds are merged automatically the first time the "
            "server starts, keeping every area — nothing to redo, and no "
            "location's coverage is lost. Deleting a location narrows the feed "
            "rather than removing it; only the last one takes it away.",
            "Fixed the last feed column being cut off on wide screens. The "
            "decorative pillar sits inboard of the action rail, and the board "
            "reserved too little room for both, so 40px of the rightmost column "
            "stayed under the pillar with no scroll left to reach it. The board "
            "now scrolls far enough to bring it fully into the open, at any "
            "window width and any column width.",
        ],
    },
    {
        "date": "2026-08-01",
        "title": "Boards that are already loaded when you get there",
        "items": [
            "⚡ Once the board you're looking at has finished loading, Delphi "
            "loads the others behind it, so switching between 🏠 Home, 📋 My "
            "feeds and a 🏛 Pantheon is instant instead of a wait. The "
            "background pass never starts before the visible board is done, "
            "runs one request at a time where the visible board gets two, and "
            "stops the moment you switch — the board you're actually reading "
            "always has the server to itself.",
            "Switching back and forth no longer re-queries columns that were "
            "loaded seconds ago, which was the largest avoidable load the "
            "dashboard produced. New articles still arrive by themselves, and "
            "⟳ on a column, ⟳ Refresh, saving a feed, and changing the "
            "staleness threshold all re-query immediately.",
            "⟳ Refresh now refreshes your columns even when it finds a poll "
            "already in progress; it used to report that and leave the board "
            "alone.",
            "Fixed a Pantheon board not picking up newly arrived articles "
            "between renders — the live refresh was reloading the account's own "
            "feeds regardless of which board was on screen.",
        ],
    },
    {
        "date": "2026-07-31",
        "title": "A proper manual",
        "items": [
            "📖 Help is now two tabs. “How to” is instructions — step by step, for "
            "every part of Delphi, from creating an account to running the operator "
            "console — and “FAQ” is what things are and why they behave as they do. "
            "It used to be one list where every question dragged its instructions "
            "along with it. Open it from ⚙ Settings → Help; it still appears by "
            "itself on a first visit.",
            "New sections cover what each marker on an article row means, what to "
            "do with an empty board, resizing and rearranging columns, restricting "
            "a feed to chosen outlets, working through alert hits, and the keyboard "
            "shortcuts.",
            "A pass over every message in the system fixed wording that no longer "
            "matched what the software does — the wizard's review step was leaving "
            "drawn map areas out of its summary entirely, and a feed watching "
            "several areas showed no badge for them.",
        ],
    },
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
