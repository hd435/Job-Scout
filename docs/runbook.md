# Runbook

Operating instructions for a **single-user, personal-use** deployment on Windows.

**Current state:** a limited personal-use beta is **running** on the operator's machine — 14
enabled employers from the catalogue, scanning on a 30-minute cadence through the Windows
scheduled task, with Telegram delivery and failure notices enabled. v0.5 (configurable scope)
and v0.6 (scale-out machinery) are delivered; the outstanding MVP work is coverage towards
roughly 100 employers and the seven-day reliability measurement, both described in
[roadmap.md](./roadmap.md) and [prd.md](./prd.md). Section 2a describes exactly what is running.

The beta is authorised by the Product Owner's blanket personal-use risk acceptance (D-066),
which replaces the earlier per-source permission review. That is a risk decision for this
single-user deployment, not a claim that any employer's terms permit automated polling, and it
does not extend to a public or multi-user service. Each source records its access basis in its
catalogue entry (D-087).

Everything below runs locally against SQLite. Nothing contacts a third party except the
employer sources the configured adapters call, and Telegram if credentials are set.

## 1. What exists today

| Command | Purpose | Leaves state behind |
|---|---|---|
| `jobscout scan --source <id> --dry-run` | One manual scan, no delivery | Yes: a scan row and job state |
| `jobscout schedule --once` | Run every source that is due, then exit | Yes: scans, deliveries, notices, `next_run_at` |
| `jobscout schedule` | Loop until interrupted | Same, continuously |
| `jobscout deliver` | Deliver queued alerts | Yes: notification states |
| `jobscout status` | Source and scan status (`--brief` for one line per source) | No |
| `jobscout sources list` | Every catalogued source, enabled or not, with adapter, state and validation date | No |
| `jobscout report --days 7` | Trial scorecard and per-source coverage | No data; applies pending schema migrations if the file is from an older version |
| `jobscout notify-test` | One labelled Telegram message to prove the delivery path works | One `delivery_checks` row; no job or alert state |
| `jobscout config show` / `config validate` | The effective scope, and every value that would not take effect | No |
| `jobscout prune --dry-run` | Predict one cleanup pass | No |
| `jobscout prune` | Apply retention | Yes: deletes/strips history |
| `jobscout validate --source <id> --confirm-live` | One bounded request set to any configured source (1 request for Lever/Greenhouse, up to 6 for Workday, 5 for sitemap) | Only capped artifacts under `data/validation/` |

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

### Live scheduling contacts employers

`jobscout schedule --once` and `jobscout schedule` contact the configured employer endpoints
with the real adapters; there is no fixture switch. A pass is therefore an **unattended live
request per due source**. On this machine that is authorised: the Product Owner accepted the
personal-use access risk for this single-user deployment (D-066), and the recurring task in
section 2a already does it. On a fresh clone it is not authorised by default — the shipped
catalogue is a starting point, not permission, and each operator makes their own decision
before enabling sources.

`status`, `report`, `sources list` and `prune --dry-run` are safe because they only read the
local database.

## 2a. Personal-use beta: what is actually running

This section describes the one deployment that exists, rather than what could exist. All of
it lives outside Git, so a fresh clone is unaffected.

| Piece | Where |
|---|---|
| Scope config (17 catalogued, 14 enabled) | `data/ops/config.toml` — the pinned London scope in `[preferences]` (section 2b) and the `disable` list; the employer list itself lives in the catalogue |
| Recurring wrapper | `data/ops/run-once.cmd` |
| Pass log | `data/ops/logs/schedule.log` |
| Scheduled task | `Job-Scout scan`, every 5 minutes, current user, interactive |

**Current state: enabled and Ready**, running the 14 enabled catalogue employers with the pinned
London scope — the six researched sources plus expansion batch 1, less three batch sources
switched off for contributing nothing (section 7c). Measured on 2026-10-24 straight after batch 1
was baselined: 23 current matches (Palantir 11, Wise 5, Lloyds 2, Monzo 2, Stripe 2, Deliveroo 1,
the rest 0) and 119 needs-review roles. Arm contributes nothing under a London-only scope because
almost all its roles are in Cambridge, Lund and Manchester; that is the scope working, not a
fault. The batch as a whole added 3 047 stored roles for 3 alert-eligible matches, so scope — not
source count — is the current constraint.

Two things are **not** yet proven in production, and this section should keep saying so until
they are: no alert has been delivered for a newly discovered matching role (the `notifications`
table has stayed empty because every match so far arrived in a baseline scan), and the seven-day
reliability measurement is still in progress. `jobscout notify-test` closes the first half of
that gap — it proves the token, chat id and transport work — but not the matching-to-alert path
(D-088).

`schtasks /Change /TN "Job-Scout scan" /DISABLE` stops it without deleting anything.

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

# Prove the Telegram path works (one labelled message; no job, no alert state)
python -m jobscout notify-test --config data\ops\config.toml
# ...or see exactly what it would send, without sending
python -m jobscout notify-test --config data\ops\config.toml --dry-run

# What is enabled, what is switched off, and whether each source was ever validated
python -m jobscout sources list --config data\ops\config.toml
```

Recovery:

- **Nothing is arriving.** Run `jobscout notify-test` first: it separates "Telegram is broken"
  from "there is nothing to send". Then check `Last Result` and the log tail, then `status`. A
  stale `Last Run` means the task is disabled or the laptop was asleep; `schtasks /Run` proves
  the wrapper still works. If the log shows `Telegram is not configured`, the `.env` file was
  moved or the token is invalid.
- **A source shows a growing failure count.** `status` names the error. Three consecutive
  failures send one Telegram notice; a completed scan clears it.
- **One message naming several sources.** That is the shared-outage notice (D-086): several
  sources failed because the connection never opened, which is almost always this machine's
  network rather than several employers breaking at once. Further sources caught by the same
  outage are recorded silently for two hours. Individual health is still per source in `status`.
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
- `prune` stays a manual command. The horizons are ratified (D-067), but a scheduled prune buys nothing: the command is bounded, idempotent and safe to run whenever you want the space back.
- A second person running their own deployment should follow [pilot.md](./pilot.md) rather than this section: their task, config, database and source choices are their own.
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

## 7b. Adding an employer (the expansion procedure)

Adding an employer is a catalogue file plus one bounded check. It was executed end to end for
Barclays, Wise and Arm on 2026-10-24, which is how the two wrong URL patterns and the need for a
Workday search were found — expect the same.

1. **Find the feed.** Decide which shape it is: Lever (`site`), Greenhouse (`board`), Ashby
   (`board`), Workable (`account`), Workday (`host`/`tenant`/`site`), or a sitemap of job URLs whose
   pages carry schema.org `JobPosting` JSON-LD. The per-adapter keys are in [configuration.md](./configuration.md).
   A site needing another shape needs an adapter, and that is development, not configuration.
   For the one-request shapes (Greenhouse, Ashby, Lever, Workable) you do not have to guess: put the
   candidate on a line of `adapter,slug,Employer` and run
   `python tools/probe_boards.py --input candidates.txt`. It reports roles, UK roles, roles in a
   target family and early-career signals for every candidate, in one request each — that is how
   expansion batch 2 chose its 43 employers (D-100).
   **Better: start from an employer, not a slug.** Guessing board tokens had fallen to a 4.5 % hit
   rate by batch 3. Batch 4-6 instead started from a public weekly list of UK graduate, internship,
   placement and apprenticeship roles whose rows name the employer, then fetched one published job
   page per employer to read the board URL it actually uses (the page carries it). That found 158
   boards across 214 employers in about four minutes and turned the guess into a measured
   inventory. Any similar list that links to employers' own postings works the same way.
2. **Write one catalogue file** in your catalogue directory. It must carry an `access_note`
   naming the public endpoint it reads and the hygiene applied (D-087) — that is the record AC-19
   asks for, and the loader rejects a file without one. For a sitemap site, open its sitemap
   and copy one real job URL: `job_url_pattern` must match it and contain `(?P<id>...)`. Do not
   guess the shape from a synthetic fixture — that is what went wrong twice.
3. **Check it loads.** Both commands are offline and read-only:

   ```powershell
   python -m jobscout config validate --config data\ops\config.toml
   python -m jobscout sources list --config data\ops\config.toml
   ```

4. **Validate it live, bounded.** Never scan an unvalidated source.

   ```powershell
   # Any source with a known adapter; a JSON source without a recorded fixture needs --live-only.
   python -m jobscout validate --source <id> --confirm-live --live-only
   ```

   Read the result, not just `PASS`. `Records: live=0` with "none of N sitemap URL(s) matched"
   means the URL pattern is wrong; a `WARN` about a new field means the adapter may need updating.
   The result is recorded: `sources list` then shows `validated 10-24 09:48 ok`.
5. **Take the first scan by hand**, with the task disabled, so you can watch it:

   ```powershell
   schtasks /Change /TN "Job-Scout scan" /DISABLE
   python -m jobscout scan --source <id> --dry-run --config data\ops\config.toml
   ```

   The first scan of a large board is `partial` by design when it hits `max_detail_fetches`
   (400). Run it again: the second pass fetches the remainder and **establishes the baseline
   silently** — no alerts for existing roles.
6. **Enable and confirm.**

   ```powershell
   schtasks /Change /TN "Job-Scout scan" /ENABLE
   python -m jobscout status --brief --config data\ops\config.toml
   python -m jobscout summary --config data\ops\config.toml --current
   ```

7. **Expect the scope to hurt.** A new employer may contribute nothing: under the pinned London
   scope, Arm's 433 roles yield zero matches. That is the preference working. Widen `[preferences]`
   deliberately if you want them, and note the extra volume.

Politeness: keep `request_delay_seconds` at the default or higher for a board with hundreds of
pages, and use `[pacing.hosts]` when several sources share a host. Arm was raised to 1.0s after ten
TLS handshakes timed out at the 0.5s default.

## 7c. Expanding in batches, towards ~100 employers

The machinery is ready; the list is operational work (D-066). Work in batches, because a batch that
adds nothing measurable is a signal worth acting on before the next one.

**Per batch (10–15 employers)**

1. **Pick for one shape.** A batch of Greenhouse boards is far quicker than a mixed batch, because
   the options are the same and only `board` changes.
2. **Write the files.** One per employer, `id` = a short stable slug (`acme_graduate`), `employer` =
   what you want to see in an alert. Copy the nearest existing catalogue file.
3. **Load-check the batch together** — `config validate` names any file with a bad option, so fix
   them all in one pass.
4. **Validate each, live.** `validate --source <id> --confirm-live --live-only` per source. Expect
   the first batch to teach you something: a URL shape, a `search_text` you need for a huge board,
   or a WARN about a field the adapter does not model.
5. **Baseline by hand** only for large sitemap boards (the 400-page cap needs two passes). Everything
   else can be left to the schedule: the first complete scan is silent.
6. **Let it run for a day**, then read the report.

**Per batch review**

```powershell
python -m jobscout report --days 1 --config data\ops\config.toml
```

- **A source with roles but no matches** is coverage without value under the current scope: either
  the scope is wrong for that employer (arm under a London-only pin) or the employer does not hire
  early-career in the target families. Keep it if you expect that to change; otherwise drop it.
- **A source with no roles at all** is a feed problem, not a scope problem: the URL pattern, the
  search text, or a board that has moved. Check `sources list` for the last scan status.
- **A source that keeps failing** is a maintenance cost. Two consecutive days of failures means the
  feed changed; `validate` will say how.

### Completed batches

**Batch 1 — 2026-10-24 — 11 Greenhouse employers.** All validated live (`--live-only`), complete,
0 skipped, and baselined silently.

| Source | Board | Roles | Eligible | Review |
|---|---|---|---|---|
| stripe | `stripe` | 719 | 2 | 27 |
| anthropic | `anthropic` | 681 | 0 | 16 |
| datadog | `datadog` | 433 | 0 | 0 |
| elastic | `elastic` | 402 | 0 | 1 |
| gitlab | `gitlab` | 239 | 0 | 7 |
| graphcore | `graphcore` | 215 | 0 | 2 |
| deliveroo | `deliveroo` | 205 | 1 | 12 |
| tide | `tide` | 100 | 0 | 0 |
| gocardless | `gocardless` | 26 | 0 | 3 |
| contentful | `contentful` | 24 | 0 | 0 |
| truelayer | `truelayer` | 3 | 0 | 0 |

3 047 roles, 3 eligible matches, 68 reviews, under the pinned London scope. Candidates were found by
probing board tokens directly: many likely UK employers 404 (`checkout`, `monzo`-era fintechs,
`trainline`, `skyscanner`, `ocado`, `asos`) because they use a different ATS. Two boards were
rejected on scope grounds rather than data: `cloudflare` reports locations as `Hybrid` /
`Distributed` with no city, and `brandwatch` has no UK locations at all. Boards are not UK-only, so
their `id` is the board token and no `uk_employer` flag is set.

Two sources failed their first baseline attempt and succeeded on retry: `anthropic` hit the 20-second
HTTP timeout on its 681-role payload, and `stripe` hit a local DNS failure (`getaddrinfo failed`).
Both were transient, not feed changes — worth remembering before diagnosing a single failure as a
broken source.

**Batch 2 — 2026-10-25 — 43 employers, selected by measurement (D-100).**

664 candidate boards were probed across Greenhouse, Ashby and Lever; 61 answered with at least one
UK role, and 43 of those also had at least one UK role in a target role family. Candidates whose UK
roles are all sales, marketing or support were rejected regardless of size, which is why `harvey`
(29 UK roles), `langchain` and `modal` are absent.

New machinery: an **`ashby` adapter** (public posting API, one request per board, plain-text
descriptions so early-career signals survive), an `ashby` profile in `validate`, and
[`tools/probe_boards.py`](../tools/probe_boards.py), which probes a candidate list and reports
roles, UK roles, family roles and early-career signals. All 43 sources were validated live
(`--live-only`) before being added.

| Measure | Value |
|---|---|
| Catalogue | 17 → **60 employers** |
| Enabled on the hosted deployment | 12 → **59** (`barclays` the only exclusion) |
| Sources scanned in the measurement pass | 59 — **57 success, 2 partial, 0 failed** |
| Roles stored | **10 276** (1 773 in the UK) |
| Matches / review under the default rules | **62 / 401** |
| Sources contributing at least one match | **15** |

Largest contributors (stored / UK / matches): `databricks` 896/49/1, `capco` 726/78/0, `stripe`
719/37/2, `anthropic` 681/50/0, `sumup` 349/76/0, `palantir` 309/45/11, `gitlab` 239/51/0,
`janestreet` 220/58/0, `celonis` 217/19/2, `sierra` 210/37/2, `wayve` 178/63/0, `elevenlabs`
159/55/0, `figma` 138/18/1, `perplexity` 132/24/1, `cohere` 116/37/2.

The two partial scans are the expected cold crawls (`wise` 428 roles capped at 400 detail fetches,
`arm` 434 pages capped at 400). Both complete on their next pass by skipping unchanged pages, and a
partial scan never alerts; `arm` still contributed one match on its partial pass.

**Batch 3 — 2026-10-25 — 17 employers, and a measured decline in the easy supply (D-100).**

Two further probe rounds (381 candidates across Greenhouse, Ashby, Lever and the new Workable
adapter) added 17 employers that met the same bar: a complete board, at least one UK role, and at
least one UK role in a target family. Added: `abnormalsecurity, braze, circleci, cognism, griffin,
jfrog, klaviyo, neo4j, onetrust, pagerduty, sardine, smartsheet, socure, vanta, verkada, wordsmith,
zoopla` (the last one via Workable). All 17 were validated live before being enabled.

| Measure | Value |
|---|---|
| Catalogue | 60 → **77 employers** |
| Enabled on the hosted deployment | 59 → **76** (`barclays` the only exclusion) |
| Deployment baseline after the batch | **76 of 76 sources scanned successfully** |
| Roles stored in the hosted database | **11 885** (1 958 in the UK) |
| Matches / review under the default rules | **62 / 451** |
| Probe hit rate | **4.5 %** of candidates (17 of 381), against 6.5 % in batch 2 |

The falling hit rate is the finding: the UK employers that publish on Greenhouse, Ashby, Lever or
Workable and hire in our families have largely been found. Batch 4 should stop sweeping the same
adapter surface and go after the platform we can already read but have barely used — **Workday**,
which is where banks, insurers and consultancies publish graduate programmes — plus Recruitee,
Teamtailor and SmartRecruiters for UK mid-market employers. Each needs per-employer discovery
(host, tenant and site for Workday) rather than a one-request probe, which is why it is a separate
batch rather than more of the same.

**Batch 4-6 — 2026-10-25 — 34 employers found from the platform behind known UK early-career
employers, plus two adapter defects and a location-coverage gap fixed on the way.**

Batch 3's conclusion was that probing guessed board tokens had run out of supply (4.5 % hit rate).
This batch changed the direction of the search: instead of guessing a slug and asking whether it
happens to have UK early-career roles, it started from employers that demonstrably do publish them.
A public, weekly-rebuilt list of UK graduate, internship, placement and apprenticeship roles — 617
roles across 214 employers, every row linked to the employer's own posting — was reduced to one
published job page per employer. That page carries the board URL the employer uses, so one request
per employer identified the platform: **78 Workday, 42 Workable, 31 Greenhouse, 6 Recruitee, 1
Rippling, 56 unidentifiable**. The probeable boards then went through the usual
`tools/probe_boards.py` gate, and the Workday graduate sites through `validate --live-only`.

| Measure | Value |
|---|---|
| Catalogue | 77 → **111 employers** |
| Enabled on the hosted deployment | 76 → **110** (`barclays` the only exclusion) |
| Added | **34**: 10 Workable, 18 Greenhouse, 3 Lever, 3 Workday |
| Roles on the new boards | **2 174** (937 in the UK) |
| Matches / review under the default rules | **≈68 / ≈73** |
| First scan complete | 34 of 34, after the two fixes below |

Largest contributors (roles / UK / matches): `cfo_insights` 363/250/3, `affirm` 225/17/1,
`retail_insights` 250/236/3, `winthrop` 239/33/1, `scale_ai` 171/26/2, `drw` 164/44/3,
`tower_research` 100/7/3, `charles_river` 91/11/3, `mbda` 73/73/13, `focusrite` 20/16/4,
`grayce` 13/13/6.

**Three defects the batch surfaced, all fixed and tested:**

1. **Workable publishes a multi-location post once per location under one `shortcode`.** The adapter
   reported those rows as duplicates, which made the scan `partial` — and a partial scan never
   baselines, never alerts and never becomes digest-eligible. `grayce`, `winthrop` and `moneyfarm`
   would have been silently dead sources. Rows are now merged into one job with the locations
   joined; `grayce` scans 33 records as 13 roles, complete.
2. **The Workday detail-lookup cap was 10.** Placeholder locations ("2 Locations") need one detail
   GET each, so `mbda` — 73 multi-site graduate postings — was permanently partial. The cap is now
   200, the same one-request-per-role cost the listing already pays. `mbda` scans complete and
   contributes 13 matching UK graduate software and cyber roles in Bristol and Stevenage.
3. **The UK town list was too short.** `Stevenage`, `Bolton`, `Solihull`, `Rotherham` and 61 other
   towns with large graduate employers were "location_unresolved", so MBDA's Stevenage roles were
   excluded while its Bristol ones matched. 65 towns were added; names that exist abroad
   (`Washington`, `Perth`, and the usual Cambridge/Oxford/Manchester collisions) stay ambiguous and
   still need a country token or a `uk_employer` board.

**Considered and rejected, with the reason recorded so it is not re-researched:**

- `rolls_royce` — the `intern_graduate` site is global (Singapore, Oberursel, Dahlewitz): 94
  postings, 7 in the UK, 0 matches, and 94 detail requests per scan mostly for non-UK roles. Revisit
  if a UK-only site appears.
- `andurilindustries` — 2 511 postings for one UK early-career match; disproportionate scan cost.
- `canonical`, `hr_wallingford` — 314 and 4 postings, 2 and 1 in a target family, none with an
  early-career signal. Coverage without value today.
- `thirdbridge` — 35 UK early-career roles, but only 1 in a target family; its titles are research
  "Associate" roles.
- `ctccampusboard`, `mwinternshipprogram` — campus/aggregator boards, not employers.
- **Recruitee (6 employers)** — no adapter; the largest identified platform we cannot read yet.
- **Workday backlog: 75 of the 78 identified tenants are not enabled.** The CXS endpoint is
  undocumented and per-tenant, and D-094's hosted posture is what got `barclays` excluded. The
  tenant/site inventory is the input to a separate decision, not a batch to force through.

**Deploying this batch.** The hosted deployment picks the new employers up on the next
`install.sh`, because the catalogue ships with the package. Existing subscribers keep the employer
set they chose until they open `/settings` — a wider catalogue never widens somebody's subscription
silently.

**What the batch proved, and what it did not.** It proved the candidate → validate → enable → baseline
path works at scale for one adapter shape, and that `report` distinguishes stored roles from value
delivered. It did not add much value: 3 047 roles produced 3 alerts-eligible matches, because the
pinned London scope and the `seniority_unclear` review gate reject almost everything on these large
tech boards. That ratio is the finding to act on before adding batch 2.

**Batch 1 triage, same day.** `tide`, `contentful` and `truelayer` were switched off with
`disable = ["contentful", "tide", "truelayer"]` in the operator's config: 129 roles between them,
0 matching and 0 in review. Their catalogue files and all stored history stay, so re-enabling one is
deleting an id from that list. `graphcore`, `gitlab`, `elastic`, `anthropic` and `datadog` were kept
for another observation period despite zero matches; `stripe` (2 matches) and `deliveroo` (1 match)
earned their place outright.

**Lesson for batch 2.** A live public board is not evidence of relevant early-career supply. All
eleven employers were broad technology boards whose UK presence is small and mostly senior or
go-to-market, so board size told us nothing about the roles worth alerting. Batch 2 should start
from evidence of London-based graduate or intern volume under the operator's actual preferences,
then confirm a supported adapter — not the other way round.

**Narrowing without deleting**: `disable = ["id", ...]` in the config turns a source off while
keeping its catalogue file and its history. Use it for anything seasonal; delete the file only when
you are sure.

**Keep the numbers honest.** `report` counts roles stored and matches, not applications. A hundred
employers that produce twenty matches is fine; a hundred that produce two means the scope needs
looking at before the list grows further.

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

## 9. Running the hosted service (D-091…D-096)

`jobscout bot` serves invited users. It shares the adapters, matching, pacing and notifier with the
CLI; what is new is Telegram inbound, the per-user layer, and continuous operation.

### 9.1 Use its own database

```bash
python -m jobscout bot --config config.toml --db data/service.db
```

**Run the service against a separate database file from the personal beta.** Both modes write
`notifications`, and both would address the operator's own chat, so sharing one file means the
personal schedule and the service can each alert about the same role. One file per deployment keeps
the two independent, and the CLI keeps working against `data/jobscout.db` exactly as before.

The service scans only the sources the config enables, so **the offered employer set is the
operator's choice** (D-094): narrow it with `[jobscout] disable` before inviting anyone.

```bash
python -m jobscout bot --once --config config.toml --db data/service.db   # one poll, then exit
```

**The pilot deployment** (2026-10-25) uses `data/ops/service-config.toml` and
`data/service/service.db`, and offers **76 employers** — the whole catalogue minus `barclays`
(D-100). The catalogue files and the personal beta are untouched: this is a per-deployment list.
`barclays` stays out because it is an undocumented Workday endpoint with documented rate-limit
risk; `arm` and `wise` stay in, because their cold crawls exceed the page budget once and complete
on the next pass, and a partial scan never alerts.

`jobscout service status --config data/ops/service-config.toml` prints the subscriber count, alert
totals, when the bot last handled an update, and any source whose last scan did not succeed. It is
read-only and is the first thing to run when a tester reports silence.

### 9.2 As a long-running process

Linux, `/etc/systemd/system/jobscout-bot.service`:

```ini
[Unit]
Description=Job-Scout Telegram service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=jobscout
WorkingDirectory=/srv/jobscout
EnvironmentFile=/srv/jobscout/.env
ExecStart=/srv/jobscout/.venv/bin/python -m jobscout bot --config config.toml --db data/service.db
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now jobscout-bot
journalctl -u jobscout-bot -f          # the bot logs one line per notable event
```

Windows: the same idea as section 3 — a `run-bot.cmd` wrapper running `jobscout bot`, registered
with `New-ScheduledTaskAction` and restarted by the task's restart-on-failure setting.

**What the pilot deployment actually uses** (2026-10-25, Windows):

| Piece | Value |
|---|---|
| Task | `Job-Scout bot`: logon trigger for one user **plus a repeating trigger every 2 minutes**, `MultipleInstances=IgnoreNew`, restart every 1 minute up to 999 times, no execution time limit |
| Action | `data\ops\run-bot.cmd` → `python.exe -u -m jobscout bot --config data\ops\service-config.toml` |
| Log | `data\ops\logs\service.log` (the wrapper redirects both streams) |

```powershell
$action  = New-ScheduledTaskAction -Execute "D:\Mini Project\Job-Scout\data\ops\run-bot.cmd" `
             -WorkingDirectory "D:\Mini Project\Job-Scout"
$logon   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$keepalive = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(-5) `
             -RepetitionInterval (New-TimeSpan -Minutes 2) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 999 `
             -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
             -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "Job-Scout bot" -Action $action -Trigger @($logon, $keepalive) `
  -Settings $settings -Description "Job-Scout Telegram service: one long-running bot process"
```

**The repeating trigger is what actually keeps it alive.** Restart-on-failure alone did not: killing
the bot process by hand left the task `Ready` with no process and nothing restarted after 90
seconds, and a `Repetition` attached to the logon trigger produced no `NextRunTime` at all. With a
separate time-based trigger repeating every two minutes and `IgnoreNew`, the bot came back 59
seconds after being killed (killed 01:15:5x, restarted 01:16:05), and a live bot is never duplicated
— the scheduler reports `0x80070420` ("already running") and does nothing. `IgnoreNew` also means a
slow crawl in progress is never disturbed. The repetition only needs the *task* re-registered once;
it survives reboots.

`-u` is not optional in the wrapper: without it Python buffers stdout when it is not a console, and
a crash leaves an empty log — which is exactly the state a restart needs to explain.

```powershell
Get-ScheduledTask -TaskName "Job-Scout bot"                      # State: Running / Ready
Stop-ScheduledTask  -TaskName "Job-Scout bot"                    # stops the wrapper only
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*-m jobscout bot*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }        # stops the bot itself
Start-ScheduledTask -TaskName "Job-Scout bot"
```

A `Register-ScheduledTask` trigger that is not scoped to a user needs elevation ("Access is
denied"), so the task is registered `-AtLogOn -User <name>` and therefore also starts when that
user signs in. **This deployment is on the operator's own machine, not a cloud host** (D-097): it
proves the Telegram journey and the operating procedure, not uptime. If the machine is off or
signed out, nothing is scanned and no one is messaged.

One process is enough at pilot scale: polling is long-poll based (no inbound port, no certificate),
and a scan happens only when a source is due. **Scans block polling while they run**, which is worth
knowing before it looks like a bug:

| Pass | Measured 2026-10-25 |
|---|---|
| Eleven of the twelve original sources | 23 seconds (stripe 719 roles, palantir 309, gitlab 239, graphcore 215) |
| Wise, first ever pass | **partial**, capped at 400 detail fetches of 428 needed, ~4 minutes |
| Wise, next pass | **success** — the 400 already-fetched pages are skipped by revision, the remaining 28 fetched, 28 new roles stored |
| All 59 offered employers, cold | ~10 minutes: 57 complete, 2 partial (`wise`, `arm`), 0 failed; 10 276 roles in the measurement database |
| All 59 after warm-up | dominated by the two sitemap crawls, which then fetch almost nothing because unchanged pages are skipped |

So a cold Wise crawl is slow and reports `partial` once with an explanation rather than pretending
to be complete; it completes on its own next pass and needs no intervention. A `partial` scan never
creates alerts and is not counted as a failure.



### 9.3 Backups and restore

Nightly, from cron or Task Scheduler:

```bash
sqlite3 /srv/jobscout/data/service.db ".backup '/srv/jobscout/backup/service-$(date +%F).db'"
```

Windows, where `sqlite3.exe` is often absent, uses the same SQLite backup API from Python:

```powershell
python -c "import sqlite3,datetime,pathlib; s=sqlite3.connect(r'data\service\service.db'); s.execute('PRAGMA wal_checkpoint(TRUNCATE)'); t=pathlib.Path('data/backups')/('service-'+datetime.datetime.now(datetime.UTC).strftime('%Y%m%dT%H%M%SZ')+'.db'); t.parent.mkdir(parents=True, exist_ok=True); d=sqlite3.connect(t); s.backup(d); d.close(); s.close(); print(t)"
```

Copy that file off the machine. Restore is: stop the service, delete the old `service.db` **and its
`-wal`/`-shm` siblings**, put the backup in place as `service.db`, start the service.

**Drill completed 2026-10-25**, on the pilot database:

| Step | Result |
|---|---|
| Backup taken while the bot was polling | `data/backups/service-20261025T000700Z.db`, 24.7 MB, `PRAGMA integrity_check` = ok |
| Contents of the backup | 3 334 jobs, 11 scans, 12 sources, 11 baselines, 0 users, 0 notifications |
| Bot stopped, live database moved aside, backup copied in, bot started | Task reached `Running`; log shows `Database:`, `Offered employers: 12`, `Polling Telegram…` |
| After restore | `jobscout service status` reports the offered employers and every source scanned successfully; baselines survived, so a restore cannot re-announce history |
| Personal beta | untouched — still 4 420 jobs / 328 scans in `data/jobscout.db`, separate file, separate task |

A restored database keeps each source's `baseline_scan_id`, which is why restore does not produce an
alert storm. Deleting the `-wal`/`-shm` siblings matters: a stale WAL next to a replaced main file is
not a database you want to trust.


### 9.4 Monitoring

- One external heartbeat (a free service pinging a URL, or a "did the bot log today" check).
- `jobscout report --days 7 --db data/service.db` for scans and delivery outcomes.
- `jobscout service status --config …` for subscribers, alert totals, the time of the last update
  handled, and every source whose last scan did not succeed. "No update handled yet" is normal
  before the first tester presses Start; it is not evidence of a fault.
- `Get-Content data\ops\logs\service.log -Tail 40` — one line per notable event, plus the startup
  banner. A crash is visible here because the wrapper runs Python unbuffered.
- A source that fails three times still sends the operator a failure notice, and a shared
  connectivity outage still produces one aggregated notice (D-086).


### 9.5 User operations

| Task | How |
|---|---|
| See who is subscribed | `sqlite3 data/service.db "select u.id, u.chat_id, u.paused, count(s.source_id) from service_users u left join service_subscriptions s on s.user_id=u.id group by u.id"` |
| See what a user was told | `store.list_alerts(user_id)` in Python, or query `notifications` by `chat_id` |
| Remove a user on request | Ask them to send `/delete`; if they cannot, delete the row and their rows in `service_preferences`, `service_subscriptions`, `service_sessions` and `notifications` |
| Change offered employers | edit `[jobscout] disable` in the service config and restart |
| Check what a user's `/digest` would show | `digest_entries(storage, store, user)` — read-only, and the same function the bot calls |

**`/digest` is a browser, not an alert.** It pages through the roles that are already open and
match the user's own saved employers and preferences, five per page, editing one message as they
navigate. It never queues a notification, moves a watermark or touches alerting state (D-095), and
it only counts roles a complete scan has confirmed (`first_complete_scan_id`), so a role seen by a
partial scan cannot appear. The stated total is the real total: every eligible role in the user's
scope is evaluated, with no candidate cap.

Cost, measured on the 2026-10-25 pilot database: 3 363 open roles across one user's 12 employers
take about 12 seconds to evaluate the first time (≈3.7 ms per role, the same deterministic matcher
the alert path uses). The result is cached per user until their scope or the newest job id changes,
so page flips are instant. A user who selects all 76 employers would see roughly 40 seconds on the
first `/digest` of each scan cycle. If the pilot grows past a handful of testers, the fix is a
persisted per-job decision cache keyed by the rules fingerprint, not a cap.

**`/settings` restores what was saved.** Reopening it rebuilds the wizard from the stored
preferences and the stored subscription set, so the buttons show the user's real answers. A saved
employer set that matches no current pack, or a saved location that matches no preset, is offered
as "keep what I chose" rather than being replaced by the nearest default. Saving without changing
anything keeps the exact employer set, keeps the watermark, and keeps preference keys the wizard
does not show.

### 9.6 What the service does not do

No web UI, no Mini App, no password reset (there is no password), no per-user custom employers, no
quiet hours, no saved jobs or application tracking. `/digest` browses existing matches; it is not a
scheduled summary of new alerts. Users are invited by the operator; there is no public signup,
because a public signup would turn the source-access question into a free-for-all (D-094).

### 9.7 The invited pilot (2026-10-25)

What is deployed: one bot process on the operator's machine, its own database, 76 offered
employers, invitations sent by hand. [deployment.md](./deployment.md) covers moving the same
image to an always-on host. Testers need nothing installed; they open
`@AwesomeJobScoutBot` and press Start. The tester-facing briefing is [pilot.md](./pilot.md).

**Operator checklist before inviting**

1. `Get-ScheduledTask -TaskName "Job-Scout bot"` reports `Running`, and the log's last lines are the
   startup banner.
2. `jobscout service status --config data\ops\service-config.toml` shows 76 offered employers and
   every source healthy in `Sources:`. A source listed under `Sources:` is either genuinely
   failing or has never been scanned; wait for its next due run before concluding.
3. Confirm the personal beta is unaffected: its own task is running, and
   `data\jobscout.db` still has its 4 420 jobs and 328 scans.
4. Take a fresh backup (9.3) and note where it is.
5. Send the invitation text below, one or two testers at a time, so a breakage is attributable.

**Invitation text**

> I have a UK early-career job bot in Telegram that watches employers' own job boards and messages
> you when a matching role appears. It is a pilot, not a product — I want to know whether it is
> understandable and useful, and what breaks.
>
> You need nothing installed:
> 1. Open `@AwesomeJobScoutBot` and press **Start** (`/start`).
> 2. Answer five short screens with buttons: kinds of role, early-career types, location, employers,
>    confirm. About a minute; `/settings` changes any of it later.
> 3. That is it. It runs in the background and messages you when a genuinely new matching role
>    appears.
>
> Please expect quiet at first: you are only told about roles posted *after* you set up — `/digest`
> lists roles that already match. `/status` tells you whether monitoring is working, `/pause` stops
> alerts, and `/delete` erases your data and stops everything.
>
> What I would like back, in any form: where you hesitated or guessed, anything that arrived and
> looked wrong or irrelevant, anything you expected and did not get, and whether you would keep it
> on. If something looks broken, sending me the `/status` output and roughly when it happened is
> plenty.
>
> Which employers to follow is your choice — the bot only watches the ones you pick.

**What was verified before inviting** (2026-10-25, against this database and the real Bot API)

- The five-step wizard, `/status`, `/digest`, `/help`, `/pause`, `/resume` and `/delete` each ran and
  the saved scope was read back from the database (families, employment types, London plus UK
  remote, all offered employers, fingerprint and watermark stored).
- One newly appearing matching role was delivered **exactly once**: the notify pass queued 1, the
  transport recorded 1 `sent` with a message id, nothing was left pending, and an immediate second
  pass delivered 0. The synthetic source, roles, scans and subscriber were then deleted.
- `/digest` listed real early-career London roles already open on the offered boards, so scanning
  and matching work on live data.
- All 12 offered sources reached a complete scan (`Sources: 12 of 12 scanned successfully last
  time`), and the 3 362 stored roles produced no new-role alerts because nothing new had appeared
  since the baseline — which is the correct quiet result, not a fault.

**What is still unproven, and must be said plainly**

- **A human pressing a button.** The wizard was driven by synthetic updates, so the inbound
  poll-and-acknowledge round trip has not been exercised by a real finger. It is the first thing a
  tester does, and the log will show it (`/status` will then report a last-update time).
- **A genuine newly discovered role reaching a person.** The delivery path is proven with a
  synthetic role; only a real new posting proves the whole chain.
- **Uptime.** This is one machine, not a cloud host (D-097). A dead bot process is restarted within
  two minutes (verified by killing it), but the machine sleeping, signing out or losing its
  connection is not covered by anything.
- **Relevance.** Nothing has been judged by a second human yet.
- **AC-20**, the seven-day reliability target, remains outstanding and is measured separately on the
  personal beta.

