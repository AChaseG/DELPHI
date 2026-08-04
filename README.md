# 🏛 D.E.L.P.H.I.

**D.E.L.P.H.I.** — **D**igital **E**xploration and **L**ayout for **P**ublicly **H**arvested **I**ntelligence — is a self-hosted global news monitoring dashboard: it continuously ingests news from sources across the globe —
local, national, and international — and presents them on a customizable,
per-user dashboard with Dataminr-style live alerting and **draw-on-map geofenced
feeds**.

## What it does

- **Social media ingestion.** Alongside the press, Delphi pulls the social
  platforms that publish open feeds: **Reddit** (news subreddits + per-topic
  search feeds), **Bluesky** (newsroom account feeds), **Mastodon** (hashtag
  feeds), and **YouTube** (news channel uploads). Feeds and alerts can filter
  by platform (e.g. "social only" or "press only"). A per-topic social tracker
  creates Reddit-search + Mastodon-hashtag sources for any query. X/Twitter,
  Facebook, and Instagram expose no open feeds (closed or paid APIs), so they
  can't be ingested; the Sources panel lets you add any feed URL if you have
  one from another platform or an RSS bridge.
- **Manage sources in the UI.** Add any RSS/Atom feed with platform, scope,
  and country; edit everything about an existing source in place (name, URL,
  platform, scope, country, language, categories); enable/disable or delete;
  per-source health dots show the last fetch status.
- **Global ingestion.** A background engine polls a curated catalog of 148
  RSS/Atom feeds in 23 languages spanning every continent — wire-level internationals (BBC, Al
  Jazeera, DW, France 24, UN News, ReliefWeb…), national outlets (Times of
  India, Dawn, CBC, ABC AU, Mail & Guardian, Times of Israel…), and local ones
  (Texas Tribune, Gothamist, Manchester Evening News…). Add any RSS URL as a
  new source, or create a **topic tracker** that ingests worldwide coverage of
  any query via Google News search RSS.
- **Enrichment.** Every article is normalized, deduplicated, **geotagged**
  (countries + major cities from a bundled gazetteer), **auto-categorized**
  into standard news categories, and given an **importance score (0–100)** that
  blends source reach, breaking-news signals, geographic breadth, and
  cross-source corroboration (similar headlines from distinct outlets).
  **Full-text matching:** the article page itself is fetched and its body text
  extracted, so feed criteria, alerts, geotagging, and categories see the whole
  story — not just the headline and feed snippet. Backfill stored articles with
  `POST /api/maintenance/fetch-content`.
- **Accounts required.** The system opens on a sign-in page; an account
  (username + **email** + password, sessions via signed tokens) is required to
  access anything. Sign in with username or email from any browser or device.
  Feeds and alerts are private per account; articles and sources are shared
  infrastructure — one ingestion pipeline feeds every user. Feeds are arranged
  in columns you can drag to reorder, widen, edit, and delete. Columns share
  out whatever width a monitor leaves over — from a 360px base up to 560px,
  with anything beyond that split evenly on both sides — so a wide or ultrawide
  screen carries more news rather than more background. A column sized by hand
  opts out and keeps the width it was given. The numbers live in
  `--col-base` / `--col-grow-max` in `frontend/css/styles.css`, and the
  arithmetic behind them is checked in `tests/test_wide_screen_layout.py`.
- **Home & My feeds.** The dashboard opens on **🏠 Home** — Delphi-curated
  columns generated live from everything ingested (top events, breaking now,
  conflict & disasters, politics, business, sci-tech, social pulse). Those
  columns are identical for every account, so the poller matches them as news
  lands rather than when someone asks (`backend/app/home.py`); a request is then
  a primary-key fetch, with language, read history and staleness still applied
  per reader. 📌 any Home column to copy it into **📋 My feeds**, your own
  editable panel.
- **Guided feed builder** — a four-step wizard (Topic → Where → Refine →
  Review) with per-step guidance, a criteria summary, and a live match-count
  preview before saving. Combinable criteria:
  - country (article country *or* any place mentioned in it)
  - standard news categories (politics, business, economy, technology, science,
    health, environment, conflict, disaster, crime, sports, entertainment, culture, world)
  - source scope (local / national / international) and language
  - free keywords + exclude-keywords
  - **user-written boolean queries** — `("supply chain" OR semiconductor) AND
    (china OR taiwan) NOT rumor` — with live validation; a feed can hold
    **several queries**, each running separately, all populating the same feed,
    each validated as you type. AND binds tighter than OR, so `a OR b NOT c`
    excludes `c` from the second alternative only; the builder flags that in
    amber as an advisory (`/api/query/validate` returns it), because it is
    usually not what was meant and occasionally exactly what was. The engine is
    checked against ordinary boolean logic over randomly generated expressions
    in `tests/test_boolean_query.py`.
  - **story focus** — clicking a headline never navigates away: it opens the
    story in the dashboard. One view whether one outlet has it or forty: the
    report you picked (summary in full, an extract of the body, outlet,
    publication time, places, importance) and, when others are carrying it, a
    map, the timeline of every report, the outlets covering it and related
    stories. The outlet's own page is a marked button inside it
  - **minimum importance** to the international community (Critical / High /
    Notable / Routine tiers)
  - recency window, specific sources, sort by newest or importance
- **Alerts (Dataminr-style).** An alert takes the exact same criteria as a
  feed. Every ingest cycle, new articles are evaluated against all active
  alerts; hits are stored, counted on the bell, and **pushed live to the
  dashboard over Server-Sent Events** as toast notifications. The alerts
  panel opens with a **live map of recent hits** — markers colored by
  importance tier (dimmed once seen), popups linking to the article, and your
  alert geofences drawn on top — plus **article thumbnails** pulled from each
  feed's enclosure/media images.
- **Event clustering.** Cross-source coverage of the same story is clustered
  into *events* (incremental headline-similarity clustering over a 72h rolling
  window). Any feed can be switched to grouped mode: one card per event with a
  🧵 report count, source count, and an expandable timeline of every outlet's
  coverage. `POST /api/events/rebuild` reclusters history from scratch.
- **Automatic translation.** Pick a reading language in the top bar (defaults
  to your browser's language) and any article in another language is machine-
  translated on the fly — titles and summaries, marked "🌐 translated from XX".
  Translations are cached in SQLite so each article+language pair is translated
  once. Providers: the free Google endpoint (default, fine for personal use) or
  a self-hosted LibreTranslate server (`NEWS_TRANSLATE_PROVIDER=libretranslate`,
  `NEWS_LIBRETRANSLATE_URL=…`), or `off`.
- **Settings (⚙).** Per-device preferences: dark/light/system theme, compact
  mode, reading language, timestamp format (relative, local, UTC, or military
  date-time group “112036Z JUL 26”), toast position, alert sound volume with a
  test button, and desktop notifications for alert hits while the tab is in
  the background.
- **Favourite locations** are watched by name as well as by position. An article
  is only given coordinates when its text names one of the ~600 cities in the
  bundled gazetteer, so a circle around anywhere smaller — a town, a district,
  an address — could never contain anything, while the picker happily offers
  any address OpenStreetMap knows. A watched place now carries the name it was
  picked under: matched in headlines (with the country disambiguating Reading,
  England from Reading, Pennsylvania), and asked of a news search so coverage
  is *gathered* rather than merely filtered for. The reader's label for the
  place is kept separate from the place's own name — "Dad's house" is a fine
  label and a useless search term. Renaming moves the source; deleting removes
  it, unless someone else watches the same place. A location feed also scans
  four times deeper than an ordinary one, because it has no text to hand the
  index (measured: 2,000 rows found 4 of 100 matches, 8,000 found 16, at ~28µs
  a row).
- **Draw on a map.** In the feed/alert builder, draw a polygon, rectangle, or
  circle on a world map (Leaflet + Leaflet.draw). Only news geolocated inside
  that area (via its tagged places, falling back to the country centroid) will
  match. Works for both feeds and alerts — e.g. "alert me on anything Critical
  inside this box around the Strait of Hormuz."

## Quick start

```bash
./run.sh                 # creates .venv, installs deps, starts on :8000
# then open http://127.0.0.1:8000
```

**No machine handy?** Open the repo in **GitHub Codespaces** (Code ▸ Codespaces
▸ Create codespace on this branch). The included devcontainer installs the
dependencies and starts the server automatically; when port 8000 is forwarded,
your browser opens the dashboard.

> **502 on the forwarded port?** The server isn't listening (it may still be
> installing on first boot, or the codespace was resumed without re-running
> hooks). Open a terminal in the codespace and run:
>
> ```bash
> bash .devcontainer/start.sh
> ```
>
> It (re)installs anything missing, restarts the server, waits until
> `/api/meta` responds, and prints the tail of `/tmp/delphi.log` if
> startup fails — then reload the forwarded-port tab. Note: browser-only sandboxes such as
bolt.new / StackBlitz cannot run this project — it is a Python server
application (FastAPI + SQLite + a background ingestion loop), not a Node
frontend, and news feeds must be fetched server-side (browser CORS blocks
cross-origin RSS fetches).

Or manually:

```bash
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.txt
.venv/bin/uvicorn backend.app.main:app --port 8000 --no-proxy-headers
```

On first run the source catalog is seeded and polling starts automatically —
each source then refreshes on its own cadence. Use **⟳** to poll on demand and
**Add starter feeds** for a ready-made dashboard layout. Everything on the
board is real reporting: Delphi generates no sample data, and any left behind
by an older build is removed at startup.

### Tests

```bash
pip install --require-hashes -r requirements-dev.txt
pytest
```

A fast, no-network suite (pure logic + in-process API via FastAPI's TestClient)
covers boolean search, language detection, multilingual scoring, criteria
matching, clustering, the rolling-poll scheduler, URL safety, and the auth /
settings / Pantheon-sharing flows. It runs against a throwaway database and
never touches `backend/data`. GitHub Actions runs it on every push
(`.github/workflows/ci.yml`).

### Dependencies

Delphi names five packages and installs twenty-six — the five, and everything
they pull in behind them. **`requirements.in` is the intent; `requirements.txt`
is the lock**, pinning every one of those twenty-six to an exact version and to
the hash of the file it must be. The image, CI, and the commands above all
install with `--require-hashes`, so a build gets precisely that set or stops.

Unpinned, a build collected whatever was newest on the day it ran: two builds
of one commit could differ, CI could pass against versions the deploy never
saw, and a compromised release anywhere in that tree would be installed on the
next deploy with nobody doing anything.

Changing a dependency means editing the `.in` file and recompiling both locks
together (they must agree, or CI tests versions the image does not run):

```bash
uv pip compile --generate-hashes --python-version 3.12 requirements.in -o requirements.txt
uv pip compile --generate-hashes --python-version 3.12 requirements-dev.in -o requirements-dev.txt
```

3.12 is the image's Python; resolving for a different interpreter pins what
*it* needs. `tests/test_dependency_lock.py` checks the locks still match the
`.in` files, that nothing is left unpinned or unhashed, and that both install
sites still enforce hashes.

Pinning alone would be the worse half of the trade — dependencies that stop
changing under you also stop receiving fixes — so `.github/dependabot.yml`
opens a pull request when something moves, and CI decides whether to take it.

## Going live

**→ See [DEPLOY.md](DEPLOY.md) for the full 24/7 hosting walkthrough.**

**Share it straight from a codespace (temporary).** With the server running,
open the **Ports** panel, right-click port 8000 → **Port visibility** →
**Public**. Anyone with the `https://<codespace>-8000.app.github.dev` URL can
open the sign-in page. Caveat: a codespace **auto-stops after ~30 minutes of
inactivity** (Settings ▸ Codespaces lets you raise the idle timeout to 4 hours)
and ingestion/alerts pause while it sleeps — so this is for demos, not hosting.
An account is required to use the app, so a public codespace URL only exposes
the sign-in gate, not your feeds.

**Host it 24/7 (Docker / Fly.io).** A `Dockerfile` and `fly.toml` are included;
the database lives on a volume at `/data` so accounts and history survive
restarts. The short version:

```bash
# Fly.io (config is in the repo)
fly launch --copy-config --no-deploy
fly volumes create newsdata --size 3
fly secrets set NEWS_SECRET=$(openssl rand -hex 32)   # stable login-token key
fly deploy

# …or any Docker host / VPS
docker build -t delphi .
docker run -d -p 8000:8000 -v newsdata:/data --restart unless-stopped \
  -e NEWS_SECRET=$(openssl rand -hex 32) delphi
```

Run a **single instance** (the scheduler, SQLite DB, and rate limiter are
in-process — scale up, not out), avoid free tiers that sleep on idle, and set
`NEWS_PUBLIC_URL` to your domain once you have one. Full details, email setup,
backups, and every tuning knob are in **[DEPLOY.md](DEPLOY.md)**.

### Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `PORT` / `HOST` | `8000` / `127.0.0.1` | bind address (run.sh) |
| `NEWS_FETCH_INTERVAL` | `180` | seconds between polls of a news wire |
| `NEWS_FETCH_CONCURRENCY` | `10` | parallel source fetches |
| `NEWS_DB_PATH` | `backend/data/news.db` | SQLite location |
| `NEWS_DISABLE_INGEST` | unset | `1` disables the background poller |
| `NEWS_CONTENT_FETCH` | `1` | fetch article bodies for full-text matching (`0` = headlines/summaries only) |
| `NEWS_CONTENT_MAX_PER_CYCLE` | `150` | article pages fetched per ingest cycle |
| `NEWS_ENRICH_CHUNK` | `25` | article bodies re-read per slice before the event loop is handed back |
| `NEWS_CLUSTER_CHUNK` | `50` | articles clustered and alert-matched per slice, same reason |
| `NEWS_POLL_BATCH` | `40` | non-city sources fetched per tick (was 80; a round was outlasting its own refresh interval) |
| `NEWS_CITY_PER_TICK` | `12` | city feeds per tick, which bounds the Google drip |
| `NEWS_TRANSLATE_CONCURRENCY` | `8` | articles translated at once; each issues title and summary together, so in-flight requests are about double this |
| `NEWS_DEVICE_LIMIT` | `0` | devices an account may be *in use on* at once (`0` = no limit); per-account overrides in the console |
| `NEWS_DEVICE_ACTIVE_WINDOW_S` | `300` | silence after which a device stops counting as in use |
| `NEWS_DEVICE_TOUCH_EVERY_S` | `30` | how often a device's last-seen time is actually written |
| `NEWS_TRANSLATE_PROVIDER` | `google` | `google` \| `libretranslate` \| `off` |
| `NEWS_LIBRETRANSLATE_URL` | unset | LibreTranslate server URL (with provider above) |
| `NEWS_REMOVE_AFTER` | `5` | consecutive failed polls before a source is retired or removed |
| `NEWS_GEOCODER` | `nominatim` | address lookup for places the gazetteer lacks; `off` keeps every lookup local |
| `NEWS_NOMINATIM_URL` | `https://nominatim.openstreetmap.org` | point at your own Nominatim instead |
| `NEWS_GEOCODER_CONTACT` | repo URL | contact put in the User-Agent, as Nominatim's policy asks |
| `NEWS_GEOCODER_GAP` | `1.0` | seconds between address lookups (their rate policy) |

## Architecture

```
backend/app
├── main.py           FastAPI app: REST API, SSE stream, static frontend
├── geocode.py        address lookup (OpenStreetMap) for places the gazetteer lacks
├── ingest.py         async poller: fetch → normalize → geotag → score → cluster → alert
├── clustering.py     incremental article→event clustering
├── translate.py      lazy cached translation (Google gtx / LibreTranslate)
├── matching.py       shared criteria matcher (feeds, alerts, ad-hoc search)
├── boolean_query.py  recursive-descent parser for user boolean strings
├── geo.py            gazetteer geotagging, point-in-polygon/circle geofences
├── scoring.py        importance score, breaking signals, auto-categories
├── models.py         SQLAlchemy models (Source, Article, Feed, Alert, AlertEvent)
├── catalog.py        source catalog seeding (curated + city feeds)
└── events.py         in-process pub/sub behind /api/stream (SSE)

backend/data
├── sources.json      the editable global source catalog
└── gazetteer.json    countries (centroids + aliases) and ~160 major cities

frontend/             no build step — plain HTML/CSS/JS; Leaflet vendored in frontend/vendor
                      (loaded on demand by ensureLeaflet(), not at startup)
```

### One process, one loop

D.E.L.P.H.I. runs as a single machine on purpose — the poller's scheduler, the
rate limiter and the SQLite file all assume there is exactly one of it, so it
scales up rather than out. That makes anything slow on the event loop a
whole-site outage rather than a slow page: a column is a SQL read plus the
matcher sifting rows in Python, and while one runs inline nothing else is
served. Column scans therefore go through `scan_articles`, which hands the work
to a worker thread and admits `SCAN_CONCURRENCY` of them at a time — enough to
keep a thread busy while another waits on SQLite, few enough that a board does
not flood the pool. Measured on a 120,000-article copy: a six-column board held
an unrelated `GET /api/locations` for 719 ms before, and 36 ms after.

### API sketch

```
GET  /api/meta                     categories/countries/stats
GET|POST|PATCH|DELETE /api/sources        manage sources
POST /api/sources/topic-tracker    track any query via Google News RSS
POST /api/articles/search          ad-hoc criteria search (feed preview, search bar)
POST /api/query/validate           boolean-string validation
GET|POST|PUT|DELETE /api/feeds     per-user feeds (X-User-Id header)
POST /api/feeds/reorder            dashboard layout
GET  /api/feeds/{id}/articles      feed contents (?lang=xx translates)
GET  /api/feeds/{id}/events        feed contents clustered into events
GET  /api/events/{id}              one event + full article timeline
GET  /api/story/{id}/export        one story + every outlet carrying it, as a file
GET  /api/feeds/{id}/why/{art}     which of a feed's words are in an article, and where
POST /api/events/rebuild           recluster all stored articles
GET|POST|PUT|DELETE /api/alerts    per-user alerts
GET  /api/alerts/{id}/events       alert hit history
POST /api/alerts/{id}/mark-seen
POST /api/ingest/run               poll all sources now
GET  /api/stream                   SSE: live article batches + alert hits
```

## Notes & limits

- **Ingestion posture**: source discovery uses publishers' own RSS/Atom
  syndication feeds (plus Google News RSS) on a modest poll interval. For
  matching, D.E.L.P.H.I. additionally fetches each new article's page once and
  extracts its text — like a feed reader's readability view — capped per cycle
  (`NEWS_CONTENT_MAX_PER_CYCLE`, default 150) and disable-able with
  `NEWS_CONTENT_FETCH=0`. Review publishers' terms before heavy use.
  **What counts as the article matters for matching**, because criteria are
  applied to title + summary + body: a page's section menu, "More from" rail
  and newsletter promos are all built from list items and headings wrapped
  around links, and once stored as body text they make a story match terms that
  appear nowhere in it — a WNBA report satisfying a data-centre query, in the
  case that prompted this. `content.py` drops link text inside list items and
  headings and keeps it inside paragraphs and quotes, where a link is an
  ordinary citation. Bodies already stored keep the text they were stored with.
- The catalog is a *seed*, not a census — some third-party feed URLs go stale;
  the Sources panel shows per-source health (`ok` / error) so dead ones are
  easy to spot, fix, or disable. Add more by editing
  `backend/data/sources.json` or via the UI/API.
- Geotagging is gazetteer-based (fast, offline). It handles countries, aliases
  (UK, UAE, Kiev/Kyiv…), and major cities, but has the usual ambiguities
  (e.g. Georgia country vs. US state). Swappable for a proper NER geocoder later.
- Importance is a transparent heuristic, not a black box — see `scoring.py`
  and tune the weights to taste. The source's reach deliberately contributes
  less than the story does: it starts an article between 31 and 46 depending on
  scope and tier, against up to 60 from breaking signals, geographic breadth
  and cross-source corroboration. It was 21–53, which is a spread wide enough
  to settle the ranking on its own — Home's "Top stories" asks for 55, and an
  international tier-1 wire's most routine item already scored 53 while a city
  outlet's report of a fatal explosion scored 40. That made the two headline
  columns a view of the ~30 catalog wires whatever else was in the catalog.
- **Sources that stop answering leave.** Five consecutive failed polls
  (`NEWS_REMOVE_AFTER`) ends a source: deleted outright if it never produced a
  single article, retired — disabled, kept, re-enablable — if it published, and
  always retired rather than deleted if a human added it. Automatic repair gets
  its attempt first, so a feed that merely moved is rewired rather than
  dropped. Without this a catalog that grows by itself only ever grows: nothing
  removed the dead ones, and they were re-requested every few minutes for the
  life of the deployment.
- **A source that answers is not the same as a source that carries news.**
  `/api/sources` reports `has_produced` per source and `/api/ingest/status`
  counts the catalog: producing, silent (polled, never carried anything),
  never polled, and retired. The Sources panel marks them and its filter box
  understands `silent` and `retired`.
- **Nothing in a poll cycle that scales with the batch runs on the event
  loop.** This was a real outage, several times a day: the app logged nothing
  for 82 seconds, the health check failed, and Fly de-routes a machine whose
  check is failing — so the site was unreachable for everyone while the app was
  perfectly healthy. Re-deriving places, categories and language from a full
  body costs ~160ms (105 gazetteer, 53 categorization), and a cycle does up to
  `CONTENT_MAX_PER_CYCLE` of them for the new batch and again for the backlog:
  ~24s a pass, ~48s a cycle, on the one thread that also answers requests.
  Every phase that scales with the batch is now threaded *and sliced*: the
  enrichment pass in `NEWS_ENRICH_CHUNK` articles (default 25, ~4s), clustering
  and alert matching in `NEWS_CLUSTER_CHUNK` (default 50), plus the backlog
  query and the six-hourly prune/checkpoint/vacuum. Threading alone was not
  enough and the live machine proved it: after clustering was moved to a thread
  it still went quiet for 37 seconds, because one thread holding the interpreter
  for an 800-article batch starves the loop as thoroughly as running on it — it
  only stops logging about it. A thread does not make the work cheaper (the
  interpreter still runs it one bytecode at a time); slicing is what makes it
  interruptible, and that is the difference between a slow minute and an outage.
  Slicing the clustering is only sound because one `LiveEvents` is carried
  across the slices over a pre-sorted batch, so the grouping is identical to a
  single pass — asserted directly in `test_matching_clustering.py`, since a
  change there would be invisible in any other test.
- **Devices are counted by recent use, not by valid sign-ins.** Sessions are
  stateless HMAC tokens with a thirty-day life, so "how many devices is this
  account signed in on" is both large and uninteresting — a phone used once
  last month still holds a token. `devices.py` counts what has actually sent a
  request inside `NEWS_DEVICE_ACTIVE_WINDOW_S` (default 5 min), per *device*
  rather than per session or connection: one laptop with four tabs is one
  laptop, because tabs share the browser's local storage and therefore its
  `device_key`. That key is minted client-side and sent as `X-Delphi-Device`;
  it identifies, it never authenticates — the token already proved the account,
  so a copied key gains nothing. Clearing site data or opening a private window
  reads as a new device, which is the honest limit of recognising a browser
  without fingerprinting one. A client that sends no key is allowed and not
  counted: the header comes from our own script, so refusing requests without
  one would be a lockout dressed as a limit.
  Enforcement lives in the `require_account` middleware, which already loads
  the account on every request, and `last_seen_at` is only written every
  `NEWS_DEVICE_TOUCH_EVERY_S` — a per-request write is exactly the cost that
  has taken this machine down before. Over the limit answers 403 with
  `code: "device_limit"` so the client shows one takeover screen rather than a
  toast per panel, offering an emailed link (`/api/auth/devices/release-link`,
  unauthenticated by necessity — the person asking is the one being refused)
  that clears the devices *and* bumps `token_version`. Both halves: clearing
  alone would free the slot while the evicted device's token still worked, so
  it could take the slot straight back.
- **Translation is warmed between cycles, not paid for by the first reader.**
  A translation is cached forever after the first request that needs it — but
  that first request was a reader's, inside their page load, and
  `translate_articles` awaits every miss before returning. A column of forty
  foreign-language stories was dozens of round-trips deep: `slow:7.2s POST
  /api/articles/search` in the live log. `warm_translations()` now runs after
  `warm_home()` each cycle, translating exactly the articles Home has warmed
  (`home.warm_article_ids()`) into the languages accounts have actually chosen
  (`reading_languages()`, read from the `lang` key in `User.settings` — not all
  sixteen offered). The reader's request then finds them already stored.
  **Rejected on purpose:** returning original text immediately and filling
  translations in behind. It makes the page appear sooner and be useless —
  someone who reads only English is not better served by Mandarin arriving
  quickly. The wait is moved off the request, not moved onto the screen.
  Separately, `_translate_one` now fetches a title and its summary with
  `asyncio.gather` rather than awaiting one then the other, halving the depth
  of any translation that still happens live.
- **The machine is `performance-1x`, not shared, and the poll batch is half
  what it was.** The phase timings below found this on their first reading: a
  round measured `fetch 45.3s store 143.0s enrich 89.0s backfill 18.5s cluster
  83.4s` — 6m19s, restarting 15s later, so the machine never idled and requests
  queued behind it. `/api/meta` was logged at **31.8s**, past the browser's 30s
  timeout, which is how a healthy server came to report itself unreachable
  while passing every health check. Reading a headline costs 2.8ms of Python
  (`extract_places` 2.1 + `classify_categories` 0.7); the live machine was
  spending ~115ms per article, and that ~40x gap is Fly throttling a *shared*
  vCPU under sustained load. Hence a dedicated core. `POLL_BATCH` also went
  80→40 and `CITY_PER_TICK` 20→12: a smaller round is not less news, since what
  a source publishes is set by the publisher — a poll that finds three items
  instead of twelve finds them three times as often.
- **Each cycle logs how long each phase took** (`fetch store enrich backfill
  cluster`, seconds, on the batch summary line). Every stall above had to be
  located by finding where the log went quiet, which only works for phases that
  log at all — the enrichment pass and the clustering pass are both silent by
  nature. The timings cost microseconds and turn the next occurrence into
  something to read instead of deduce.
  Note that the earlier defences for this class of stall (the restart warmup and
  a health check that tells busy from broken) could not help here: both assume
  the event loop still gets to run.
- **The feed/alert editor opens before the source catalog is fetched, not
  after.** `openBuilder` used to `await ensureSources()`, so pressing ✎ showed
  nothing at all — no window, no spinner — until half a megabyte covering 1,200
  outlets had arrived and been parsed, and that wait grew every time discovery
  added an outlet. Nothing on the step the reader lands on needs it: countries,
  categories and languages come from `META` at startup, and the outlet picker is
  one optional control on the last step, which now names its own state
  (loading / ready / unavailable) instead of rendering an empty list. Cold, with
  the connection shaped: 79ms on localhost, 201ms on an ordinary home line and
  438ms on hotel wifi, against 46 / 28 / 30ms after — flat, because opening no
  longer touches the network.
- Accounts are self-serve; passwords are PBKDF2-hashed with HMAC-signed
  session tokens (`NEWS_SECRET` env or an auto-generated key beside the DB).
  Every API route except sign-in/register requires a session. **Common
  passwords are refused** wherever one is set — registration, an emailed reset,
  a change, and an operator setting one: the 10,000 most common
  (`backend/data/common_passwords.txt`, from SecLists, MIT), matched through
  padding and leetspeak so `password`, `Password123` and `P@ssw0rd1` are all
  the same answer, plus all-digit passwords and any containing the account's
  own username or email name. Not a strength meter — counting character classes
  pushes people towards `P@ssw0rd1`, which is on every list. It exists for the
  case the other defences miss entirely: credentials reused from a site that
  was breached and replayed here, where nothing is guessed and there is nothing
  to rate-limit. Measured at zero false positives across 25,000 generated
  passwords and passphrases. **Passwords can be changed in the app** (Settings → Account security), which needs no SMTP —
  the emailed reset flow is for someone locked out. It requires the current
  password even though the caller is signed in: a session is not proof of
  ownership (an unlocked laptop, a copied token), and without that check brief
  access would be enough to set a new password and keep the owner out
  permanently. **Sessions can be ended.** Each token carries the account's `token_version`; a password reset
  (self-service or by an operator) and Settings → *Sign out everywhere* both
  increment it, which refuses every token issued before that moment. Without
  it a signature and an expiry were the whole story, so resetting a password
  left whoever already had your token signed in for the rest of its 30 days.
  The session token is read **only** from the `Authorization` header: the live
  event stream authenticates with a separate 60-second ticket
  (`POST /api/stream/ticket`), because EventSource cannot set headers and
  anything in a URL is written to every intervening proxy's access log — which
  is where a 30-day credential used to land on every page load. **Email
  verification and password reset** activate automatically when SMTP is
  configured (`NEWS_SMTP_HOST/PORT/USER/PASS/FROM`, `NEWS_SMTP_TLS` =
  starttls|ssl|none — works with Resend, Mailgun, SES, or any SMTP relay):
  registration then emails a 48h verification link and sign-in is blocked
  until it's clicked; "Forgot password?" emails a 1h reset link. Without
  SMTP, accounts auto-verify (self-host mode) and would-be mails are logged.
  **Sign-up does not confirm whether an email address is registered**: an
  address that already has an account gets the same answer as a free one, and
  a notice goes to the address itself (naming the username and carrying a reset
  link) where only its owner can read it. Otherwise anyone with a list of
  addresses could learn which of them read news here. Usernames are still
  reported as taken — they must be unique, and a shared feed shows the username
  of whoever shared it, so nothing is revealed that the app doesn't display.
  Without SMTP there is nowhere to send the notice, so that case still answers
  plainly.
  Login, registration, password-reset, search, and export endpoints are
  rate-limited per client IP (disable with `NEWS_RATE_LIMIT=0`). Search and
  export are deliberately far apart — a reader searches constantly, while an
  export is up to 2,000 articles assembled into a file and, with a reading
  language set, run through the translation service. Which client that is comes
  from the socket peer counted back through **`NEWS_TRUSTED_PROXIES`** hops of
  `X-Forwarded-For` — that header is appended to by each proxy, so its leading
  entries are written by the caller and believing them hands anyone a fresh
  allowance per request. The default is `1` on Fly (detected via `FLY_APP_NAME`)
  and `0` elsewhere; set it to however many proxies you actually run, and set
  it to `0` if the app is reachable directly. When you expose the app
  behind a proxy, set **`NEWS_PUBLIC_URL`** (e.g. `https://delphi.example.com`)
  so emailed verification/reset links use a fixed origin instead of a
  spoofable `Host` header — or `NEWS_ALLOWED_HOSTS` (comma-separated) to
  allowlist the hosts links may use. Sources stay shared and editable by every
  signed-in user by design, but the actions that touch the whole archive are
  not: **deleting a source and the maintenance jobs** (rebuild events,
  reclassify, detect languages, fetch content, seed cities) require an
  operator account, and a manual refresh is rate-limited per client the same
  way sign-in is. Adding, editing, and polling sources stay open to everyone.
- **Outbound fetches can only reach the public internet.** Every URL a user
  supplies — a feed, an article page, a discovered candidate, an alert webhook
  — is resolved and refused if it points at a private, loopback, link-local, or
  reserved address, or at a port other than 80/443. The check lives in the HTTP
  transport rather than at the call site, so each redirect hop is validated too
  and a publisher answering `302 http://169.254.169.254/` gets nowhere. Saving
  a URL that does not resolve yet is allowed (the webhook host may not exist
  yet); fetching one is not. Without this, any signed-in account could use the
  server as a proxy into whatever sits unauthenticated on its private network.
- **A strict Content-Security-Policy**, plus `nosniff`, `Referrer-Policy`,
  `Permissions-Policy`, and HSTS (`NEWS_HSTS_MAX_AGE`, one day by default —
  raise it once you're sure of the certificate). The policy is
  `script-src 'self'; style-src 'self'` with no `unsafe-inline` anywhere, which
  is affordable because there's no inline script, no `<style>` block, no inline
  `style=` attribute, and no CDN in the page — Leaflet is vendored. It closes
  nothing today: every piece of feed text reaches the page through the DOM,
  never `innerHTML`. That's the reason to add it while it's cheap, not a reason
  to skip it. `img-src` is the one loose directive, because thumbnails are
  hotlinked from publishers and map tiles come from OpenStreetMap.
- **No cross-origin access by default.** Delphi serves its own frontend from the
  same origin, so nothing legitimate needs it; set `NEWS_CORS_ORIGINS`
  (comma-separated) only if you're building a browser client of your own.
- **Local coverage for ~500 major cities** (170 countries) seeds on first
  run: each city gets a Google News city-edition source in the country's
  language, and auto-discovery grows its real local outlets from there.
  `NEWS_SEED_CITIES=0` skips the whole city catalog.
- **Ingestion is a continuous rolling poller.** Each source refreshes on its
  own cadence (wires every three minutes, city feeds hourly, quiet ones less
  often) with per-host pacing, so a large catalog on a rate-limited host
  (Google News) never blocks or bursts. Tunable via `NEWS_CITY_INTERVAL`,
  `NEWS_POLL_TICK`, `NEWS_GOOGLE_GAP`.
- **Polling is conditional.** Each source remembers the `ETag` and
  `Last-Modified` the publisher last sent and asks with them, so an unchanged
  feed answers `304` with no body and nothing to parse. That is what makes the
  intervals above affordable — measured against a stub publisher, ten polls of
  an unchanged feed moved 4.9 KB instead of 48.8 KB.
- **Article bodies catch up.** A busy minute brings more articles than one
  cycle fetches bodies for; the remainder are picked up on the quiet ticks that
  follow, newest first. A page that cannot be fetched is not retried for
  `NEWS_BACKFILL_RETRY_HOURS`, so one dead URL never blocks the backlog.

## Roadmap ideas

- Push channels for alerts (email / Slack / webhook / mobile push)
- Entity extraction and multilingual event clustering (current clustering is
  token-based, so same-story coverage in different languages forms separate events)
- PostgreSQL + PostGIS for precise geofencing at scale
- Full-text article fetch for sources whose feeds only carry snippets
