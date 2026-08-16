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
        "date": "2026-09-22",
        "title": "Typhon: what is burning, on the map, while it burns",
        "items": [
            "🐉 <b>Typhon</b> is a new layer on 🗺 Atlas — named for the "
            "storm-giant, beside Atlas and the Pantheons — carrying live "
            "wildfire incidents from <b>NIFC</b>, the federal interagency "
            "feed. Switch it on from the layers button in the map's top-right "
            "corner. Every incident is a dot sized and coloured by how much of "
            "a problem it is; press one for its acreage, its containment, "
            "where it is and what started it.",
            "🇺🇸 It covers the <b>United States</b>, which is why the layer "
            "says so in its own name, and it is off until you switch it on. "
            "Worldwide disasters — earthquakes, floods, cyclones, volcanoes — "
            "arrive separately through GDACS, as ordinary news you can feed, "
            "filter and alert on.",
            "📈 The point of a live layer is that it is live. A fire that "
            "grows from 200 acres to 40,000 is the <i>same</i> incident with a "
            "new number, so Typhon keeps a record it can rewrite rather than a "
            "story it can only add to — which is what will let a watched place "
            "tell you a fire near it got worse, rather than that one exists.",
            "🧯 Deliberately quiet about itself: the layer is off by default, "
            "asks the server only for the rectangle you are looking at, and "
            "asks nothing at all while it is switched off. Hazards are kept "
            "out of your feeds, out of search and off 🏠 Home — a fire is not "
            "a story, and mixing the two would make Delphi worse at both.",
        ],
    },
    {
        "date": "2026-09-21",
        "title": "Global disaster alerts, from the source the responders use",
        "items": [
            "🌍 <b>GDACS</b> — the Global Disaster Alert and Coordination "
            "System, run jointly by the UN and the European Commission — is "
            "now one of Delphi's sources. Earthquakes, floods, tropical "
            "cyclones, volcanoes and droughts, worldwide, with the severity "
            "colour the humanitarian world actually works from.",
            "🧭 It arrives as ordinary news, which means everything already "
            "built for news works on it on day one: put it in a feed, exclude "
            "it from one, match it with a boolean query, catch it with an "
            "alert, or let it fall inside one of your watched places and "
            "appear on 🗺 Atlas. Nothing new to learn.",
            "🔕 It is filed under <b>disaster</b> and weighted low on purpose, "
            "so it colours in the map without pushing wire copy off the front "
            "of 🏠 Home. If you would rather not see it at all, switch it off "
            "in 📡 Sources like any other.",
        ],
    },
    {
        "date": "2026-09-20",
        "title": "A pass over the whole of it, and what the pass turned up",
        "items": [
            "🧹 A read-through of every file, looking for the things tests do "
            "not catch: work that is done and then thrown away, rows left "
            "behind by a delete, and sentences that stopped being true. What "
            "follows is what it found.",
            "🗑 <b>Deleting a Pantheon cleaned up after itself; the other two "
            "ways one ends did not.</b> When the last member leaves, or when "
            "the owner's account is deleted, the Pantheon closes — and the "
            "locations shared into it were being left in the database, which "
            "is the bug that was fixed a month ago arriving through a "
            "different door. All three paths now go through the same cleanup.",
            "🧾 <b>A deleted account now really is deleted.</b> Its saved "
            "places, its read history and its device registrations were "
            "staying behind: unreachable, so nothing looked wrong, but a list "
            "of where somebody lives is not a thing to keep after they have "
            "asked to be forgotten — and it was travelling in every nightly "
            "backup. The news source each saved place kept pointed at it goes "
            "too, instead of being polled forever for a place nobody watches.",
            "🤝 Locations shared into a Pantheon now pass to whoever inherits "
            "it when the sharer leaves, the way a shared feed or alert "
            "already did. They belong to the group.",
            "⚡ <b>The Pantheons board costs the same whether you are in one "
            "or in twenty.</b> It was running five database queries per tile "
            "and three per invitation; it now runs four, total. The public "
            "directory was doing the same thing for up to two hundred "
            "entries.",
            "🎟️ The sign-up form only offers the <b>invitation code</b> field "
            "where there is something to be excused from. On an instance that "
            "charges nobody — the default — it was offering a code described "
            "as meaning “you never pay”, which is a promise about a bill "
            "nobody was going to get. Where payment <i>is</i> on, the card now "
            "says what it costs before you sign up rather than after.",
            "🛠 Smaller: an error path in Pantheon deletion would have crashed "
            "instead of reporting itself; two dialogs did not tell a screen "
            "reader their own names; a test had been silently shadowing "
            "another and neither was running the check it described; and a "
            "note in these very release notes still said the Alerts and "
            "Locations fold-outs were below their feeds, a week after they "
            "moved to the top.",
        ],
    },
    {
        "date": "2026-09-19",
        "title": "The pop-out rail is gone; everything it held has a home",
        "items": [
            "🧭 The strip of buttons pinned to the right edge has been taken "
            "apart, and each thing it held put where it belongs. Nothing was "
            "dropped.",
            "🏛 <b>Pantheons is a board</b> now, on the tab bar beside 🏠 Home, "
            "📋 My feeds and 🗺 Atlas — and it is laid out as the two questions "
            "it answers. Against the left wall, in a narrow column, the ways "
            "in: <b>New Pantheon</b> and the public directory. Across a "
            "hairline, with the rest of the window to itself, a wall of the "
            "ones you are already in.",
            "🧹 A Pantheon no longer takes a tab of its own — fine at three, a "
            "scrolling mess at a dozen. Open one from its button on the "
            "Pantheons board; a strip above its columns says which one you are "
            "on, who is in it and what it is for, with <b>← Pantheons</b> to go "
            "back and <b>Manage</b> to its right.",
            "🎴 Each of those is a <b>button carrying what it is</b>: name, "
            "description, how many members, how many shared feeds and alerts, "
            "and whose it is. Press it to open its board; <b>Manage</b> in its "
            "corner is still there for members and settings. A list of bare "
            "names told you nothing and made you open each one to find out.",
            "⚙ <b>Settings sits in the top-right corner</b>, with the account "
            "button 👤 to its right, in the corner itself. The four counters — "
            "articles, countries, sources, unseen alert hits — moved into "
            "Settings under <b>This instance</b>, and <b>📡 Sources</b> opens "
            "from there too. They are a report on how Delphi is doing rather "
            "than something you act on, and they were taking a row off the top "
            "of every board.",
            "🔍 Search and <b>＋ Create</b> are in the header, where they are "
            "always visible instead of behind a pop-out.",
            "📐 The board gets the whole window back. On a phone the header "
            "wraps to its own rows rather than hiding things behind a toggle.",
        ],
    },
    {
        "date": "2026-09-18",
        "title": "Atlas: five base maps, and two tabs that are columns",
        "items": [
            "🗺 The map can be read on <b>five different grounds</b> now — "
            "Street, Muted, Night, Terrain and Satellite — from the layers "
            "button in its top-right corner. A city block, a coastline and a "
            "mountain range are not best read on the same map, and on a dark "
            "dashboard at night the plain street map is the brightest thing on "
            "the screen. Your choice is remembered on this browser.",
            "📰 Both tabs are proper columns now, and each is only its own "
            "subject. <b>🔔 Alerts</b> is a feed of what your alerts have found "
            "inside the view, newest first, with the button that makes another "
            "alert. <b>📍 Locations</b> is a feed of what has been reported "
            "inside the places you watch, with the button that adds another "
            "one. Managing either is a fold-out at the top of its tab.",
            "✂️ Before this the alerts tab grouped its hits under your watched "
            "places — half of one tab living on the other — and the locations "
            "tab had nothing on it but a form. Neither was the whole of "
            "anything.",
            "🛝 The pop-out rail is now only on 🏠 Home and 📋 My feeds. On "
            "Atlas it covered the map, and the pane already does its job.",
            "🔤 Fixed, and overdue: the What's-new popup was showing the markup "
            "in these notes as literal text — “was taking &lt;b&gt;78 "
            "seconds&lt;/b&gt;”. Thirteen entries were affected. It renders the "
            "emphasis properly now.",
        ],
    },
    {
        "date": "2026-09-17",
        "title": "Atlas — a whole board that is a map",
        "items": [
            "🗺 There is a third board beside 🏠 Home and 📋 My feeds: "
            "<b>Atlas</b>. One map, filling the screen, carrying both the "
            "places you watch and every alert hit Delphi has geolocated. "
            "🔔 Alerts and 📍 Locations have moved into it — the rail buttons "
            "still take you straight to each of them, they just land on the "
            "map now.",
            "🎯 Beside the map is a pane that only ever shows <b>what the map "
            "is looking at</b>. Pan to the Aegean and it lists the alerts that "
            "have fired around the Aegean; pan away and they go. It groups them "
            "by the watched place they landed inside, because “an alert fired” "
            "and “an alert fired inside the harbour you watch” are two "
            "different pieces of news. Anything inside the view but outside "
            "every watched place is still listed, under <i>Elsewhere in "
            "view</i> — the pane narrows what you see, it never hides it.",
            "🔔📍 The pane has two tabs, one per thing: <b>Alerts</b> and "
            "<b>Locations</b>. Each carries its whole subject — what is inside "
            "the view, everything else, and the button that makes another one. "
            "Creating an alert is on the Alerts tab; creating a watched place "
            "is on the Locations tab, where the form appears when you pick a "
            "point and puts itself away once it is saved.",
            "🔭 There used to be two maps and neither could show the other's "
            "layer: one inside Locations for picking a place, one inside Alerts "
            "that was off by default. So the obvious question — <i>did that "
            "alert fire somewhere I care about?</i> — could not be asked at "
            "all. Now the stars and the hits are on the same picture.",
            "📱 On a narrow window the map and the pane stack instead of "
            "squeezing, so both stay readable on a phone.",
        ],
    },
    {
        "date": "2026-09-16",
        "title": "Subscriptions, and invitations that skip them",
        "items": [
            "💳 Delphi can now charge for access. The operator sets the price, "
            "the currency, monthly or yearly, and how long a free trial runs — "
            "all from 🛠 <b>Operator console → Access &amp; payment</b>, so "
            "changing what it costs never needs a deploy. Payment goes through "
            "Stripe's own hosted checkout: no card details, and no Stripe "
            "code, ever touch this page.",
            "🎟️ Invitations are the other half. An operator can mint a code "
            "that gives whoever redeems it <b>free access, permanently</b> — "
            "for one person or for many, with an expiry or without. The console "
            "copies a link that fills the code in; whoever opens it registers "
            "and never sees a paywall. Codes can be revoked, which stops "
            "further use without evicting anyone who already used one.",
            "🕊️ <b>Every account that existed before this shipped keeps free "
            "access, permanently.</b> Turning payments on must not take Delphi "
            "away from the people already using it, so it doesn't.",
            "⏳ New accounts get the trial and a quiet reminder in its last few "
            "days. Nothing else interrupts: the notice can be dismissed, and it "
            "never appears again once there is a subscription.",
            "🧾 What you paid for is what you keep. Access runs against the "
            "date a period is paid to, not against a status flag — so a card "
            "that fails on a Tuesday, a webhook that goes missing, or an outage "
            "at Stripe cannot cut anybody off mid-period. Cancelling works the "
            "same way: it stops the next payment, and everything keeps working "
            "until the period ends. Cancelling is done from Stripe's own page, "
            "reachable from ⚙ Settings.",
        ],
    },
    {
        "date": "2026-09-15",
        "title": "intitle:, intext:, source:, site: — and AROUND(5)",
        "items": [
            "🎯 The operators every research guide lists are implemented, and "
            "they are the sharpest tool for a search that keeps collecting the "
            "wrong thing: <code>intitle:solar</code> matches only the headline, "
            "<code>intext:solar</code> only the summary and body, "
            "<code>source:Reuters</code> the publication, and "
            "<code>site:bbc.co.uk</code> the site it was published on — with "
            "<code>-site:example.com</code> to exclude one. Phrases work too: "
            "<code>intitle:\"solar farm\"</code>.",
            "🔭 An energy search that keeps finding football can now say "
            "<b>intitle:solar OR intitle:wind OR intitle:\"natural gas\"</b>. A "
            "story that is <i>about</i> something says so in its headline, so "
            "this is the strictest filter available short of naming the "
            "outlets.",
            "📏 <code>AROUND(5)</code> is Google's spelling of "
            "<code>NEAR/5</code>, and both work now. It used to parse as “this "
            "AND the number 5 AND that”, so it quietly demanded the digit 5 be "
            "in the article.",
            "🚫 Here is the part worth knowing: those four operators were "
            "already <i>accepted</i>. They just did nothing — "
            "<code>intitle:sleep</code> became a single literal word that no "
            "article contains, so the feed sat empty and nothing said why. "
            "Every operator now either works or refuses. "
            "<code>~synonyms</code> refuses, and says to write "
            "<code>(academic OR scholarly)</code> instead, because there is no "
            "thesaurus here to expand it.",
            "🔒 Ordinary terms still read the article and nothing else — never "
            "the outlet's name, the web address or the section it was filed "
            "under. <code>source:</code> and <code>site:</code> are the only "
            "way to reach those, and they are, because you typed them.",
        ],
    },
    {
        "date": "2026-09-14",
        "title": "A word said once is not what an article is about",
        "items": [
            "🪶 Reported with a link and a query: an energy search — solar OR "
            "wind OR coal OR nuclear… — kept collecting a vote on the worst "
            "football kit of the month. Nothing was broken. A kit colourway is "
            "called <b>Solar Yellow</b>, so the page really does contain the "
            "word, and the search was right to say so. The same thing turned up "
            "before as a concert listing, because one of the songs is called "
            "“Innocent Wind”.",
            "🔍 No dictionary of word senses can fix that — “solar” means "
            "whatever the sentence around it means. So Delphi now asks how "
            "<b>prominent</b> a word is, which is the judgement you make "
            "skimming a page: a bare word counts if it is in the headline or "
            "summary, or if it is said at least twice, or if another of your "
            "terms turns up too. One mention, buried in the body, on its own, is "
            "a passing mention and no longer matches.",
            "💬 Quoted phrases are exempt. Somebody who wrote “shale gas” has "
            "already been specific, and holding a phrase to the same bar would "
            "punish the very thing the query advice asks for.",
            "🎚️ There is a switch, per feed: <b>🪶 Keep passing mentions</b> in "
            "the builder restores the old behaviour for a search that is "
            "deliberately hunting rare one-line mentions.",
            "❓ And “why is this here” now answers in full. Open a story from "
            "one of your own feeds and the line under it names the term, says "
            "where it was found and how many times, and <b>quotes the sentence "
            "it sits in</b> — which used to be a hover tooltip, and so was "
            "invisible on a phone, which is where the question gets asked.",
        ],
    },
    {
        "date": "2026-09-13",
        "title": "The outlet counts for as much as it used to again",
        "items": [
            "📊 Importance scoring gives the source's reach its old weight back: "
            "<b>international 45, national 35, local 25</b>, plus 8 for a major "
            "wire and minus 4 for a niche or local feed. That is 32 points "
            "decided by who is carrying a story, where it had been narrowed to "
            "15.",
            "🎚️ The narrowing was done to stop the wires owning Home's headline "
            "columns, and it did — but it also flattened the thing every feed "
            "and alert filters on. With every outlet scoring within a few points "
            "of every other, a minimum-importance floor could no longer tell "
            "“an outlet worth reading has this” from “something on the internet "
            "has this”, and feeds let through material you did not ask for. A "
            "filter that discriminates is worth more than an evenly-spread front "
            "page.",
            "⚖️ The cost, stated plainly, because it is the same one as before: "
            "routine wire copy now sits two points under <b>Top stories</b>, so "
            "that column will lean towards the big outlets again, and a single "
            "local report of something serious has to earn its way up through "
            "breaking signals or corroboration — <b>+8 for every other outlet "
            "carrying the same story</b>, which is what lifts a local scoop onto "
            "the front page.",
            "🔧 If a feed is now too quiet or too loud, its "
            "<b>minimum importance</b> slider is the dial that changed meaning: "
            "the same number admits noticeably less from small and newly "
            "discovered outlets than it did yesterday.",
        ],
    },
    {
        "date": "2026-09-12",
        "title": "A deleted Pantheon that kept coming back",
        "items": [
            "🏛 Reported as a Pantheon that would not stay deleted. The Pantheon "
            "itself did go — that was checked end to end, empty and fully in "
            "use, through the browser and not just the API. What came back was "
            "a <b>shared location</b>: sharing a pin into a Pantheon makes a "
            "copy of it, and deleting the Pantheon cleaned up its shared feeds, "
            "shared alerts, members and invitations while leaving that copy "
            "behind. It sat on your Locations list on every load, badged as "
            "shared with something that no longer existed, and there was no way "
            "to remove it because the thing it belonged to was already gone. "
            "The copy is now deleted with everything else, and your own pin is "
            "untouched.",
            "🧾 Deleting also proves itself now, on both sides. The server "
            "checks the Pantheon is actually gone before answering, and the "
            "page re-reads your list before it removes the card — so a delete "
            "that fails says so, instead of looking like a success that undoes "
            "itself the next time you load the page.",
            "🖱️ And the Delete button no longer depends on the panel having "
            "finished loading. It used to take the Pantheon's name from a "
            "second request, and if that request was slow or failed the button "
            "did nothing at all: no error, no request, nothing to see.",
            "📍 A related fix while in there: a location shared into a Pantheon "
            "was losing the place's real name and country on the way, so the "
            "same pin found less news on the Pantheon's board than on yours. "
            "Both now travel with it.",
        ],
    },
    {
        "date": "2026-09-12",
        "title": "Filters were matching the page, not the article",
        "items": [
            "🔍 A filter searches three things: the headline, the feed summary, "
            "and the article's text. That was checked end to end — nothing else "
            "on an article is searchable, not the web address, not the section "
            "it was filed under, and not the outlet's name. The words putting "
            "unrelated stories in your feeds were in those three fields; they "
            "just were not the article.",
            "🧹 Most of it was the page around the story. A newsletter promo, a "
            "“most read” box, a related-stories rail — all of them are words on "
            "the page, and enough of them were being stored as article text "
            "that a basketball report could satisfy a data-centre search. Three "
            "ways in are now closed: a block whose entire text is a link is a "
            "pointer to another page rather than prose, whichever tag it uses; "
            "an unclosed paragraph no longer means everything below it counts "
            "as the story, which is what let one omitted tag hand over an "
            "entire footer; and a page that gives no readable prose at all now "
            "yields nothing instead of its own navigation menu.",
            "📰 The other way in was the feed itself. Many publishers end every "
            "item with “The post … appeared first on <i>Their Name</i>”, so the "
            "publication's own name sat in every article's searchable text — a "
            "feed watching <b>energy</b> quietly collected everything Energy "
            "Voice published, on any subject. Those trailers, and the “Share "
            "this:” and “Filed under:” tails, are now removed before anything "
            "is stored or searched.",
            "🗺️ Also fixed: searching with two watched areas on the map "
            "returned an error rather than news.",
            "🕰️ One honest caveat — this cleans articles as they arrive. "
            "Stories already collected keep the text they were stored with, and "
            "the original pages are not kept, so there is nothing to re-read "
            "them from. The archive turns over in about a week, so the last of "
            "it clears on its own.",
        ],
    },
    {
        "date": "2026-09-11",
        "title": "One number was taking 78 seconds to count",
        "items": [
            "⏱️ The sign-in page was timing out again — and this time it was a "
            "single number. Of the five figures across the top of the "
            "dashboard, four are counted in under a third of a second each. The "
            "fifth, how many countries the news came from today, was taking "
            "<b>78 seconds</b>. The browser gives up at thirty.",
            "🗂️ The cause was an index that stopped just short. Finding "
            "today’s articles was quick, but the country was not stored "
            "alongside the date, so working out which countries they were meant "
            "opening 170,000 articles one at a time inside a seven-gigabyte "
            "file. The two are now filed together, which is the whole fix: "
            "twenty-five times faster, and the answer comes from the index "
            "without touching the articles at all.",
            "🔢 And the numbers are no longer counted while you wait. They are "
            "recounted on a timer in the background, so opening the dashboard "
            "reads a figure that is already there. Before, they were cached for "
            "a minute and whoever arrived the moment the minute was up paid the "
            "entire cost — which is why this struck at random. Loading the "
            "dashboard now takes about a hundredth of a second where it was "
            "taking over a minute.",
            "🧯 The trade, stated plainly: for a few seconds after a restart "
            "the five figures read zero until the first count finishes. A "
            "dashboard briefly showing zeros is worse than one showing the "
            "truth, and far better than one that will not load.",
        ],
    },
    {
        "date": "2026-09-10",
        "title": "Half a logo on the sign-in page",
        "items": [
            "🖼️ On a phone with a weak signal the sign-in page showed the top "
            "of the crest and nothing else — no name, no tagline, just an empty "
            "space where the rest should be. Nothing was broken: the artwork is "
            "drawn from the top down as it downloads, it was a 372 KB file, and "
            "what you were looking at was however much of it had arrived.",
            "⏳ It is now held back until the whole picture is there, so you "
            "either see the crest or you see nothing — never half of it. The "
            "space it occupies is reserved either way, so the form no longer "
            "shifts when it appears.",
            "🪶 And it is less than half the weight it was, which is the part "
            "that matters on a bad connection: on a slow link it now completes "
            "in around seven seconds instead of twenty-five. The picture itself "
            "is unchanged — a smaller version was tried and rejected because it "
            "flattened the soft edges the crest needs to sit on the card in "
            "either light or dark mode.",
        ],
    },
    {
        "date": "2026-09-09",
        "title": "Seven papers, one newsroom, one vote",
        "items": [
            "🔎 The question was whether the catalogue is full of outlets that "
            "just repost other people’s work. Measured over twelve hours and "
            "105,118 stories: not really. Only 0.4% of what arrives comes from "
            "outlets that are mostly reposting, because identical links are "
            "already thrown away as they come in.",
            "📰 But the ones that do repost turned out not to be random. They "
            "come in families — four Glacier Media titles in British Columbia, "
            "seven Spanish papers, four Dutch ones — one newsroom writing under "
            "many local mastheads. Each has its own website, so nothing "
            "recognised them as the same story, and each was counted as a "
            "separate outlet confirming it. One newsroom looked like a "
            "consensus, and stories were scored as more important than they "
            "were.",
            "⚖️ Mastheads sharing a newsroom now corroborate a story once "
            "between them rather than once each, so “widely covered” means what "
            "it says. The families are worked out from what outlets actually "
            "publish — the same copy under two names, several times over — and "
            "relearned daily, because publishing groups buy and sell titles.",
            "🏘️ Nothing has been switched off. These papers still break their "
            "own local news: one of them was first with 58% of what it ran. "
            "This changes how an outlet is counted, never whether it is read.",
        ],
    },
    {
        "date": "2026-09-08",
        "title": "A third of every hour was spent counting the same headlines",
        "items": [
            "🧊 The freezes were not the network and not the host this time. "
            "To work out how many outlets are carrying a story, D.E.L.P.H.I. "
            "builds an index of every recent headline — and it was rebuilding "
            "that index from scratch before every single collection round, on "
            "the one thread that also answers pages. Measured: 1,188 seconds of "
            "every hour, which is a third of the time, doing nothing else.",
            "📐 It was designed for a window of about two thousand headlines. "
            "Nothing capped the window, so as the catalogue grew the window grew "
            "with it — to roughly 350,000, a hundred and seventy times what it "
            "was built for. The index is now capped, looks back twelve hours "
            "instead of forty-eight, is kept between rounds rather than thrown "
            "away, and is built off to one side so a slow moment is no longer a "
            "site that will not load. Same work, 0.4 seconds an hour.",
            "📡 The cause behind the cause: the catalogue had doubled in a day, "
            "from 4,655 outlets to 9,421, because nothing ever said how many was "
            "enough. It now stops adopting new outlets at 12,000 — the number it "
            "can read through inside an hour, which is what makes a feed worth "
            "watching. Nothing is switched off or removed to fit, and outlets "
            "you added yourself are never counted against the limit.",
            "⚖️ One deliberate trade: corroboration now looks back twelve hours "
            "rather than two days, so a story being carried by many outlets over "
            "a longer stretch may score slightly lower than before. Twelve hours "
            "is what “others are covering this now” actually means.",
        ],
    },
    {
        "date": "2026-09-07",
        "title": "Found it: the nightly outage was the backup",
        "items": [
            "🎯 The “can’t reach the server” screen "
            "that has been appearing every night was not D.E.L.P.H.I. failing. "
            "Once a day, at the same minute, the host was making a backup copy "
            "of the entire news archive — and reading six and a half "
            "gigabytes off the disk left nothing for reading a headline. The "
            "server stayed perfectly healthy the whole time, which is exactly "
            "why five rounds of looking for a bug found nothing: by the time "
            "anyone refreshed and checked, it was over.",
            "📈 It appeared out of nowhere and got worse because it "
            "grew with the archive. Three nights at under 4.7 GB passed "
            "unnoticed; the night it reached 5.6 GB the site was down for 85 "
            "minutes, and at 6.5 GB for four hours. Tonight would have been "
            "worse again.",
            "🛠️ The nightly copy is now switched off, so the "
            "outage stops tonight and no news is lost. The trade is that "
            "there is no automatic backup any more: a day of news costs about "
            "0.84 GB, and keeping enough history to be useful is not "
            "compatible with copying all of it every night on this machine.",
            "🧹 Also fixed, before it could bite: shortening the "
            "retention window would have deleted every article past the new "
            "cutoff in one go, on the first tick after the change — the "
            "same hours-long freeze, caused by the setting meant to prevent "
            "it. Clean-up now works in bounded batches and comes back for the "
            "rest a minute later.",
            "💾 Because there is no automatic backup any more, there is now a "
            "deliberate one — of the small part. Every day D.E.L.P.H.I. now "
            "emails the operator a file holding the things the feeds cannot "
            "give back: accounts, columns, alerts, pantheons, watched places "
            "and outlets added by hand. Not the news, which is why it is "
            "kilobytes rather than gigabytes, and why it can be sent daily "
            "when copying the whole archive could not be. It goes by email on "
            "purpose: a copy kept on this server is no help if this server is "
            "what goes missing. There is a <b>Download account backup</b> "
            "button in the operator console for taking one on the spot, and "
            "the date of the last one is shown beside it — a backup that "
            "quietly stopped is the only kind that ever matters.",
            "🩹 And a quiet one, found while testing the above: the "
            "what’s-new popup you are reading had stopped appearing entirely a "
            "few days ago. Three of these entries were written in a way the "
            "server could not process, so the request that decides what to show "
            "you failed every time the app loaded. Nothing else was affected, "
            "and nothing was lost — it simply went silent. Fixed, and now "
            "tested so it cannot go silent again.",
        ],
    },
    {
        "date": "2026-09-06",
        "title": "Keeping a record of the moments it goes quiet",
        "items": [
            "📝 Every time D.E.L.P.H.I. has been unreachable, the "
            "evidence was gone before anyone could look \u2014 the server\u2019s "
            "log holds about a minute, and by the time the page is refreshed "
            "and someone checks, it is healthy again. Twice that meant a real "
            "cause was found and fixed while the rest of it stayed hidden.",
            "🕒 It now keeps its own record instead: whenever the "
            "server is unable to answer for more than a moment, it writes down "
            "when, for how long, and what it was doing. The last forty are kept "
            "and shown in the operator console, so a report of \u201cit was "
            "down around four\u201d can be checked against what actually "
            "happened at four.",
            "\u2699\ufe0f Also corrected: yesterday\u2019s clean-up of "
            "non-news sources switched off the Amazon Web Services feed, "
            "because the rule written for the shop matched everything beneath "
            "it. AWS is a legitimate source of technology news and the rule was "
            "too broad. It has been narrowed; re-enable the source in "
            "📡 Sources.",
        ],
    },
    {
        "date": "2026-09-05",
        "title": "Why a concert listing turned up in an energy feed",
        "items": [
            "🎫 A feed watching for solar, wind, coal and nuclear "
            "carried a Taiwanese concert listing. Nothing had gone wrong with "
            "the search: the page lists the performer\u2019s songs and one of "
            "them is called \u201cInnocent Wind\u201d, so the article really "
            "does contain the word. A bare word finds every use of it, and "
            "most uses of these words are not about energy.",
            "\u26a0\ufe0f The query builder now says so while you type. A term "
            "like <b>wind</b> or <b>nuclear</b> on its own gets an amber note "
            "suggesting the pairing that usually fixes it \u2014 \u201cwind "
            "power\u201d, \u201cwind farm\u201d, \u201cnuclear reactor\u201d. "
            "It is advice, not an error; the search still saves exactly as "
            "written.",
            "🧹 The same rules are now applied to sources already "
            "collected, not just new ones \u2014 once when D.E.L.P.H.I. starts "
            "and once a day after that. Anything the current rules would "
            "refuse is switched off, never deleted, and says in the Sources "
            "panel why. Sources you added yourself are left alone.",
            "🚫 And the source should never have been in the catalog. "
            "It was a ticketing calendar that D.E.L.P.H.I. adopted by itself "
            "because it publishes a feed. Ticketing and event platforms are "
            "now refused outright \u2014 including everything beneath them, "
            "which the old list missed \u2014 and a newly seen publisher has "
            "to turn up more than once before it is considered at all. A real "
            "outlet keeps appearing in the news; a ticket calendar, or the "
            "funeral home that also got in, does not.",
        ],
    },
    {
        "date": "2026-09-04",
        "title": "Chasing the last of the \u201ccan\u2019t reach the server\u201d",
        "items": [
            "\u26a1 Moving to a dedicated processor worked, and the numbers are "
            "worth stating: one round of collecting the news went from six "
            "minutes nineteen seconds to seventy-six, and the most expensive "
            "stage \u2014 working out what each article is about \u2014 from 143 "
            "seconds to under five. Pages that were taking four to seven "
            "seconds are no longer waiting on it.",
            "🧩 But a dedicated processor is one processor, where the "
            "shared pair were two. Collecting the news and answering you now "
            "take turns on the same one, and while the average has plenty of "
            "room, a burst can still keep you waiting. So the work is now done "
            "in half-size pieces, and D.E.L.P.H.I. hands the processor back "
            "five times more often than it used to between them.",
            "🔍 Two suspects were measured and cleared rather than "
            "guessed at \u2014 reading a feed, and running a search \u2014 which "
            "is why neither was \u201cfixed\u201d. The stage timings added "
            "earlier are what made that possible.",
        ],
    },
    {
        "date": "2026-09-03",
        "title": "Operators can see and limit where an account is being used",
        "items": [
            "📱 The operator console now shows how many devices each account is "
            "being used on <b>right now</b> — which is not the same as how many "
            "it is signed in on. A sign-in lasts thirty days, so counting those "
            "would report a phone somebody used once last month. This counts "
            "devices something has arrived from in the last five minutes. Close "
            "the laptop and it stops counting shortly after.",
            "💻 <b>Devices</b> on any account lists what those devices are — "
            "\"Chrome on Windows (desktop)\", \"Safari on iOS (mobile)\" — "
            "marking which are in use now and when the rest were last seen. "
            "Four tabs on one laptop count as one laptop.",
            "🚦 <b>Device limit</b> caps how many an account may be used on at "
            "once. Each account can have its own, or follow a server-wide "
            "default (NEWS_DEVICE_LIMIT). There is no limit at all unless one "
            "is set, so nothing changes for anyone until you choose it.",
            "✉️ Somebody who hits the limit is told so plainly and offered an "
            "emailed link that signs the account out of every device, so a lost "
            "or replaced phone can't lock its owner out until the count lapses. "
            "An operator can do the same from the console for anyone who cannot "
            "receive the mail.",
            "🔒 Devices are recognised by an identifier this browser stores for "
            "itself, not by fingerprinting. Clearing site data or using a "
            "private window therefore looks like a new device — that is the "
            "honest limit of the approach, and the console says so.",
        ],
    },
    {
        "date": "2026-09-02",
        "title": "Stories arrive already translated",
        "items": [
            "🌍 A column of foreign-language reporting was taking seconds to "
            "appear, and the reason was the translation: a story is translated "
            "the first time somebody looks at it, so whoever opened the board "
            "first paid for every story on it, one at a time, before the page "
            "could be drawn. Forty stories meant dozens of waits.",
            "⏳ D.E.L.P.H.I. now translates the stories on the front page "
            "between collection rounds, before anyone opens it — so by the "
            "time you look, the English is already there and the page just "
            "loads. It only translates into languages an account here has "
            "actually chosen, so nothing is spent on the fifteen it isn't "
            "reading in.",
            "⚡ And when a story does still have to be translated while you "
            "wait, its headline and summary are now fetched at the same time "
            "rather than one after the other, which halves that wait.",
            "🈶 Deliberately unchanged: you are never shown the original "
            "language first with the English following behind. Seeing Mandarin "
            "sooner is no use to a reader who wants English — the wait has "
            "been moved off your request, not moved onto your screen.",
        ],
    },
    {
        "date": "2026-09-01",
        "title": "Why the dashboard kept saying it couldn't reach the server",
        "items": [
            "🧾 The stage timings added yesterday found it on their first "
            "reading. One round of collecting the news was taking six minutes "
            "and nineteen seconds and starting again fifteen seconds later, so "
            "the server was never once idle — and the request for the "
            "dashboard's headline numbers was measured at 31.8 seconds, past "
            "the half-minute a browser waits before declaring a server "
            "unreachable. Nothing was broken. It was simply too busy to answer "
            "in time, which looks identical from the outside.",
            "🖥 D.E.L.P.H.I. now runs on a dedicated processor rather than a "
            "shared one. Reading a headline takes about three thousandths of a "
            "second; on the shared processor, under sustained load, it was "
            "taking forty times that, because that kind of processor is "
            "deliberately slowed down when something uses it continuously — "
            "which is exactly what watching a thousand news sources does.",
            "⏱ And each round of collection now takes on about half as much at "
            "a time. This is not less news: what a source publishes is decided "
            "by the publisher, and checking it more often simply finds fewer "
            "items each time. The same articles arrive through a narrower door, "
            "which is what leaves the machine room to answer you.",
            "📊 The headline numbers themselves — articles stored, countries "
            "covered, sources answering — were being counted from scratch for "
            "every reader on every page load, including a count of every "
            "article ever collected. They are the same for everyone and they "
            "describe something that changes over minutes, so they are now "
            "worked out once a minute and shared.",
        ],
    },
    {
        "date": "2026-08-31",
        "title": "The site going unreachable for a minute at a time",
        "items": [
            "🚦 Several times a day D.E.L.P.H.I. stopped answering entirely — "
            "not slow, not partly broken, simply unreachable for a minute or "
            "more, then fine again with no sign anything had happened. The "
            "cause was its own news collection. Working out where a story is "
            "about, what it is about and what language it is in takes about a "
            "sixth of a second per article once the full text is in hand, and a "
            "collection round does that for up to three hundred articles in one "
            "unbroken run — on the same single thread that serves the site. For "
            "that stretch nothing else could be answered, including the "
            "automated check that decides whether the app is alive, so the host "
            "concluded it was dead and stopped sending anyone to it.",
            "🧵 That work now happens alongside serving the site rather than "
            "instead of it, and in small batches, so the site stays answerable "
            "throughout a collection round. It is the same amount of work and "
            "takes the same total time; it no longer holds everything else up "
            "while it runs.",
            "🧹 The six-hourly clear-out of old articles — deleting, compacting "
            "and reclaiming disk — was doing the same thing, and has had the "
            "same treatment. It was rarer and so easier to miss, but each one "
            "was another window where the site could not be reached.",
            "🧩 Grouping the new articles into events and checking them against "
            "your alerts was the last piece, and it needed the same fix: a busy "
            "minute brings eight hundred articles and doing all of them in one "
            "go was still enough to lock everything else out for half a minute. "
            "It now happens in batches as well. Which stories end up grouped "
            "together is unchanged — that is checked by its own tests, because "
            "a change there would have been an easy thing not to notice.",
            "🔍 And each collection round now records how long each of its "
            "stages took. Every one of these stalls had to be worked out from "
            "where the log went quiet, which only finds the stages that write "
            "to the log at all. If it happens again the log will say which "
            "stage, in seconds.",
        ],
    },
    {
        "date": "2026-08-30",
        "title": "The feed editor opens straight away",
        "items": [
            "✎ Pressing edit on a feed or an alert used to show nothing at all "
            "— no window, no spinner — until the list of every outlet on the "
            "server had been fetched. That is most of a megabyte across some "
            "1,200 outlets, and it got slower every time Delphi discovered "
            "another one, on a button that ought to be instant. On a hotel "
            "wifi connection it was taking the better part of half a second "
            "before anything appeared.",
            "⚡ The editor now opens immediately and the outlet list catches up "
            "behind it — measured at about three-hundredths of a second, and "
            "the same whether the connection is fast or slow, because nothing "
            "in opening it waits on the network any more.",
            "📋 The one control that does need the list — “only these outlets”, "
            "on the last step — says it is still loading rather than sitting "
            "there looking empty, and says so plainly if the list cannot be "
            "fetched at all. Every other part of the editor works throughout, "
            "and once the list has loaded it stays loaded, so opening a second "
            "feed is instant end to end.",
        ],
    },
    {
        "date": "2026-08-29",
        "title": "The board uses your whole monitor now",
        "items": [
            "🖥 Columns were a fixed width, so a wider screen bought you more "
            "empty background rather than more news. On a 3440-pixel monitor "
            "seven columns left 1,140 pixels of it, and on a 32:9 ultrawide "
            "2,820 — over half the screen. Columns now widen to share out "
            "whatever is left, up to a point where a headline still reads like "
            "a column rather than a page.",
            "↔️ Past that point the space that cannot be used is split evenly "
            "on both sides, so a board with only a few columns sits in the "
            "middle instead of huddling against the left edge.",
            "📐 A column you have sized yourself is left exactly as you set it "
            "— it takes no part in the sharing out, so a board you have "
            "arranged stays arranged.",
            "🔍 Everything else was checked at 1920, 3440 and 5120 pixels wide "
            "— the sign-in page, the feed editor, help, settings, sources, "
            "alerts, locations and Pantheons — and nothing overflows, is cut "
            "off, or stretches a line of text across the whole screen.",
        ],
    },
    {
        "date": "2026-08-28",
        "title": "Why a feed carried a story with none of its words in it",
        "items": [
            "🧹 A page carries more than its story: a section menu, a “More "
            "from” rail, a newsletter promo. Those were being stored as part "
            "of the article, and searches read the whole article — so a report "
            "about the WNBA, carried on a site with a “Data Center” section "
            "link and a “data center boom” promo beside it, genuinely "
            "contained the words “data center” and turned up in a data-centre "
            "feed. Those parts of a page are no longer taken for article text. "
            "Real bulleted lists inside a story are kept.",
            "⚠️ The builder now warns when an exclusion covers less than you "
            "probably meant. AND binds tighter than OR, so “a OR b NOT c” "
            "means “a OR (b AND NOT c)” — c is only excluded from the second "
            "alternative. Bracket the alternatives, “(a OR b) NOT c”, to "
            "exclude from all of them. It is a note in amber, not an error; "
            "the search still saves.",
            "✅ The Boolean engine itself was checked against ordinary logic on "
            "48,000 cases and agrees on every one, so a query does mean what "
            "it says. That check now runs with the tests.",
            "🔎 And a story now tells you why it is in a column. Open one from "
            "a feed of your own and it says which of that feed's words are in "
            "it and where — “matched “AI industry” in the article body” — with "
            "the surrounding sentence on hover. When nothing in the headline "
            "or summary put it there, it says so in amber, because that is the "
            "case worth a second look.",
            "🕓 The clean-up applies to articles collected from now on. "
            "Anything already stored keeps the text it was stored with until "
            "it ages out — so a result you are suspicious of today can be "
            "checked with the line above.",
        ],
    },
    {
        "date": "2026-08-27",
        "title": "The side panels open again",
        "items": [
            "⚡ Pantheons, Locations, Alerts, Sources and the feed editor were "
            "taking a very long time to open, or never opening at all. The "
            "cause was the source catalog: it is half a megabyte now that the "
            "catalog has passed a thousand outlets, and it was rebuilt from "
            "scratch for every request. Building it holds the server "
            "completely, so while one request was doing it, every other one "
            "waited — including panels that have nothing to do with sources. "
            "It is now built once and handed out. Measured on a "
            "1,200-source catalog: ten requests at once went from 2.9 seconds "
            "each to 58 milliseconds, and forty at once from never finishing "
            "to 173 milliseconds.",
            "🕓 The Sources panel can now show a source's poll status up to "
            "fifteen seconds late as a result. Sources are polled minutes "
            "apart so there is nothing to see in that window — and anything "
            "you change yourself still appears immediately.",
        ],
    },
    {
        "date": "2026-08-26",
        "title": "A thousand sources should not read like thirty",
        "items": [
            "🏛 Which stories reach 🏠 Home no longer depends so much on who "
            "reported them. A story's importance started from its outlet's "
            "reach — and started so far apart that a wire's most routine item "
            "outscored a local paper's report of people killed. Top stories "
            "and Breaking now were reserved for about thirty international "
            "wires no matter how many outlets were added underneath them, "
            "which is why the board looked the same as the catalog grew past a "
            "thousand. Reach still counts; it is no longer worth more than "
            "what happened.",
            "🚫 A source that fails five polls in a row is taken out of the "
            "rotation. Nothing used to remove one, so a feed that went dead "
            "was re-requested every few minutes forever. If it never carried a "
            "single story it is deleted; if it published, it is retired "
            "instead — no longer polled, but everything it gathered is kept "
            "and you can put it back if the feed returns.",
            "🕸 The Sources panel now says which feeds answer but never carry "
            "anything. Search it for “silent” to list them, or “retired” for "
            "the ones that have stopped. ⚙ Settings → Service health counts "
            "both, so you can see how much of the catalog is really working.",
        ],
    },
    {
        "date": "2026-08-25",
        "title": "Take a Focus away, and drag your columns where you want them",
        "items": [
            "📤 A Focus can now be exported. Open any story and press ⭳ Export "
            "for the whole of it — the report you opened first, then every "
            "other outlet carrying the same story — as a Word brief, a "
            "spreadsheet, CSV, Markdown or JSON.",
            "↔️ Columns are moved by dragging them. Grab a column anywhere on "
            "its header and slide it; the board opens a gap where it will "
            "land, so you can see where you are putting it before you let go. "
            "This replaces the ◀ ▶ buttons, which moved a column one place per "
            "press and never showed you where it was going. ← and → still move "
            "the focused column one place, for anyone not using a pointer.",
            "🖐 Holding a dragged column against either edge of the board "
            "scrolls it, so you can move a column to the far end even when the "
            "far end is off screen.",
            "⏱ Panels no longer wait behind the board. Loading a column used "
            "to hold the whole server while it worked, which is why 📍 "
            "Favourite Locations could sit there and then give up with “the "
            "server didn't respond”. Columns are now worked on beside "
            "everything else rather than in front of it.",
            "💾 Correcting how the last release shipped: on a database made "
            "before Delphi set this up at creation, handing deleted news back "
            "to the disk needs a one-off rewrite of the whole file — and that "
            "was being done automatically on the first poll after a deploy. "
            "The site is unresponsive while it runs, minutes on a large "
            "archive, so doing it unannounced at a moment nobody chose was "
            "wrong. It is now a button in ⚙ Settings → Service health, which "
            "says what it will cost before you press it.",
        ],
    },
    {
        "date": "2026-08-24",
        "title": "Favourite locations actually carry news now",
        "items": [
            "📍 If your 📍 Favourite Locations column has been empty, this is why: "
            "a story only counted as being near your location if it named one "
            "of the ~600 cities Delphi recognises by position. Anywhere smaller "
            "— a town, a district, an address, a spot on the map — could never "
            "match anything, however long you waited. It now also recognises "
            "the place by name, so watching somewhere small works.",
            "📰 Saving a location now starts gathering news about it, instead of "
            "only filtering what was already being collected. Rename the place "
            "and the search follows; delete the location and it stops.",
            "🏷 The name you give a location is yours — “Home”, “Dad's house”. "
            "Delphi remembers separately what the place is actually called, and "
            "that is what it searches for, so your own label never has to be a "
            "sensible search term.",
            "🔎 The column also looks further back than an ordinary feed. A "
            "watched place gives the search index nothing to work with, so it "
            "reads deeper instead — four times as far as before.",
        ],
    },
    {
        "date": "2026-08-23",
        "title": "Delphi now clears out old news properly, and says when it's tight",
        "items": [
            "🧹 Old articles were already being deleted after 30 days — but the "
            "space never came back. A database keeps its size once it has grown, "
            "reusing the room inside instead of returning it, so the archive "
            "stayed as large as its busiest week forever. It now hands the space "
            "back as it clears out.",
            "📏 There's also a size limit, not just an age one. Thirty days of a "
            "busy month is far more than thirty days of a quiet one, so if the "
            "archive outgrows its disk the oldest stories are dropped to fit. "
            "Nothing from the last couple of days is ever dropped this way.",
            "🔖 Fixed a leak of my own making: the marks that dim stories you've "
            "already read were kept forever, including for stories long since "
            "deleted. They're cleared with the story now.",
            "💾 Operators get a disk line in Service health, and if space runs "
            "genuinely low Delphi pauses collecting rather than filling the last "
            "of it — it keeps serving what it has while it clears room.",
        ],
    },
    {
        "date": "2026-08-22",
        "title": "Delphi now turns down the passwords attackers try first",
        "items": [
            "🚫 New passwords are checked against the ten thousand most common "
            "ones and refused if they match — including the padded versions, "
            "so “password”, “Password123” and “P@ssw0rd1” all get the same "
            "answer. Existing passwords still work; this applies when you set "
            "a new one.",
            "🎯 The reason is narrower than “be more secure”: most break-ins "
            "are not somebody guessing your password, they are somebody "
            "replaying an address and password taken from a different site "
            "that was breached. A password nobody else has used cannot arrive "
            "that way.",
            "💡 A few unrelated words — quartz-heron-lantern — are accepted and "
            "are far stronger than a short one with symbols in it. So is "
            "anything a password manager generates.",
            "🙅 Also refused: passwords containing your own username or email "
            "name, and ones made only of digits.",
        ],
    },
    {
        "date": "2026-08-21",
        "title": "Change your password without leaving the app",
        "items": [
            "🔑 ⚙ Settings → Account security → Change password. Your current "
            "password, then the new one twice. No email needed, so it works "
            "even where the server can't send any — before this, the only "
            "route was the “Forgot password?” link, which is really meant for "
            "someone locked out.",
            "🛡 It asks for your current password even though you're already "
            "signed in. That is the point of it: being signed in isn't proof "
            "you're the owner, and without the check, a minute at your "
            "unlocked screen would be enough for someone to take the account.",
            "📱 Your other devices are signed out when it works. This one "
            "isn't — you just proved you know the password.",
        ],
    },
    {
        "date": "2026-08-20",
        "title": "Sign-up stops confirming who has an account here",
        "items": [
            "📧 Creating an account with an email address that already has one "
            "now gets the same answer as any other address, and a note goes to "
            "that address instead — with the username on it and a link to set a "
            "new password. Before, the form said “an account with that email "
            "already exists”, which let anyone with a list of addresses find "
            "out which of them read news here.",
            "🙋 Usernames still say when they're taken. They have to be unique, "
            "and a shared feed already shows who shared it, so there is nothing "
            "there to protect.",
            "🛡 The page now tells your browser to run only scripts and styles "
            "that came from Delphi. Nothing on screen changes; it means a "
            "script smuggled into a headline would have nowhere to run.",
            "⏱ Search and export now have request limits, like signing in "
            "already did. Ordinary reading is nowhere near them — exporting is "
            "the one held tightly, since a single export can be two thousand "
            "articles and translations of all of them.",
        ],
    },
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
