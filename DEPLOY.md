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
| `NEWS_DB_MAX_FRACTION` | `0.7` | Share of the volume the news archive may occupy before the oldest articles start being dropped to fit. A fraction rather than a fixed size so provisioning a bigger volume raises the ceiling by itself. `NEWS_DB_MAX_MB` overrides it with an absolute figure. |
| `NEWS_LOW_SPACE_MB` | `128` | Free space below which ingestion **pauses**. Delphi keeps serving and keeps pruning, but stops adding — which leaves enough room for the database to be opened at all on the next restart. Getting this wrong in the low direction is how the app becomes unstartable. |
| `NEWS_MIN_KEEP_DAYS` | `2` | Articles newer than this are never dropped, whatever the disk says. Without a floor, a database that cannot shrink would be pruned to nothing chasing a number it can't move. |
| `NEWS_RETENTION_DAYS` | `30` | Ordinary age-based retention. Size-based pruning above is a backstop, not a replacement. |
| `NEWS_HSTS_MAX_AGE` | `86400` (1 day) | How long browsers are told to reach this site over https only. Deliberately short to begin with: HSTS cannot be withdrawn faster than the max-age already handed out, so a bad certificate with a long max-age locks visitors out. Raise it (e.g. `31536000`) once the certificate is settled. `0` disables it. Add `includeSubDomains`/preload by hand only when every subdomain is ready to be https-only permanently. |
| `NEWS_CORS_ORIGINS` | Off | Comma-separated origins allowed to call the API cross-origin. Off by default because Delphi serves its own frontend from the same origin and nothing needs it. Set it only if you're building a separate browser client. |
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

#### Automatic snapshots are OFF on delphi-news.com, deliberately

Read this before turning them back on.

A snapshot reads the whole volume. On a single-core machine whose every request
needs SQLite on that same volume, a big one saturates the disk for as long as it
takes, and during it the app answers nothing: `/healthz` misses the 10-second
timeout, Fly de-routes the machine, and the site reports itself unreachable
while the process is perfectly healthy. That is what the nightly "can't reach
the server" was — for days, at 23:05 UTC, with nothing left in the one-minute
log buffer by the time anyone looked.

It scaled with the database, which is why it appeared out of nowhere and then
got worse:

| snapshot | size | site |
|---|---|---|
| 2026-08-04 23:25 | 3.40 GB | fine |
| 2026-08-05 23:26 | 3.92 GB | fine |
| 2026-08-06 23:27 | 4.70 GB | fine |
| 2026-08-07 23:27 | 5.59 GB | **down 85 min** |
| 2026-08-08 23:28 | 6.48 GB | **down 4 h 03** |

Measured alongside it: a day of news costs about **0.84 GB**, confirmed twice
over — 6.67 GB holding ~8 days of articles, and the snapshot growing
0.85 GB/night. So the trade is direct, and there is no third way. Trimming
stored article text does not work (average body is already 1,332 characters
against the 20,000 cap, so capping it lower saves ~7% of the file — text is
only a third of the database, the rest being the FTS index, the JSON place and
category columns, five indexes, and the events table). More CPU does not work
either: `performance-2x` would be two cores waiting on the same disk.

Disabled with:

```bash
fly volumes update <vol-id> --scheduled-snapshots=false
```

**What this costs.** There is no automatic backup. Losing the volume means
rebuilding the archive from the feeds, which only reaches back as far as the
feeds themselves do — days, not weeks. The news is re-fetchable; **accounts,
feeds, alerts, pantheons and watched places are not**, and they are a few
megabytes of the six gigabytes. If you take one thing from this section, take a
copy of those.

To turn snapshots back on, first get the database under about 4.5 GB — the
level the nights that stayed up were at — by lowering `NEWS_DB_MAX_MB` (an
explicit ceiling beats `NEWS_DB_MAX_FRACTION`, and it decouples the cap from
the volume size so growing the volume for vacuum headroom does not grow the
snapshot). At 0.84 GB/day that is about five days of history. The alternative
is to watch fewer sources: 4,655 enabled feeds produce ~170k articles a day,
and halving that doubles the days of history per gigabyte.

---

## First-run behavior

On first boot Delphi seeds the source catalog (86 curated sources + ~500 city
local-news feeds) and starts polling. The "sources healthy" tile reads
`…/N` until the first ticks complete, then climbs as the rolling poller works
through the city feeds and auto-discovery promotes real local outlets. No
manual step is required — create your account and it runs itself.

## Disk

The archive lives on the mounted volume and grows with the news. Two things are
worth knowing, because between them they took the site down once:

**Deleting rows does not shrink a SQLite database.** Freed pages are reused
inside the file; the file keeps its high-water mark. Retention had been pruning
correctly for weeks and the volume filled anyway. Delphi now converts the
database to incremental auto-vacuum on its first poll and hands freed pages back
to the filesystem as it prunes — but the conversion itself needs free space
roughly equal to the database (it rewrites it), so it refuses on a full disk and
says so in the log. **If the volume is already full, add space first**; the
cleanup cannot dig you out from inside.

**A full disk stops the app from starting at all**, not just from writing:
SQLite fails on `PRAGMA journal_mode=WAL`, so the process never opens a port and
every request becomes a 503. Ingestion now pauses at `NEWS_LOW_SPACE_MB` free
to avoid reaching that point, disk usage shows in the operator console's Service
health, and `/healthz` (wired to a Fly health check) fails when the database
cannot be read — so a deploy into this state fails loudly instead of reporting
success.

To grow the volume:

```bash
fly volumes list -a <app>
fly volumes extend <vol_id> -s 3 -a <app>   # gigabytes; volumes cannot shrink
fly machine restart <machine_id> -a <app>
```

## Upgrading

Deploys are ordinary: schema changes are additive and applied at boot, and
sessions survive them because the signing key lives on the volume.

Dependencies do not move on their own. `requirements.txt` is a lock — every
package pinned to one version and to the hash of the file it must be — and the
image installs with `--require-hashes`, so a build gets that exact set or
fails. Two builds of the same commit are therefore the same build, which they
previously were not.

That means a build can fail on a dependency change rather than a code one. If
it does, the message names the package and the mismatch; recompile the locks
from `requirements.in` (see the Dependencies section of the README) rather than
relaxing the flag. Updates arrive as Dependabot pull requests that CI tests
first.

One exception, once: the release that added revocable sessions signs everyone
out on first boot. Tokens issued before it have no `token_version` to check, and
accepting them as version 0 would have exempted exactly the tokens revocation
exists to cancel. Users sign in again and lose nothing; if you run an instance
for other people, it is worth saying so before you deploy it.
