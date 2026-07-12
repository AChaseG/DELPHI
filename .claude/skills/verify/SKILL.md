---
name: verify
description: Build/launch/drive recipe for verifying Delphi (global news dashboard) end-to-end.
---

# Verifying Delphi

## Launch

```bash
python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt   # once
NEWS_DISABLE_INGEST=1 .venv/bin/uvicorn backend.app.main:app --port 8123 &
```

`NEWS_DISABLE_INGEST=1` keeps the background poller off so cycles are driven
explicitly with `POST /api/ingest/run`. DB is `backend/data/news.db` (gitignored);
delete it for a clean slate. Catalog sources are seeded on startup.

## Drive the API surface

```bash
curl -s -X POST :8123/api/demo/seed                      # 20 offline sample articles
curl -s -X POST ':8123/api/articles/search?limit=5' -H 'Content-Type: application/json' \
  -d '{"criteria":{"query":"(earthquake OR tsunami) AND japan"}}'
# geofence: GeoJSON Polygon is [lon,lat]; Circle is {"center":[lat,lon],"radius_km":N}
# feeds/alerts are scoped by the X-User-Id header
```

## End-to-end ingest → alert → SSE (works offline)

Sandboxed environments block external news domains (proxy 403), but
127.0.0.1 is exempt — serve a local RSS file and register it:

```bash
python3 -m http.server 8200 --directory <dir-with-wire.xml> &
curl -X POST :8123/api/sources -d '{"name":"Test Wire","rss_url":"http://127.0.0.1:8200/wire.xml","scope":"international","tier":1}' -H 'Content-Type: application/json'
curl -N :8123/api/stream &            # watch for {"type":"alert",...}
curl -X POST :8123/api/ingest/run
```

A headline like "Breaking: Massive earthquake strikes near Tokyo, tsunami
warning issued" scores ≥80 (Critical) from a tier-1 international source and
fires a `min_importance: 80` alert.

## Drive the UI

Playwright + `/opt/pw-browsers/chromium-*/chrome-linux/chrome`. Open `/`,
a fresh browser profile shows the empty state → click **Add starter feeds** →
expect 4 feed columns with `.article` rows. `#btn-create` opens the builder
(the `#builder-mode` toggle picks feed vs alert, and flips an existing item
into a conversion);
typing in `#b-query` shows live validation in `#b-query-status`.
Leaflet comes from unpkg CDN — blocked in sandboxes, so the map button shows a
graceful message instead; test map drawing only with real network.

## Gotchas

- **Always test ingest with two local feeds sharing one article URL** —
  articles.url is globally unique and real feeds (Google News especially)
  syndicate identical URLs across sources. Per-source-only dedup once made
  every real-world cycle roll back with IntegrityError (0 articles forever).

- Elements hidden via the `hidden` attribute need the `[hidden]{display:none!important}`
  rule in styles.css — several containers set their own `display`.
- Alert evaluation happens only inside ingest cycles, not on demo seed.
- Startup seeds ~500 city local-news sources (added_by="city-catalog",
  scope="local", Google News feeds) by default — set `NEWS_SEED_CITIES=0`
  for a lean test DB. Ingestion is a **rolling poller**: `ingest_loop` ticks
  every `NEWS_POLL_TICK`s, fetching `due_sources()` (wires first, then ≤
  `NEWS_CITY_PER_TICK` city feeds), paced per host by `HostPacer` (Google gets
  `NEWS_GOOGLE_GAP`). `run_ingest_cycle()` = manual refresh: all wires + a
  bounded, due slice of city feeds (never a Google stampede). Don't fetch real
  google feeds in tests — pacing sleeps + blocked network hang; unit-test
  `due_sources`/`effective_interval`/`HostPacer` and drive `_ingest_batch`
  against local stubs. Lower `NEWS_GOOGLE_GAP`/`NEWS_SHARED_HOST_GAP` for
  count-only tests so pacing doesn't slow them.
- **Auto-discovery adds sources mid-cycle**: entries carrying
  `<source url="...">Name</source>` tags (Google News trackers especially) make
  ingest probe unknown publisher domains and add them as sources
  (added_by="auto-discovered"). Outcomes persist in discovered_domains.
  NEWS_AUTO_DISCOVER=0 disables when a test needs a stable source list.
- **Source self-repair rewrites rss_url**: after 2 consecutive 403/404/not-a-feed
  cycles, ingest hunts for a replacement feed (homepage autodiscovery → common
  paths → Google News site: fallback) and switches the source. If a test needs
  URLs to stay put, set NEWS_AUTO_REPAIR=0 or disable the sources. Statuses
  starting "ok" (incl. "ok (auto-repaired)") count as healthy.
- Pantheons (organizations): feeds/alerts with `pantheon_id` set are shared
  copies — excluded from /api/feeds and included in every member's /api/alerts.
  Access: members read, sharer + owner/admin edit; /api/pantheons/public must
  stay declared before /api/pantheons/{id} (route order).
- Auth endpoints are rate-limited per IP (login 10/5min, register 5/hr,
  forgot 5/15min…). Tests that register several accounts must launch the
  server with `NEWS_RATE_LIMIT=0`; a dedicated limiter test runs it enabled.
- Emailed verify/reset links honor `NEWS_PUBLIC_URL` / `NEWS_ALLOWED_HOSTS`
  over the Host header (anti host-header-injection). The `/api/auth/claim`
  endpoint and anonymous `X-User-Id` profile were removed — accounts are
  mandatory, so nothing is ever created under an anonymous id.
- Registering a user on an empty DB auto-triggers a first catalog ingest that
  holds the cycle lock — /api/ingest/run returns 409 meanwhile; poll/kick until
  your stub source's last_status is set instead of assuming one run sufficed.
