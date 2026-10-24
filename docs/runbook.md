# Runbook

Operating instructions for a **single-user, personal-use** deployment on Windows.

**Current release:** v0.4 beta (two live sources) while [v0.5](./iterations/v0.5-configurable-scope.md) is under development. The configuration keys in ?2a are the current schema; the upcoming `[preferences]` keys are specified in the v0.5 iteration file and do not exist yet. This
document is preparation only: **no trial has started, no scheduled task exists, and no
unattended polling is authorised.** The seven-day trial (M5.4) is blocked on the per-source
permission decisions in [source-access.md](./source-access.md) and D-027.

Everything below runs locally against SQLite. Nothing contacts a third party except the
employer sources the configured adapters already call, and Telegram if credentials are set.

## 1. What exists today

| Command | Purpose | Leaves state behind |
|---|---|---|
| `jobscout scan --source <id> --dry-run` | One manual scan, no delivery | Yes: a scan row and job state |
| `jobscout schedule --once` | Run every source that is due, then exit | Yes: scans, deliveries, notices, `next_run_at` |
| `jobscout schedule` | Loop until interrupted | Same, continuously |
| `jobscout deliver` | Deliver queued alerts | Yes: notification states |
| `jobscout status` | Source and scan status | No |
| `jobscout report --days 7` | Trial scorecard | No data; applies pending schema migrations if the file is from an older version |
| `jobscout prune --dry-run` | Predict one cleanup pass | No |
| `jobscout prune` | Apply retention | Yes: deletes/strips history |
| `jobscout validate --source <id> --confirm-live` | One bounded request set to one source (palantir, monzo = 1 request; lloyds_graduate = listing pages only, max 6) | Only capped artifacts under `data/validation/` |

Credentials come from `JOBSCOUT_TELEGRAM_TOKEN` and `JOBSCOUT_TELEGRAM_CHAT_ID`, from the
environment or from `.env` (gitignored). Without them, `schedule` and `scan` still run but
queue no alerts and send no failure notices.

## 2. Rehearsal without contacting any employer

`jobscout scan --fixture` builds a payload-backed HTTP client instead of a network one
(`FixtureHttpClient`), so nothing leaves the machine. Use a scratch database so a rehearsal
never touches the real one:

```powershell
$scratch = Join-Path $env:TEMP "jobscout-rehearsal.db"
python -m jobscout scan --source palantir --fixture tests\fixtures\palantir_jobs.json `
  --db $scratch --dry-run
python -m jobscout status --db $scratch
python -m jobscout report --days 1 --db $scratch
python -m jobscout prune --dry-run --db $scratch
```

A pass looks like `Status: success (complete) records: seen=22 parsed=22 skipped=0`, then
`report` shows one completed scan, and `prune --dry-run` reports nothing to remove. `status`
lists the sources from your configuration but only the scratch database's history, so the
five sources you have not scanned here correctly show "No scans recorded yet". Delete
`$scratch` afterwards.

What this rehearsal does **not** cover: scheduling, `next_run_at`, delivery and failure
notices — those only happen through `schedule`, which is live.

### Live scheduling is gated

`jobscout schedule --once` and `jobscout schedule` contact the configured employer endpoints
with the real adapters; there is no fixture switch. Running either is therefore an
**unattended live request per source**, which requires the per-source permission decision
(D-027) and your explicit approval. Until then, do not run them — not even once "to check the
output". The rehearsal above and the offline tests are the substitute.

Everything above still assumes the sources you configure are ones you are permitted to call.
`status`, `report` and `prune --dry-run` are safe because they only read the local database.

## 2a. Personal-use beta: what is actually running

This section describes the one deployment that exists, rather than what could exist. All of
it lives outside Git, so a fresh clone is unaffected.

| Piece | Where |
|---|---|
| Scope config (Monzo + Lloyds Graduate only) | `data/ops/config.toml` — sources plus the pinned London scope in `[preferences]` (section 2b) |
| Recurring wrapper | `data/ops/run-once.cmd` |
| Pass log | `data/ops/logs/schedule.log` |
| Scheduled task | `Job-Scout scan`, every 5 minutes, current user, interactive |

**Current state: the task exists but is disabled.** It was disabled on 2026-10-24 so that v0.5
changes land without a scan running mid-edit. Re-enable it with
`schtasks /Change /TN "Job-Scout scan" /ENABLE` when you want the beta running again; the
definition and the database are untouched while it is off.

The task runs `jobscout schedule --once`; each pass scans only sources whose cadence has
elapsed (30 minutes), delivers any queued alerts, and exits. `MultipleInstancesPolicy` is
`IgnoreNew`, so an overlapping pass is skipped rather than stacked — and the source claim in
the database would prevent a duplicate scan anyway.

Common commands:

```powershell
# Is it running and when did it last fire?  (Last Result 0 = success)
schtasks /Query /TN "Job-Scout scan" /FO LIST /V | Select-String "Status|Last Run|Last Result|Next Run"

# What happened in the last passes?
Get-Content data\ops\logs\schedule.log -Tail 20

# Current per-source state: last scan, failures, next run, match counts
python -m jobscout status --config data\ops\config.toml

# Stop it (keeps the definition, disables it)
schtasks /Change /TN "Job-Scout scan" /DISABLE
# Start it again
schtasks /Change /TN "Job-Scout scan" /ENABLE
# Remove it entirely
schtasks /Delete /TN "Job-Scout scan" /F

# Run one pass right now
schtasks /Run /TN "Job-Scout scan"
```

Recovery:

- **Nothing is arriving.** Check `Last Result` and the log tail, then `status`. A stale
  `Last Run` means the task is disabled or the laptop was asleep; `schtasks /Run` proves the
  wrapper still works. If the log shows `Telegram is not configured`, the `.env` file was
  moved or the token is invalid.
- **A source shows a growing failure count.** `status` names the error. Three consecutive
  failures send one Telegram notice; a completed scan clears it.
- **Everything looks healthy but no alerts.** Expected unless a genuinely new role appears:
  baselined roles never re-alert, by design. Use `summary --config data\ops\config.toml
  --current` to see what is currently matching.
- **Fewer matches than expected.** The operating scope is pinned to London (section 2b), so a
  Manchester-only or Edinburgh-only role is an exclusion, not a miss. `summary` lists matching
  and needs-review roles; `jobscout config show --config data\ops\config.toml` prints the
  effective scope.

## 2b. Changing the job-search scope

The scope is configuration, not code. Edit `[preferences]` in `data\ops\config.toml` (the full
key-by-key reference is [configuration.md](./configuration.md)), then:

```powershell
python -m jobscout config validate --config data\ops\config.toml   # will it load?
python -m jobscout config show --config data\ops\config.toml       # what will it do?
```

Both are read-only and make no network request. The new scope applies from the **next scan** of
each source, because every scan re-decides every role it sees. Changing preferences never
re-alerts an existing role: baselines and sent notifications are untouched.

The shipped defaults are `mode = "uk"`, every approved role family, and the five early-career
employment types. They live in `src/jobscout/matching.py`, so a fresh clone is never silently
narrowed to one operator's preferences; the London scope below lives only in the gitignored
ops file.

The beta deliberately runs:

```toml
[preferences.locations]
mode = "pinned"
cities = ["London"]
remote_uk = true
hybrid_uk = true
```

Measured on 2026-10-24 against the 105 roles already stored: this keeps 4 current matches
(2 Monzo internships and 2 London Lloyds schemes) and excludes 13 Lloyds schemes that sit in
Manchester, Leeds, Edinburgh, Bristol, Chester and Halifax. Switch `mode` to `"uk"` to widen
back to the whole UK.

## 3. Windows Task Scheduler setup (reference for creating or recreating it)
Two shapes work. **Option B is recommended for a first trial** because supervision comes for
free from Task Scheduler and each pass is short-lived.

Both shapes are **live scheduling**. A task of this shape is already running (section 2a); this
section exists so it can be recreated on another machine. The operator has accepted the blanket
personal-use access posture for the configured employers (D-066), which replaces the earlier
per-source permission gate.

### Option B — a short-lived pass every five minutes (recommended)

Each pass runs only the sources that are due and exits. Overlapping passes are harmless:
every source turn is claimed in SQLite before the scan starts (D-063), so a second process
cannot scan the same source in the same cycle.

1. Create the wrapper so no quoting is needed in the task definition. Save as
   `scripts\run-once.cmd` in the repository (the operator creates this file; it is not
   generated by the code):

   ```bat
   @echo off
   setlocal
   cd /d "D:\Mini Project\Job-Scout"
   if not exist logs mkdir logs
   ".venv\Scripts\python.exe" -m jobscout schedule --once >> "logs\schedule.log" 2>&1
   exit /b %ERRORLEVEL%
   ```

2. Register the task. Run this **only when the trial is approved**; it changes this machine:

   ```powershell
   schtasks /Create /TN "Job-Scout scan" /TR "\"D:\Mini Project\Job-Scout\scripts\run-once.cmd\"" `
     /SC MINUTE /MO 5 /RU "$env:USERNAME" /IT /F
   ```

   `/IT` runs it in the interactive session, which keeps access to the user profile and
   `.env`. Use `/RL LIMITED` if you need a non-elevated context.

3. Make the task resilient and non-overlapping (Task Scheduler GUI, or XML export):
   - **Settings → If the task fails, restart every:** 1 minute, up to 3 times.
   - **Settings → Stop the task if it runs longer than:** 15 minutes (a pass should take
     seconds; this bounds a hung network call).
   - **Settings → If the task is already running:** *Do not start a new instance*.
   - **Conditions → Start the task only if the computer is on AC power:** unchecked, or the
     task will not run on battery laptops.

### Option A — one long-running loop

```powershell
schtasks /Create /TN "Job-Scout loop" /TR "\"D:\Mini Project\Job-Scout\scripts\run-loop.cmd\"" `
  /SC ONSTART /RU "$env:USERNAME" /IT /F
```

where `run-loop.cmd` calls `python -m jobscout schedule` (no `--once`) with the same logging
redirection. This needs a supervisor: Task Scheduler's "restart on failure" only fires when
the process exits, so a hung process is invisible. Option B avoids that class of failure.

### Adding jitter

Six sources on the same cadence would otherwise hit their endpoints on the same second.
`--jitter-seconds 120` spreads each source's next run by up to two minutes. Default is 0.

## 4. Logging and evidence

- Redirect stdout **and** stderr to an append-only file, as above. The `.cmd` wrapper's `>>`
  is raw `cmd.exe` redirection, so the CLI's UTF-8 output is stored as UTF-8.
- **PowerShell 5.1 caveat:** `>` and `>>` in PowerShell re-encode, writing UTF-16LE with a
  BOM (verified on 5.1: the file starts `FF FE`). For PowerShell, use
  `... | Out-File -Encoding utf8 -Append logs\report.txt`, or run the redirect through
  `cmd /c`. Do not mix both styles in one log file.
- Rotate by size: keep `logs\schedule.log`, and when it passes ~10 MB rename it to
  `schedule-YYYY-MM-DD.log`. Nothing rotates automatically.
- Keep the scorecard as evidence:
  `python -m jobscout report --days 7 | Out-File -Encoding utf8 logs\report-<date>.txt`.
  It changes no data, so running it daily is safe.

## 5. Detecting a process that has stopped

There is no heartbeat table, by design (D-064). Three signals, cheapest first:

1. **Task Scheduler history** (`Get-ScheduledTaskInfo -TaskName "Job-Scout scan"`) shows the
   last run time and last result. A stale `LastRunTime` or repeated non-zero
   `LastTaskResult` is the earliest signal.
2. **Log freshness:** `Get-Item logs\schedule.log | Select-Object LastWriteTime`. With a
   five-minute task the file should never be older than ~15 minutes.
3. **Scan freshness:** `python -m jobscout report --days 1` shows per-source
   `scans N/expected`, the longest gap, and `missed episodes`. A gap longer than about twice
   the cadence means the schedule stopped, the machine slept, or the source is failing.

`python -m jobscout status` additionally shows consecutive failures, the last error, and the
failure-notice outcome per source.

## 6. Retention operations (opt-in, never scheduled)

Pruning is bounded, idempotent, and never runs during a scan or from the scheduler (D-061,
D-062). The four horizons are engineering defaults that the Product Owner has **not yet
ratified** (D-025); do not run a destructive prune on a database you still need as trial
evidence without first taking a copy.

Safe workflow, monthly or after the trial:

```powershell
# 0. Point both variables at the real database and at the copy you want.
$env:JOBSCOUT_DB = "data\jobscout.db"
$env:JOBSCOUT_BACKUP = "data\jobscout-backup-$(Get-Date -Format yyyyMMdd-HHmm).db"

# 1. Take a consistent copy WITHOUT stopping anything: SQLite writes a snapshot itself.
python -c "import os,sqlite3; src=sqlite3.connect(os.environ['JOBSCOUT_DB']); dst=sqlite3.connect(os.environ['JOBSCOUT_BACKUP']); src.backup(dst); dst.close(); src.close()"

python -m jobscout prune --dry-run      # 2. predict one pass
python -m jobscout prune                # 3. apply one pass
python -m jobscout prune --dry-run      # 4. repeat if it printed "More historical data remains"
```

The `sqlite3.Connection.backup` call is WAL-safe and consistent even while the database is in
use; `VACUUM INTO '<path>'` is an equivalent one-liner on SQLite 3.27+.

**Do not copy `jobscout.db` on its own while anything is running.** The database runs in WAL
mode, so committed pages can live in `jobscout.db-wal` and a plain file copy can be stale or
inconsistent. If you prefer a filesystem copy, first stop every jobscout process (disable the
Task Scheduler entry, confirm no `python` process is running), then copy `jobscout.db`,
`jobscout.db-wal`, and `jobscout.db-shm` together.

Rules:

- Read the dry run before applying. Its counts predict **one** real invocation under the same
  `--batch-size`/`--max-batches`, not the whole backlog.
- A run that prints "More historical data remains" is incomplete by design; run it again.
- Do not add `prune` to Task Scheduler while the horizons are unratified.
- `prune` never deletes open roles, source rows, or any scan a role or source still points at,
  so deduplication, reopening, and closure stay correct.

## 7. Seven-day trial measurement

The trial answers one question: **did the bot reliably tell me about new matching roles for a
week without daily babysitting?** Measure with `jobscout report --days 7` at the end (and
daily, into the log). What each figure means, and what "good" looks like:

| Measure | Where it comes from | Target for a pass |
|---|---|---|
| Scans per expected cycle | `scans N/expected` per source | ≥ 95% for every enabled source |
| Completed vs partial vs failed | `complete / partial / failed` | Complete dominates; failures are isolated and explained |
| Missed episodes | Gaps longer than 1.5 × cadence | 0, or each one explained (machine asleep, source outage) |
| Mean delay | Gap minus cadence | Within a few minutes; large positive values mean the schedule is lagging |
| Consecutive failures | Per-source state | Never stuck at ≥ 3 (that would mean an unnoticed outage) |
| Delivery outcomes | `sent / failed / ambiguous / pending` | `failed` and `ambiguous` are zero, or each has a recorded reason |
| Recovery after restart | A missed episode followed by resumed scans | Every restart resumes within one cadence, with no catch-up burst |

Availability is a proxy: nothing records a process heartbeat, so "up" means "scans were
written when the cadence said they should be". That is the honest interpretation, and the
report states it.

Two reading caveats:

- `report` lists every source in the database so history stays visible, and marks the ones
  that are disabled with `[disabled: not a trial target]`. Only sources without that marker
  count toward the targets above; a disabled source showing 0% is expected, not a failure.
- A source added part-way through the window looks under-covered because its expected cycles
  are counted for the whole window. Fix the source set before starting the trial.
- Expected cycles assume the machine was meant to be running for the whole window. Scheduled
  downtime (a laptop switched off overnight) shows up as missed episodes; note it in the
  evidence rather than treating it as a defect.

## 8. Standing posture and limits

- **Access:** the operator has accepted a blanket personal-use risk posture for any employer using a
  public ATS or careers endpoint (D-066). Robots rules, rate limits and non-evasion remain engineering
  hygiene, and technically hostile sources are still excluded by engineering judgement. This is a risk
  decision, not a claim that any site's terms grant permission.
- **Retention:** horizons are as implemented and accepted as-is (D-067); `prune` stays opt-in.
- **Alert volume:** immediate per-role alerts with no cap, unchanged for now (D-070).
- **Coverage:** beyond the six researched employers, expansion is operational work (D-066), not a
  milestone.
- **Supervision:** none beyond Task Scheduler's own restart settings; section 5 is the substitute.
- **Known blind spot:** a source that returns `partial` every cycle neither alerts nor counts as a
  failure, so it can look healthy while never alerting. Watch `report` for complete-versus-partial
  drift when a source goes quiet.

Nothing here is deployed by the repository: the task in section 2a was created by the operator.
