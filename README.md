# 🌐 Global News Dashboard

A self-hosted tool that continuously ingests news from sources across the globe —
local, national, and international — and presents them on a customizable,
per-user dashboard with Dataminr-style live alerting and **draw-on-map geofenced
feeds**.

## What it does

- **Global ingestion.** A background engine polls a curated catalog of 70+
  RSS/Atom feeds spanning every continent — wire-level internationals (BBC, Al
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
- **Customizable per-user dashboard.** Each browser gets its own profile; feeds
  are arranged in columns you can reorder, widen, edit, and delete.
- **Feed builder** with combinable criteria:
  - country (article country *or* any place mentioned in it)
  - standard news categories (politics, business, economy, technology, science,
    health, environment, conflict, disaster, crime, sports, entertainment, culture, world)
  - source scope (local / national / international) and language
  - free keywords + exclude-keywords
  - **user-written boolean queries** — `("supply chain" OR semiconductor) AND
    (china OR taiwan) NOT rumor` — with live validation
  - **minimum importance** to the international community (Critical / High /
    Notable / Routine tiers)
  - recency window, specific sources, sort by newest or importance
- **Alerts (Dataminr-style).** An alert takes the exact same criteria as a
  feed. Every ingest cycle, new articles are evaluated against all active
  alerts; hits are stored, counted on the bell, and **pushed live to the
  dashboard over Server-Sent Events** as toast notifications.
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
> `/api/meta` responds, and prints the tail of `/tmp/news-dashboard.log` if
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

On first run the source catalog is seeded and a full poll starts automatically
(every 5 minutes thereafter). Use **⟳** to poll on demand, **Add starter
feeds** for an instant dashboard, or `POST /api/demo/seed` for offline sample
data.

## Going live

**Share it straight from a codespace (temporary).** With the server running,
open the **Ports** panel, right-click port 8000 → **Port visibility** →
**Public**. Anyone with the `https://<codespace>-8000.app.github.dev` URL can
now open your dashboard. Two caveats: a codespace **auto-stops after ~30
minutes of inactivity** (Settings ▸ Codespaces lets you raise the idle timeout
to 4 hours) and ingestion/alerts pause while it sleeps — so this is for demos,
not hosting. Also note the app has **no authentication**: anyone with a public
URL can read every feed and add sources, so keep visibility private unless
you're actively sharing.

**Host it 24/7 (Docker).** A `Dockerfile` is included; the database lives on a
volume at `/data` so history survives restarts:

```bash
docker build -t news-dashboard .
docker run -d -p 8000:8000 -v newsdata:/data --restart unless-stopped news-dashboard
```

That works on any VPS (~$5/month). For a managed platform, `fly.toml` is
included for Fly.io:

```bash
fly launch --copy-config --no-deploy   # pick your app name
fly volumes create newsdata --size 1
fly deploy
```

(Railway and Render also auto-detect the Dockerfile — set a volume/disk at
`/data`. Avoid free tiers that sleep on idle; a sleeping instance stops
polling and alerting, same as a codespace.)

### Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `PORT` / `HOST` | `8000` / `127.0.0.1` | bind address (run.sh) |
| `NEWS_FETCH_INTERVAL` | `300` | seconds between ingest cycles |
| `NEWS_FETCH_CONCURRENCY` | `10` | parallel source fetches |
| `NEWS_DB_PATH` | `backend/data/news.db` | SQLite location |
| `NEWS_DISABLE_INGEST` | unset | `1` disables the background poller |
| `NEWS_TRANSLATE_PROVIDER` | `google` | `google` \| `libretranslate` \| `off` |
| `NEWS_LIBRETRANSLATE_URL` | unset | LibreTranslate server URL (with provider above) |

## Architecture

```
backend/app
├── main.py           FastAPI app: REST API, SSE stream, static frontend
├── ingest.py         async poller: fetch → normalize → geotag → score → cluster → alert
├── clustering.py     incremental article→event clustering
├── translate.py      lazy cached translation (Google gtx / LibreTranslate)
├── matching.py       shared criteria matcher (feeds, alerts, ad-hoc search)
├── boolean_query.py  recursive-descent parser for user boolean strings
├── geo.py            gazetteer geotagging, point-in-polygon/circle geofences
├── scoring.py        importance score, breaking signals, auto-categories
├── models.py         SQLAlchemy models (Source, Article, Feed, Alert, AlertEvent)
├── catalog.py        catalog seeding + offline demo articles
└── events.py         in-process pub/sub behind /api/stream (SSE)

backend/data
├── sources.json      the editable global source catalog
└── gazetteer.json    countries (centroids + aliases) and ~160 major cities

frontend/             no build step — plain HTML/CSS/JS + Leaflet from CDN
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
POST /api/demo/seed                offline sample articles
```

## Notes & limits

- **Respectful by design**: ingestion uses publishers' own RSS/Atom syndication
  feeds (plus Google News RSS), not HTML scraping, with a modest poll interval
  and an honest User-Agent. Review each publisher's terms before heavy use.
- The catalog is a *seed*, not a census — some third-party feed URLs go stale;
  the Sources panel shows per-source health (`ok` / error) so dead ones are
  easy to spot, fix, or disable. Add more by editing
  `backend/data/sources.json` or via the UI/API.
- Geotagging is gazetteer-based (fast, offline). It handles countries, aliases
  (UK, UAE, Kiev/Kyiv…), and major cities, but has the usual ambiguities
  (e.g. Georgia country vs. US state). Swappable for a proper NER geocoder later.
- Importance is a transparent heuristic, not a black box — see `scoring.py`
  and tune the weights to taste.
- Users are lightweight anonymous profiles (per-browser id sent as
  `X-User-Id`). Put real auth in front of it before exposing publicly.

## Roadmap ideas

- Push channels for alerts (email / Slack / webhook / mobile push)
- Entity extraction and multilingual event clustering (current clustering is
  token-based, so same-story coverage in different languages forms separate events)
- More non-English catalog sources
- PostgreSQL + PostGIS for precise geofencing at scale
- Full-text article fetch for sources whose feeds only carry snippets
