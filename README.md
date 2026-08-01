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
  in columns you can reorder, widen, edit, and delete.
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
    each validated as you type
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
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn backend.app.main:app --port 8000
```

On first run the source catalog is seeded and polling starts automatically —
each source then refreshes on its own cadence. Use **⟳** to poll on demand and
**Add starter feeds** for a ready-made dashboard layout. Everything on the
board is real reporting: Delphi generates no sample data, and any left behind
by an older build is removed at startup.

### Tests

```bash
pip install -r requirements-dev.txt
pytest
```

A fast, no-network suite (pure logic + in-process API via FastAPI's TestClient)
covers boolean search, language detection, multilingual scoring, criteria
matching, clustering, the rolling-poll scheduler, URL safety, and the auth /
settings / Pantheon-sharing flows. It runs against a throwaway database and
never touches `backend/data`. GitHub Actions runs it on every push
(`.github/workflows/ci.yml`).

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
| `NEWS_TRANSLATE_PROVIDER` | `google` | `google` \| `libretranslate` \| `off` |
| `NEWS_LIBRETRANSLATE_URL` | unset | LibreTranslate server URL (with provider above) |
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
- The catalog is a *seed*, not a census — some third-party feed URLs go stale;
  the Sources panel shows per-source health (`ok` / error) so dead ones are
  easy to spot, fix, or disable. Add more by editing
  `backend/data/sources.json` or via the UI/API.
- Geotagging is gazetteer-based (fast, offline). It handles countries, aliases
  (UK, UAE, Kiev/Kyiv…), and major cities, but has the usual ambiguities
  (e.g. Georgia country vs. US state). Swappable for a proper NER geocoder later.
- Importance is a transparent heuristic, not a black box — see `scoring.py`
  and tune the weights to taste.
- Accounts are self-serve; passwords are PBKDF2-hashed with HMAC-signed
  session tokens (`NEWS_SECRET` env or an auto-generated key beside the DB).
  Every API route except sign-in/register requires a session. **Sessions can be
  ended.** Each token carries the account's `token_version`; a password reset
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
  Login, registration, and password-reset endpoints are rate-limited per
  client IP (disable with `NEWS_RATE_LIMIT=0`). Which client that is comes
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
