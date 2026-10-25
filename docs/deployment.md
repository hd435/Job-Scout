# Running Job-Scout somewhere other than your desk (D-099)

The hosted service is one long-running Python process: it long-polls Telegram, scans employers when
they are due, and writes everything to one SQLite file. There is **no inbound port, no web server,
no TLS certificate and no domain** — Telegram calls are outbound, so nothing has to be exposed. That
shapes every hosting decision below: what we need is *a small always-on machine with a disk*, not an
application platform.

Everything here is delivered as code: [`Dockerfile`](../Dockerfile),
[`.dockerignore`](../.dockerignore), [`deploy/fly.toml`](../deploy/fly.toml) and
[`deploy/service-config.toml`](../deploy/service-config.toml). The image runs the same
`python -m jobscout bot` that the Windows deployment runs.

## 1. What the workload actually needs

| Need | Value, measured on the pilot deployment |
|---|---|
| CPU | One shared core is idle almost all the time; a full 12-employer pass took 23 s |
| Memory | Well under 256 MB for a 3 700-role database; 512 MB leaves room for a wide crawl |
| Disk | 27 MB for 3 762 roles and a month of scans; 1 GB is years of history |
| Network | ~100 outbound HTTPS requests per 30-minute cycle at 60 employers, no inbound |
| State | One SQLite file (`/data/service.db`) — the only thing that must survive |
| Uptime | Always-on; a missed cycle is tolerable, a missed *day* is not |

## 2. The options, with real prices (2026-10-25)

| Option | Cost | Always-on? | Persistent disk | Verdict |
|---|---|---|---|---|
| **Fly.io** (chosen) | $2.19/mo for `shared-cpu-1x`/256 MB, $3.69 for 512 MB, `lhr` ≈ 1.15× → **≈ $4.25**; 1 GB volume $0.15 → **≈ $4.40/month (≈ £3.40)** | Yes — a Machine runs until you stop it; no spin-down | Yes, volumes | Right shape: no OS to patch, restarts by itself, secrets and logs built in |
| Hetzner / DigitalOcean VPS | €3.79–$6/mo | Yes | Yes (local disk) | Cheapest per GB and fully ours, but we own OS patching, systemd units and monitoring |
| Render (free web service) | $0 | **No** — spins down after 15 min idle, no disk on free | No | Disqualified: a spun-down bot receives nothing and loses its database |
| Render background worker | $7/mo | Yes | Disk extra | Works, costs more than Fly for the same shape |
| Railway | Hobby $5/mo (usage-based, $5 credit) | Yes if the service keeps running | Volume billed separately | Fine, but a container platform with more moving parts than we use |
| Koyeb / Northflank free tiers | $0 → scales to zero | **No** for a long-polling worker | Limited | Scaling to zero is exactly wrong here |
| Oracle Cloud "Always Free" ARM VM | $0 | Yes | Yes | Genuinely free and capable, but signup is fiddly (card + phone), capacity is region-dependent and you still administer the OS |
| Cloudflare Workers + cron | ~$5/mo | Cron only, no long polling | Durable Objects, not SQLite | Would need the service rewritten; rejected |
| GitHub Actions cron | free minutes | **No** — scheduled runs are routinely 5–30 min late, no durable state between runs | Artifacts only | Not dependable enough for "you were told within the hour" |

**Decision: Fly.io, London (`lhr`), one 512 MB Machine and a 1 GB volume.** It is the smallest
always-on deployment with a real disk, it survives restarts without us writing supervisors, and the
Dockerfile keeps the VPS path open if the price or the platform ever stops suiting us. Section 4
explains how to run the same image on a VPS instead.

## 3. Deploying to Fly

Prerequisites: a Fly account with a payment method, and `flyctl` (`iwr https://fly.io/install.ps1
-UseBasicParsing | iex` on Windows, `curl -L https://fly.io/install.sh | sh` elsewhere).

```bash
# 1. One-time: create the app and the disk that holds the database
fly apps create job-scout
fly volumes create jobscout_data --region lhr --size 1

# 2. Secrets: the Telegram token is the only credential. In service mode each user has their own
#    chat, so no chat id is needed.
fly secrets set JOBSCOUT_TELEGRAM_TOKEN=123456789:AA...

# 3. Deploy from the repository root
fly deploy --config deploy/fly.toml

# 4. Watch the first pass baseline every employer
fly logs
```

`deploy/fly.toml` contains no `[http_service]`, `[services]` or port, deliberately: there is nothing
to route to. It declares one process (`python -m jobscout bot`), a 512 MB `shared-cpu-1x` Machine, the
volume mounted at `/data`, and a script health check (`jobscout service check`) every ten minutes.

Image details that matter:

- **`JOBSCOUT_DATABASE=/data/service.db`** — the database lives on the volume, never in the image.
- **`JOBSCOUT_CONFIG=/app/service-config.toml`** — the employer selection ships with the deployment,
  so no file has to be written on the host. Explicit `--config`/`--db` arguments still win.
- **Unbuffered stdout** (`PYTHONUNBUFFERED=1`) so `fly logs` is useful during a crash.
- **SIGTERM stops the bot cleanly**, so a restart does not interrupt a scan mid-transaction.

## 4. Running the same image on a VPS instead

```bash
docker build -t jobscout .
docker run -d --name jobscout --restart unless-stopped \
  -v /srv/jobscout/data:/data \
  -e JOBSCOUT_TELEGRAM_TOKEN=123456789:AA... \
  --memory 512m \
  jobscout
```

Or without Docker: a systemd unit exactly like [runbook §9.2](./runbook.md) with
`Environment=JOBSCOUT_DATABASE=/srv/jobscout/data/service.db`.

## 5. Verifying a deployment

Run these in order; each answers a different question.

| Question | Command |
|---|---|
| Is the process alive? | `fly status` — one Machine, state `started` |
| Is it scanning? | `fly logs` — startup banner, then one line per scan when a source is due |
| Has it stored anything? | `fly ssh console -C "python -m jobscout service status"` |
| Is monitoring healthy? | `fly ssh console -C "python -m jobscout service check"` (exit 0 = ok) |
| Do the checks agree? | `fly checks list` |
| Is Telegram reachable from there? | `fly ssh console -C "python -m jobscout notify-test"` |

`service check` is the one to automate: it returns non-zero when nothing has completed a scan
within 90 minutes (configurable), which is the failure that actually matters — an idle bot with no
messages from anyone is healthy, and a bot that stopped scanning is not.

## 6. Backups

```bash
# Take and verify a backup inside the volume (safe while the bot is running)
fly ssh console -C "python -m jobscout service backup --to /data/backups/service.db"

# Copy it off the platform — a backup on the same disk is not a backup
fly sftp get /data/backups/service.db ./service-backup.db
```

`jobscout service backup` uses SQLite's online backup API, checkpoints the write-ahead log first, and
verifies the copy with `PRAGMA integrity_check` and row counts before reporting success.

Keep a copy off Fly at least weekly (and one before any risky change). Restoring is in
[runbook §9.3](./runbook.md): stop the Machine, replace `/data/service.db`, delete `-wal`/`-shm`,
start again. A restored database keeps every source's baseline, so a restore cannot re-announce
history.

## 7. Restarts, recovery and what is *not* covered

| Situation | What happens |
|---|---|
| The process crashes | Fly restarts the Machine (restart policy `always`) |
| The health check fails 3× | Fly replaces the Machine |
| You deploy a new image | One Machine rolls to the new release; the volume stays |
| The volume is lost | Restore the last off-platform backup; the database is the only state |
| The account is unpaid / suspended | Nothing runs. This is a hosted dependency we now accept |
| Telegram is unreachable | The bot retries with a growing back-off; scans continue; alerts queue |

Not covered by design: horizontal scaling (one process is the design — two would double-poll the
same token and fight over the database), and multi-region failover (the pilot has one region).

## 8. Moving off the Windows deployment

Telegram allows **one** `getUpdates` consumer per bot, so two live deployments would fight (HTTP 409)
and both would scan the same employers. Order matters:

1. Deploy to Fly and let it baseline (`fly logs` shows the pass finishing).
2. Verify: `service status` on Fly shows the same offered employers and a subscriber count of zero.
3. Stop the Windows task: `Stop-ScheduledTask -TaskName "Job-Scout bot"`.
4. Tell testers nothing yet — the bot identity does not change.
5. Keep `data/service/service.db` and its backups for a fortnight as a fallback, then retire them.

There is no need to copy the Windows database: a fresh baseline is clean, costs one extra pass, and
avoids carrying 27 MB of history into a deployment that has no users yet.

## 9. Cost control

At pilot scale the bill is one Machine plus one volume: **≈ $4.40 per month**. Things that would
raise it, and why we are not doing them: `performance-*` Machines (not needed — CPU is idle),
dedicated IPv4 (no inbound traffic), a second Machine (one process is the design), Managed Postgres
($38/month for something SQLite does for free at this size).
