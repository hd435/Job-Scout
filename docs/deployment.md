# Running Job-Scout off your own machine (D-101)

The hosted service is one long-running Python process: it long-polls Telegram, scans employers when
they are due, and writes everything to one SQLite file. There is **no inbound port, no web server,
no TLS certificate and no domain** — every Telegram call is outbound. What it needs is *a small
always-on machine with a disk*, not an application platform.

**This deployment targets Oracle Cloud "Always Free"**, because the product decision is zero ongoing
hosting cost (D-101). Everything needed is in [`deploy/oracle/`](../deploy/oracle): an idempotent
installer, hardened systemd units, a health timer, a backup timer and an env template. The Dockerfile
and Fly configuration are still in the repository for other hosts, but they are an appendix now, not
the deployment path.

## 1. What the workload actually needs

| Need | Value, measured on the running deployments |
|---|---|
| CPU | One shared core, idle almost all the time; a 59-employer warm pass is seconds, a cold pass ~10 min |
| Memory | ~100–250 MB for a 10 000-role database; a cold crawl is the peak |
| Disk | 27 MB for 3 762 roles, ~60 MB for 10 000; 1 GB is years of history |
| Network | ~60–120 outbound HTTPS requests per 30-minute cycle, no inbound |
| State | One SQLite file — the only thing that must survive |
| Uptime | Always-on; a missed cycle is tolerable, a missed day is not |

## 2. What "Always Free" actually gives (checked 2026-10-25)

From Oracle's [Always Free resources documentation](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm):

| Resource | Always Free allowance | What we use |
|---|---|---|
| `VM.Standard.A1.Flex` (Arm) | 1 500 OCPU-hours + 9 000 GB-hours per month = **2 OCPU / 12 GB total** | **1 OCPU / 6 GB** |
| `VM.Standard.E2.1.Micro` (AMD x86) | Up to **2 instances**, 1/8 OCPU / 1 GB each | fallback only |
| Block volume | **200 GB total**, minimum 47 GB boot volume per instance | **47 GB** boot volume |
| Object Storage | ~20 GB (10 GB Standard + 10 GB Archive) with a monthly request allowance | nightly backups |
| Outbound data | 10 TB/month | a few hundred MB |

Constraints that matter:

- Always Free compute must be in the **home region** chosen at sign-up. Pick one in the UK/EU
  (London, Manchester, Frankfurt, Amsterdam) — it cannot be changed later without a new tenancy.
- Instances beyond the A1 limits are **disabled and then deleted after 30 days**, so total A1 usage
  must stay within 2 OCPU / 12 GB.
- "Out of host capacity" is common for Always Free shapes. If it happens, try another availability
  domain, try the `E2.1.Micro` shape instead, or retry later — **do not upgrade the account** (D-101).
- Sign-up needs a phone number and a card for identity verification; the card is not charged unless
  the account is upgraded, and we do not upgrade.

### The shape we chose, and why

**`VM.Standard.A1.Flex`, 1 OCPU / 6 GB, Ubuntu 24.04 (aarch64), 47 GB boot volume.**

- Ubuntu 24.04 ships **Python 3.12**, exactly our floor, so no PPA or source build is needed.
- 6 GB leaves room for a cold crawl over 76 employers plus SQLite's page cache; the 1 GB micro shape
  would work but leaves less headroom, and its 1/8 OCPU makes crawls noticeably slower.
- The application has **no runtime dependencies** — `dependencies = []` in `pyproject.toml` — so the
  Arm architecture only has to run CPython with the standard library's `sqlite3`, `ssl` and `json`.
  There are no wheels to match and nothing to compile. Verified by installing from `pyproject.toml`
  into a clean virtual environment and running the CLI from it.
- If A1 capacity is unavailable, **the same installer works on `E2.1.Micro`/x86_64**: nothing in
  `deploy/oracle/` is architecture-specific.

### Idle-instance reclamation — the honest position

Oracle may reclaim Always Free instances that look idle over a seven-day window: **CPU 95th
percentile below 20 %, network below 20 %, and (A1 only) memory below 20 %**. A long-polling bot is
idle by that definition — it uses a fraction of a core and a trickle of bandwidth. **We will not
generate artificial load to dodge the policy.**

So this is a real, residual availability risk, mitigated rather than denied:

1. **Re-provisioning is scripted.** `install.sh` plus a restore is ~10 minutes on a fresh instance.
2. **Backups leave the VM.** The nightly timer writes a verified copy locally and, if the OCI CLI is
   configured, uploads it to Object Storage. A reclaimed VM therefore costs data since the last
   upload, not the deployment.
3. **Failure is visible.** The health timer restarts a stale bot every 15 minutes, and
   `verify.sh`/`service status` show what is happening. Oracle emails the account before reclaiming.
4. **Shape choice does not remove the risk.** The micro shape drops the memory criterion but keeps
   the CPU and network ones, so it is a capacity fallback, not a loophole.

If reclamation becomes a practical problem, the options are a paid OCI upgrade (excluded by D-101) or
a free tier elsewhere — see §7, which does not switch silently.

## 3. Provisioning the VM (operator, once)

These steps need the account owner; nothing below them does.

1. Sign up at <https://signup.cloud.oracle.com> with a **home region in the UK/EU** (e.g. London).
   Use a real phone number and card — verification only, no charge unless the account is upgraded.
2. In the console: **Compute → Instances → Create instance**.
   - Image: **Canonical Ubuntu 24.04** (the aarch64 build is chosen automatically for A1).
   - Shape: **Ampere → VM.Standard.A1.Flex**, **1 OCPU, 6 GB** (stay at or below 2 OCPU / 12 GB in
     total across all A1 instances).
   - Boot volume: **47 GB** (the free minimum).
   - Networking: accept the default VCN/public subnet, **assign a public IPv4 address**.
   - SSH keys: upload your public key, or let the console generate a pair and download the private
     key — it cannot be downloaded twice.
3. Note the public IP and connect: `ssh ubuntu@<public-ip>`.

**Firewall:** the service opens **no inbound ports**. Narrow the security list to SSH from your own
IP:

```text
VCN → Security Lists → Default Security List → Edit ingress rules
  Source 203.0.113.7/32   IP Protocol TCP   Destination Port 22
  (delete the 0.0.0.0/0 SSH rule)
```

`ufw` on the Ubuntu image already allows SSH only. The bot needs nothing else.

## 4. Installing Job-Scout on the VM (one command)

```bash
sudo apt-get install -y git
sudo git clone https://github.com/hd435/Job-Scout.git /opt/jobscout
sudo /opt/jobscout/deploy/oracle/install.sh
```

`install.sh` is idempotent and does the whole bootstrap:

| Step | Detail |
|---|---|
| Toolchain | `python3`, `python3-venv`, `pip`; refuses anything older than 3.12 and prints the arch |
| Code | `git pull --ff-only` when `/opt/jobscout` is a checkout, and it says which commit it is on |
| Service user | system account `jobscout`, no shell, no sudo except one narrow rule |
| Layout | code `/opt/jobscout`, data `/srv/jobscout/{data,backups}`, config `/etc/jobscout` |
| Package | `.venv` + `pip install /opt/jobscout`, then imports `jobscout` to prove it works |
| Config | copies `deploy/service-config.toml` to `/etc/jobscout/` **only if absent** |
| Secrets | creates `/etc/jobscout/jobscout.env` (root, mode 600) from the template |
| Units | `jobscout.service`, `jobscout-health.{service,timer}`, `jobscout-backup.{service,timer}` |
| Safety | a `sudoers` entry letting the health script restart *only* `jobscout.service` |

The first run stops short of starting the bot, because the env file still holds a placeholder token:

```bash
sudoedit /etc/jobscout/jobscout.env      # set JOBSCOUT_TELEGRAM_TOKEN (never in chat, never in git)
sudo systemctl start jobscout
sudo /opt/jobscout/deploy/oracle/verify.sh
```

`verify.sh` loads `/etc/jobscout/jobscout.env` first and runs the CLI as the `jobscout` user, so it
sees exactly what the unit sees and a plain `sudo verify.sh` is enough. It prints the enabled and
active state, the deployed commit, the database path in use, the timer schedule, `service status`
(subscribers, alerts, sources not scanning), `service check` and the last 15 journal lines. Without
the env file the CLI would fall back to the shipped default database path and report a database that
does not exist on this host.

### Upgrades

```bash
sudo /opt/jobscout/deploy/oracle/install.sh      # pulls, re-installs the package and units, restarts
```

`install.sh` fast-forwards the checkout itself and prints `updated <old> -> <new>` (or
`already at <commit>`), so an upgrade cannot silently reinstall the previous code. A checkout with
local edits is left alone with a warning instead of being overwritten. The database, the config and
the env file are never touched by an upgrade. `verify.sh` prints the commit now running, which is
the quickest way to confirm a deployment actually moved.

## 5. Operating it

| Question | Command |
|---|---|
| Is the process alive? | `systemctl status jobscout` |
| Is it scanning? | `journalctl -u jobscout -f` — startup banner, then a line per scan |
| Who is subscribed, what was sent? | `sudo -u jobscout /opt/jobscout/.venv/bin/python -m jobscout service status` |
| Is monitoring healthy? | `… -m jobscout service check` (exit 0 = ok) |
| Did the timers run? | `systemctl list-timers 'jobscout-*'` |
| Everything at once | `sudo /opt/jobscout/deploy/oracle/verify.sh` |

`service check` is the one that matters: it returns non-zero when nothing has completed a scan for 90
minutes. The health timer runs it every 15 minutes and restarts the unit when it fails, which covers
what the unit state cannot see — a live process that has stopped scanning. The unit itself is
`Restart=always`, so a crash comes back within 10 seconds.

## 6. Backups and restore

```bash
# Nightly at 00:15 UTC via jobscout-backup.timer, running as the jobscout user:
/opt/jobscout/deploy/oracle/backup.sh
#   -> jobscout service backup --to /srv/jobscout/backups/service-<stamp>.db   (verified copy)
#   -> keeps the newest 7, prunes the rest
#   -> uploads to Object Storage when JOBSCOUT_BACKUP_BUCKET is set and the oci CLI is configured
```

`jobscout service backup` uses SQLite's online backup API, checkpoints the write-ahead log first and
verifies the copy with `PRAGMA integrity_check` plus row counts before reporting success, so it is
safe while the bot is polling.

**Off-VM copy (recommended, still free).** Create a bucket in Object Storage (Always Free allowance
~20 GB), then give the `jobscout` user a working OCI CLI:

```bash
sudo apt-get install -y python3-pip
sudo pip3 install --break-system-packages oci-cli
sudo -u jobscout -H oci setup config          # API key for your own user, stored in jobscout's home
sudo -u jobscout -H oci os bucket list        # prove it works as the service user
echo 'JOBSCOUT_BACKUP_BUCKET=jobscout-backups' | sudo tee -a /etc/jobscout/jobscout.env
```

If the CLI is not configured, the script still keeps seven local copies and says so in the journal —
a backup on the same disk protects against a mistake, not against losing the VM.

**Restore drill** (do it once, before inviting anyone):

```bash
sudo systemctl stop jobscout
sudo -u jobscout cp /srv/jobscout/backups/service-<stamp>.db /srv/jobscout/data/service.db
sudo rm -f /srv/jobscout/data/service.db-wal /srv/jobscout/data/service.db-shm
sudo -u jobscout /opt/jobscout/.venv/bin/python -m jobscout service check
sudo systemctl start jobscout && sudo /opt/jobscout/deploy/oracle/verify.sh
```

A restored database keeps every source's `baseline_scan_id`, so a restore cannot re-announce history.

## 7. If Always Free is not workable

The engineering is not Oracle-specific: `deploy/oracle/install.sh` is plain Ubuntu + systemd and runs
on any VM. If A1 capacity never becomes available, the order of preference is: try another
availability domain → try `E2.1.Micro` (also free) → keep the Windows task → only then discuss a paid
option with the Product Owner. The Dockerfile and `deploy/fly.toml` stay in the repository for that
last case; they are not the plan.

## 8. Moving off the Windows deployment

Telegram allows **one** `getUpdates` consumer per bot token: two live deployments would fight (HTTP
409) and both would scan. The order is fixed:

1. Provision the VM and run `install.sh`; fill the token; **do not start** the service yet.
2. `sudo systemctl stop jobscout` on the VM if the installer already started it.
3. Stop the Windows task: `Stop-ScheduledTask -TaskName "Job-Scout bot"` (and kill the wrapper's
   `python.exe` child if it survives).
4. `sudo systemctl start jobscout` on the VM, then `sudo /opt/jobscout/deploy/oracle/verify.sh`.
5. Confirm in the journal that the first pass baselines the offered employers, and send one Telegram
   message to the bot to prove the inbound path from the new host.
6. Keep `data/service/service.db` and `data/ops/logs` on the Windows machine for a fortnight.

There is no need to copy the Windows database: a fresh baseline is clean, costs one pass, and avoids
carrying history into a deployment that has no users yet. The one thing that carries over is your own
scope — send `/settings` and choose employers again if you want the expanded set.

## Appendix — a managed host (not used)

`Dockerfile`, `.dockerignore` and `deploy/fly.toml` build and run the same service as a single managed
container (≈ $4.40/month in London). They are kept because they cost nothing to keep and make the
last-resort option a one-command deployment, but **the approved target is Oracle Always Free**
(D-101), and nothing in this repository deploys there automatically.
