# Deploying D.E.L.P.H.I. as a 24/7 website

Delphi is a single FastAPI process with an in-process scheduler (ingestion,
alert firing, source self-repair, rolling source polling) and a SQLite database.
That shapes every deployment decision below:

> **Run exactly one instance.** The scheduler, the SQLite file, and the
> in-process rate limiter cannot be shared across machines. Scale the VM
> **up** (more CPU/RAM), never **out** (more machines). A second instance
> would double-poll every source and can't share the database.

The database — accounts, feeds, alerts, Pantheons, articles — is a single file
on disk. It **must** live on a persistent volume, or everything resets on
redeploy.

---

## Option A — Fly.io (repo is preconfigured)

```bash
fly auth login                          # or: fly auth signup
fly launch --copy-config --no-deploy    # reuses fly.toml; pick a unique app name
fly volumes create newsdata --size 3    # persistent disk mounted at /data
fly secrets set NEWS_SECRET=$(openssl rand -hex 32)
fly deploy
```

`fly.toml` already keeps one machine always on (`min_machines_running = 1`,
`auto_stop_machines = "off"`) so ingestion and alerts run around the clock,
and mounts the volume at `/data` (where `NEWS_DB_PATH` points the database).

Custom domain:

```bash
fly certs create yourdomain.com     # then add the shown DNS records
fly secrets set NEWS_PUBLIC_URL=https://yourdomain.com
```

Updating the site: push your changes, then `fly deploy`. The volume (and all
data) is untouched across deploys.

---

## Option B — any Docker host (VPS, Render, Railway, …)

The `Dockerfile` is host-agnostic. Requirements: a persistent volume at
`/data`, the env vars below, HTTPS in front, and a single running instance.

```bash
docker build -t delphi .
docker run -d --restart=always -p 80:8000 \
  -v delphi-data:/data \
  -e NEWS_SECRET=$(openssl rand -hex 32) \
  -e NEWS_PUBLIC_URL=https://yourdomain.com \
  delphi
```

Terminate TLS with the platform's built-in HTTPS, or put Caddy/nginx in front.

---

## Configuration

### Secrets & environment variables

| Variable | Needed? | Purpose |
|----------|---------|---------|
| `NEWS_SECRET` | **Strongly recommended** | Stable key that signs login tokens. If unset, a key is generated and stored beside the database (persists on the volume), but setting it explicitly is safest and lets you rotate it deliberately. |
| `NEWS_PUBLIC_URL` | Recommended (public sites) | Your canonical origin, e.g. `https://yourdomain.com`. Emailed verification/reset links use it instead of the request's `Host` header (prevents host-header injection). |
| `NEWS_ALLOWED_HOSTS` | Alternative to the above | Comma-separated hostnames allowed in email links, if you'd rather allowlist than pin one URL. |
| `NEWS_ADMIN_USERS` | Recommended | Comma-separated usernames and/or emails that are **operators** — they get the 🛠 Admin console to manage every account (verify, promote, suspend, reset password, delete). Matched case-insensitively; a listed account is an operator the moment it registers, and stays one even if the database is lost. No admin password is baked into the code; from the console operators can promote others. A designated operator is also **verified on sight**, so broken SMTP can't lock you out of your own instance — sign in as that account and force-verify anyone else. Example: `NEWS_ADMIN_USERS=you@example.com,ops-lead`. |
| `NEWS_SMTP_HOST` / `PORT` / `USER` / `PASS` / `FROM` | Optional | Enables email verification, password reset, **and ✉️ alert delivery** (Resend, Mailgun, SES, any SMTP relay). Set `NEWS_SMTP_TLS` to `starttls`, `ssl`, or `none`. **Without SMTP, accounts auto-verify and alert emails are logged, not sent** — fine for a private instance, not ideal for a public one. (Alert 🔗 webhooks work without SMTP.) Note the half-configured case: if `NEWS_SMTP_HOST` is set but mail cannot actually be delivered, sign-up requires a verification link that never arrives. Either fix delivery, sign in as a `NEWS_ADMIN_USERS` operator (verified on sight) and force-verify accounts from the console, or unset `NEWS_SMTP_HOST` to return to auto-verify. |
| `NEWS_TRUSTED_PROXIES` | Auto-detected | How many proxies of yours sit in front of the app — `1` on Fly (detected from `FLY_APP_NAME`), `0` elsewhere. It decides who the rate limiter thinks is calling, so it is worth getting right in both directions. Each proxy *appends* to `X-Forwarded-For`, so anything before the last N entries was written by the caller: set this **too high** (or leave it at 1 with nothing actually in front) and anyone can forge a fresh allowance per request, removing the sign-in, registration, and reset limits entirely. Set it **too low** behind a real proxy and every visitor shares one bucket, so one person mistyping a password locks out the rest. Count your own proxies and set it explicitly if you run anything unusual. |
| `NEWS_CLIENT_IP_HEADER` | Off | A header your proxy *overwrites* with the real client address (e.g. `Fly-Client-IP`, `CF-Connecting-IP`), used instead of counting hops. Only set this if you know the proxy overwrites rather than appends — a header a caller can set and Delphi believes is the same bypass as above. |
| `NEWS_DB_PATH` | Preset in Docker | Database file path (`/data/news.db` in the image). |

Ingestion/tuning knobs (sensible defaults; change only if needed):

Ingestion is a **continuous rolling poller** — each source refreshes on its own
cadence, with requests to a shared host (Google News backs the ~500 city feeds)
paced into a steady drip instead of a burst. Knobs:

| Variable | Default | Purpose |
|----------|---------|---------|
| `NEWS_FETCH_INTERVAL` | `180` | Refresh interval for news wires (seconds). Polls are conditional, so an unchanged feed costs a request and no body. |
| `NEWS_CITY_INTERVAL` | `3600` | Refresh interval for city local feeds (seconds; ~60 min). Quiet feeds back off further automatically. |
| `NEWS_POLL_TICK` | `15` | Seconds between scheduler ticks. |
| `NEWS_POLL_BATCH` | `80` | Max news wires fetched per tick. |
| `NEWS_CITY_PER_TICK` | `20` | Max city feeds fetched per tick (bounds the Google drip). |
| `NEWS_GOOGLE_GAP` | `2.0` | Min seconds between requests to news.google.com. |
| `NEWS_BACKFILL_HOURS` | `48` | How far back the article-body backlog reaches. |
| `NEWS_BACKFILL_RETRY_HOURS` | `6` | Before an article whose page could not be fetched is tried again. |
| `NEWS_DISCOVER_MAX_PER_CYCLE` | `8` | Unknown publishers probed for a feed of their own per tick. |
| `NEWS_DISCOVER_CONCURRENCY` | `4` | How many of those probes run at once. |
| `NEWS_RETENTION_DAYS` | `30` | Prune articles (and their translations/alert history) older than this; `0` keeps everything forever. |
| `NEWS_SEED_CITIES` | `1` | Seed ~500 city local-news sources on first run (`0` to skip). |
| `NEWS_AUTO_DISCOVER` | `1` | Auto-add local outlets found in coverage (`0` to disable). |
| `NEWS_AUTO_REPAIR` | `1` | Auto-fix broken source feeds (`0` to disable). |
| `NEWS_RATE_LIMIT` | `1` | Rate-limit auth endpoints (`0` to disable). |
| `NEWS_TRANSLATE_PROVIDER` | `off` | Per-user translation provider, if configured. |

### Resources

Ingestion runs continuously, so this is a **sustained** workload, not a bursty
one. That matters on Fly: shared CPUs get a burst allowance and are throttled
once it's spent, which shows up as a dashboard that takes many seconds to do
anything while the poller works.

`fly.toml` ships **shared-cpu-2x / 2 GB**, which handles ~500 city feeds plus
content extraction and clustering. If the app still feels slow, move to
**`performance-1x`** — a dedicated core with no throttling — by editing the
`[[vm]]` block. Set the size **in `fly.toml`, not with `fly scale vm`**: the
config wins on the next deploy and would silently undo a CLI change.

Cheaper than scaling up: cut the work. `NEWS_CONTENT_FETCH=0` skips fetching
article bodies (the largest single cost, and it shrinks the database, which
also speeds up search), `NEWS_RETENTION_DAYS=14` keeps the corpus smaller, and
raising `NEWS_CITY_INTERVAL` polls city feeds less often.

Never raise the machine **count** — the scheduler, SQLite database, and rate
limiter are all in-process. Scale up, never out.

### Backups

The volume holds everything. On Fly, daily volume snapshots are automatic
(5-day retention: `fly volumes snapshots list <vol-id>`). For an ad-hoc copy:

```bash
fly ssh console -C "cat /data/news.db" > backup-$(date +%F).db
```

Restore by shipping the file back to `/data/news.db` and restarting the app.

---

## First-run behavior

On first boot Delphi seeds the source catalog (86 curated sources + ~500 city
local-news feeds) and starts polling. The "sources healthy" tile reads
`…/N` until the first ticks complete, then climbs as the rolling poller works
through the city feeds and auto-discovery promotes real local outlets. No
manual step is required — create your account and it runs itself.

## Upgrading

Deploys are ordinary: schema changes are additive and applied at boot, and
sessions survive them because the signing key lives on the volume.

One exception, once: the release that added revocable sessions signs everyone
out on first boot. Tokens issued before it have no `token_version` to check, and
accepting them as version 0 would have exempted exactly the tokens revocation
exists to cancel. Users sign in again and lose nothing; if you run an instance
for other people, it is worth saying so before you deploy it.
