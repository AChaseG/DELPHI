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
