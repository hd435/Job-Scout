# Job-Scout

A personal UK early-career job monitor. It reads **official employer and ATS job boards**, records every role it observes, and alerts on Telegram when a **new** role matches the target role families and locations.

**Current state:** v0.5 (configurable scope), v0.6 (scale-out machinery) and v0.7 (hosted service) are delivered. The hosted service is deployed and its employer coverage has grown from 12 to 76 enabled in three measured batches (see [docs/runbook.md](./docs/runbook.md) §7c). The **coverage target is still short** — the intended ~100 enabled employers has not been reached — and the seven-day reliability measurement (AC-20) is still open, so the MVP is not complete.

Deployment: the approved target is **Oracle Cloud Always Free**, which costs nothing to keep running (D-101). [docs/deployment.md](./docs/deployment.md) covers provisioning, the one-command installer in `deploy/oracle/`, health checks, backups and the restore drill; the Windows scheduled task remains the fallback until the VM is running.

Two ways to use it:

- **Self-hosted CLI** — clone, configure, run a scheduled task. This document is that path.
- **Hosted Telegram bot** — an invited user presses Start and configures everything in chat, with nothing to install. See [docs/runbook.md](./docs/runbook.md) §9 for the operator side and [docs/pilot.md](./docs/pilot.md) for the invited-user side.

Product requirements: [docs/prd.md](./docs/prd.md). Technical design: [docs/architecture.md](./docs/architecture.md). Decisions: [docs/decisions.md](./docs/decisions.md). Delivery plan and history: [docs/roadmap.md](./docs/roadmap.md). Documentation map: [docs/README.md](./docs/README.md). Source research: [docs/sources.md](./docs/sources.md). Access terms per source: [docs/source-access.md](./docs/source-access.md).

## Requirements

- Python 3.12 or newer. No runtime dependencies beyond the standard library.
- A clone of this repository. Install it **editable** (`pip install -e .`) — the validation fixtures live under `tests/fixtures/` and are not packaged, so an installed copy cannot run `validate --fixture`.

## The hosted bot (no installation)

If someone has invited you to a Job-Scout bot, you do not need any of this repository:

1. Open the bot in Telegram and press **Start** (or send `/start`).
2. Answer five button questions: role families, early-career types, location, employers, confirm.
3. That's it. You are told about matching roles that appear **after** setup; `/digest` shows what
   already matches, `/status` says whether monitoring is working, `/pause` stops alerts, and
   `/delete` removes your data.

Running the service is an operator job: [docs/runbook.md](./docs/runbook.md) §9 covers the process,
its own database, backups and user administration.

## Set up your own deployment (about 15 minutes)

This is the whole path. Each step says what you should see before moving on.

**1. Install.**

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

python -m pip install -e ".[dev]"
```

**2. Create your own Telegram bot** and note the token and your chat id.

1. In Telegram, message `@BotFather`, send `/newbot`, choose a name and a username ending in
   `bot`. Copy the token it gives you (looks like `123456789:AA...`).
2. Send your new bot any message, then open
   `https://api.telegram.org/bot<your-token>/getUpdates` in a browser and read
   `"chat": {"id": ...}`. Group ids are negative.

**3. Put the credentials in `.env`** (gitignored; `.env.example` is the template).

```bash
cp .env.example .env    # Windows PowerShell: Copy-Item .env.example .env
```

**4. Prove delivery works before anything else.**

```bash
python -m jobscout notify-test
```

Expect `Delivery check: sent` and one labelled message in Telegram. If it fails, the error names
the cause and nothing else has been touched — fix this before going further.

**5. Choose your scope**, then check it.

```bash
cp config.example.toml config.toml   # then edit: sources, families, locations
python -m jobscout config validate --config config.toml
python -m jobscout config show     --config config.toml
```

`config.example.toml` explains the five choices and their valid values. **Which employers you
scan is your decision** — the example starts with six and leaves the rest off.

**6. Prove the whole alert journey** before waiting for a real role. This runs detection,
matching, queueing, formatting, delivery and recording against synthetic records in a throwaway
database, using your own credentials and preferences:

```bash
python -m jobscout rehearsal --config config.toml
```

Expect one clearly synthetic alert in Telegram and `Result: PASS`. Add `--dry-run` to see the
message without sending it.

**7. Establish the baseline.** The first complete scan of each source records what is already
open, silently:

```bash
python -m jobscout scan --source <id> --dry-run --config config.toml
```

Repeat per source (`jobscout sources list` shows the ids you enabled), or take them all at once
with a manual scheduler pass:

```bash
python -m jobscout schedule --once --config config.toml
```

**8. See what is already out there.** Alerts only start with the *next* genuinely new role, so
this is how the first session shows value:

```bash
python -m jobscout summary --current --config config.toml
python -m jobscout digest --dry-run --config config.toml
python -m jobscout digest --config config.toml
```

**9. Run it on a schedule.** See [docs/pilot.md](./docs/pilot.md) for a five-minute task on
Windows, macOS and Linux, plus how to check on it and what to report if something looks wrong.

## Usage

```bash
# Recorded snapshot, no network access (recommended for a first look)
python -m jobscout scan --source palantir --dry-run --fixture tests/fixtures/palantir_jobs.json

# Recorded Workday snapshot (synthetic fixture: proves parsing, not live shapes)
python -m jobscout scan --source lloyds_graduate --dry-run --fixture tests/fixtures/workday_lloyds_graduate.json

# Recorded Greenhouse snapshot (synthetic fixture: proves parsing, not live shapes)
python -m jobscout scan --source monzo --dry-run --fixture tests/fixtures/greenhouse_monzo.json

# Live scan of one configured source (contacts that employer's public board)
python -m jobscout scan --source palantir --dry-run

# Scan and deliver alerts for new matching roles via Telegram
python -m jobscout scan --source palantir --deliver

# Retry alerts that are still queued (no scan, no source request)
python -m jobscout deliver

# Run the sources that are due, then exit (no daemon, nothing in the background)
python -m jobscout schedule --once

# Run the scheduler loop in the foreground (a scheduled task normally does this for you)
python -m jobscout schedule --max-iterations 1

# Inspect what retention would remove, then remove it (never runs automatically)
python -m jobscout prune --dry-run
python -m jobscout prune

# Read-only views of stored state
python -m jobscout summary --current
python -m jobscout status
python -m jobscout report --days 7

# Send the roles that already match, once (does not queue alerts or touch the database)
python -m jobscout digest --dry-run
python -m jobscout digest

# Prove the Telegram delivery path with one labelled message (no job, no alert state)
python -m jobscout notify-test --dry-run
python -m jobscout notify-test

# Prove the whole journey - detect, match, queue, deliver, record - with synthetic records
python -m jobscout rehearsal --dry-run
python -m jobscout rehearsal
```

## First usable session

New roles only alert from the moment they are first seen, so a fresh install would stay
silent until an employer posts something. `jobscout digest` closes that gap: it reads the
roles that already match and sends them once. It writes nothing, queues no notifications, and
does not disturb baselining or deduplication — so the same role can never be alerted twice.

```bash
python -m jobscout digest --dry-run   # preview the messages
python -m jobscout digest             # send them to the configured chat
```

`jobscout notify-test` answers a different question — "can this machine reach my chat at all?"
— with one message that says it is a delivery check rather than a role. It records the attempt
in a `delivery_checks` table, never in the alert history, and it changes no job state.

`jobscout rehearsal` goes one level further: it runs a first scan (silent baseline), a second
scan containing one extra matching role, and delivery of the resulting alert, all against
synthetic records in a throwaway database. That is the only way to see the whole chain work
before a real employer happens to post something. What it cannot show is that a live employer
will post a **relevant** role — only a genuine new role proves that (D-089).

## Live validation (any configured source)

`jobscout validate` is a **bounded** read-only check that proves a source's live payload still
matches what its adapter expects, without touching the database or Telegram:

```bash
# Refuses to run without the acknowledgement; no client is even constructed
python -m jobscout validate

# One GET to api.lever.co, retries disabled, sample written under data/validation/
python -m jobscout validate --confirm-live

# The same bounded check for Monzo's Greenhouse board
python -m jobscout validate --source monzo --confirm-live

# Lloyds Graduate: listing pages only, no detail lookups (Workday)
python -m jobscout validate --source lloyds_graduate --confirm-live

# Any other configured source, with a recorded payload for the shape comparison
python -m jobscout validate --source barclays --confirm-live --fixture tests/fixtures/barclays.json
```

It prints the request(s), the live-versus-fixture record counts, the adapter's parse outcome and
`PASS`/`FAIL`, then exits `0` only when the payload is shape-compatible and every record parsed
with no skips. Artifacts are capped at five records with descriptions truncated, and `data/` is
gitignored.

The request budget comes from the **adapter**, not the source id, so a source you add yourself is
covered: Lever and Greenhouse allow one request, Workday allows six (a paginated listing POST),
and the sitemap adapter allows five (two sitemap documents plus three job pages). A budget
violation is refused before the extra request reaches the network, and text requests count as
well as JSON ones. A named profile (Palantir, Monzo, Lloyds Graduate) supplies the fields to
watch and a bundled fixture; any other source is judged by its adapter's shape, and a JSON source
without a fixture needs `--fixture PATH` so there is something to compare against.

Sitemap/HTML sources have no JSON payload to compare. Their `PASS` means the board was found,
URLs matched `job_url_pattern`, and pages parsed; the report says so rather than pretending a
shape comparison happened.

What a `PASS` proves: the request(s) reached the board, the payload had the expected envelope and
field types, and every role mapped without a skip. What it does **not** prove: permission for
recurring automated access, the correctness of extracted values, matching quality on that
employer's titles, or anything about scheduling, deduplication or delivery. A Workday `PASS` will
normally show `complete=no`: detail lookups are capped so the scan is partial by design, which is
reported as a warning rather than a failure.

Common options: `--source ID`, `--db PATH`, `--config PATH`, `--fixture PATH`, `--accept-empty`.

Exit codes: `0` complete scan and clean delivery, `1` partial scan or a delivery failure, `2` failed scan or usage error.

`--dry-run` and `--deliver` are mutually exclusive, and one of them is required: a scan never sends anything unless you ask it to.

## Sources

| Source id | Employer | Adapter | State |
|---|---|---|---|
| `palantir` | Palantir Technologies | `lever` | **Live-validated and running** (309 roles) |
| `barclays` | Barclays | `workday` | **Live-validated and running** (92 roles for a `graduate` search) |
| `lloyds_graduate` | Lloyds Banking Group | `workday` | **Live-validated and running**; the `Graduate_careers` site only |
| `monzo` | Monzo | `greenhouse` | **Live-validated and running** |
| `wise` | Wise | `sitemap_html` | **Live-validated and running** (427 roles); the vacancies sitemap only |
| `arm` | Arm | `sitemap_html` | **Live-validated and running** (434 roles); never the `/search-jobs/` path |

`jobscout sources list` prints every configured source with its adapter, cadence and last scan;
`jobscout config show` prints the effective scope. `--source` is required by `scan` while more
than one source is enabled.

The two Workday sources share one adapter: the listing is a read-only POST query paginated to the
reported `total`, the requisition id is the job identity, and a detail page is fetched only when
the listing shows a placeholder location such as "2 Locations".

Wise and Arm use the sitemap/JSON-LD adapter, which is generic: `sitemap_urls`,
`job_url_pattern` (with a named `(?P<id>...)` group) and an optional `location_from = "url"`. The
sitemap supplies every role URL and its `lastmod`; a page is fetched only when a role is new or its
`lastmod` changed, so an unchanged role is recorded as observed rather than mistaken for a removed
one. Pages are parsed from schema.org `JobPosting` JSON-LD; a page without it contributes only a
title, which leaves the location unknown and the role in `needs_review` rather than guessing.
Requests are spaced by `request_delay_seconds` and capped per scan. Any careers site of that shape
can be added in configuration alone — see [docs/configuration.md](./docs/configuration.md).

Employers are **catalogue files**, one per employer, shipped in `src/jobscout/catalogue/`. Adding
one is a new TOML file, not a Python change: `[jobscout] catalogue = "sources"` points at your
own directory, and `disable = ["id", ...]` turns entries off without editing them. A
`[[sources]]` block in the config file still replaces the list entirely, which keeps a
one-source config meaning one source. Options are validated when the configuration loads: an
option the adapter does not read, or a required one that is missing, fails with a message
naming the file, the source and the key. Two enabled sources that watch the same employer, or
read the same feed, produce a warning, because deduplication is per source id.

Each catalogue file also carries `access_note` — one line naming the public endpoint it reads and
the hygiene applied (one request per scan, page caps, request spacing, a search narrowing). It is
required, because the access basis should travel with the employer rather than live in a paragraph
somewhere else (D-087); `config validate` names the sources in a `[[sources]]` block that omit it.

`jobscout sources list` prints every source with its adapter, state, match counts and whether
it has been validated (and when); `jobscout status --brief` gives one line per source and shows
an error only while it is failing. Requests are paced per host across sources — see `[pacing]`
in [docs/configuration.md](./docs/configuration.md).

A `--fixture` file feeds list-shaped sources only (Palantir JSON, Workday listing, Monzo board),
and a paginated Workday fixture stops as `partial` with a visible note instead of looping.
Sitemap-driven sources need several documents, so their offline coverage lives in
`tests/test_adapter_sitemap_html.py`.

All six researched employers were validated live on 2026-10-24 and each has completed a scan on the
operator's machine. Two researched URL shapes turned out to be wrong — Wise serves
`/job/<slug>-jid-<id>` and Arm's paths carry two trailing numbers — which is exactly what the
bounded check exists to catch. Barclays' external board is far larger than its early-career subset,
so that source sets `search_text = "graduate"` and its completeness is measured against the search's
own total. Fixtures stay synthetic: they prove parsing, pagination, discovery, failure handling and
scan safety, not that the services keep returning those shapes.

The catalogue now also carries growth beyond the researched six; `jobscout sources list` is the
authoritative count. Batch records and the per-batch expansion procedure live in
[docs/runbook.md](./docs/runbook.md) section 7c.

## Telegram credentials

Delivery needs two values: a bot token and your chat id. Put them in a local `.env` file in the project root:

```bash
cp .env.example .env          # Windows PowerShell: Copy-Item .env.example .env
```

Then edit `.env`:

```dotenv
JOBSCOUT_TELEGRAM_TOKEN=123456789:AA...
JOBSCOUT_TELEGRAM_CHAT_ID=555000111
```

- `.env` is gitignored; only `.env.example`, with placeholders, is committed. Never commit real values.
- The file is read from the **current working directory**, so run the CLI from the project root (or wherever your `.env` lives).
- Real environment variables override `.env`, so a host or CI can inject secrets without a file.
- Credentials are never read from `config.toml`.
- The token is shape-checked before use; values are never printed, and every stored or displayed delivery error has the token and chat id replaced with `[redacted]`.
- `.env` is read with a small built-in parser (`KEY=VALUE`, optional `export`, quoted values, `#` comments), so no extra dependency is needed.

The comments in [`.env.example`](./.env.example) explain where to get the token from @BotFather and how to find your chat id.

## How a scan behaves

- The first **complete** scan of a source is a **silent baseline**: existing roles are stored without individual alerts.
- A role is **new** only when first observed in a complete scan *after* that baseline.
- A role first observed only in a partial scan is not alert-eligible until a complete scan sees it.
- Any record that cannot be mapped (missing source ID, title, or URL) is skipped **with a reason** and makes the scan `partial`. There are no silent drops.
- An empty result is `success_empty`; if the source previously returned roles, an empty result is downgraded to `partial` unless `--accept-empty` is given.
- Partial and failed scans never establish a baseline, never make a role alert-eligible, and never count toward closure.
- A role closes after **two consecutive complete scans** miss it. Updates to stored fields do not re-alert.
- Matching is deterministic and explainable: `match`, `no_match`, or `needs_review` with reasons such as `family:data`, `early_career:graduate`, `location:london`, `review:seniority_unclear`.

## State

State lives in SQLite (default `data/jobscout.db`, gitignored): `sources`, `scans`, `jobs`, and `notifications`. Only normalized role fields, hashes, scan counts, delivery state, and bounded error summaries are stored; raw payloads and HTML are not.

## Notifications

Matching new roles are queued as `pending` notification rows, one per `(chat_id, job_id)`, inside the same transaction that records the scan. A `Notifier` formats and delivers them through a `Transport` and records every attempt. `TelegramTransport` sends one POST per alert to the Bot API and classifies the result:

| Outcome | Recorded as | Retried? |
|---|---|---|
| Delivered (`ok: true`) | `sent` (with Telegram message id and timestamp) | Never |
| Timeout after send | `ambiguous` | Never, by decision (D-024): the outcome is unknowable |
| 429, 5xx, or a connection that never opened | stays `pending` with attempt count and error | Yes, next `deliver`/`--deliver` pass |
| Permanent rejection (other 4xx, `ok: false`) | `failed` | No |

The HTTP client does not retry a POST internally, so the state machine is the only retry authority and a retry cannot silently double-send. (The one exception is a read-only query POST with no side effect, such as the Workday listing search, which opts in explicitly.) No `sent` row is ever sent again, which makes `deliver` idempotent.

Known limitation, accepted for the MVP (D-042): if the process dies between Telegram accepting a message and the database recording `sent`, that alert can be sent again later.

`jobscout status` reports delivery counts and the last delivery error, with the chat id and bot token redacted. No notification is sent unless you pass `--deliver` or run `deliver`.

## Scheduling and failure notices

`jobscout schedule` runs the sources that are due and records `next_run_at` in the database, so a restart resumes from persisted state rather than memory.

- Each source keeps its own `cadence_minutes` (default 30) plus optional `--jitter-seconds`, so sources do not align on the same second.
- A missed cycle is not replayed: a source that was due hours ago runs once and is rescheduled from now, so downtime never produces a burst.
- Sources are scanned one at a time and isolated: a broken adapter is reported for that source and the others still run.
- Alerts queued before a restart are delivered on the next pass — including when the scan itself fails; `sent` rows are never re-sent.
- Every turn is claimed in the database *before* the scan starts, so two scheduler processes cannot scan the same source in one cycle, and a process killed mid-scan retries at the next cadence instead of spinning.
- An exception raised outside an adapter's own error handling (a broken adapter factory, a storage error) is recorded as a failed scan, so the failure streak, the three-failure notice, and `status` still see it.
- Telegram is optional here. Without credentials the scheduler scans and reports, and says so; with credentials it also delivers alerts and sends failure notices.
- `--once` runs one pass and exits; without it the command loops until interrupted. Nothing runs in the background on its own, and `schedule` is not deployed anywhere.

**Failure notices (D-010, D-038):** a source that fails three consecutive scans produces exactly one message — employer, consecutive failure count, last error, and a pointer to `jobscout status`. The notice is claimed in the database before sending, so a long outage cannot spam you; a completed scan ends the episode, and a later outage notifies again. A partial scan is not a failure. One send attempt is made, and an ambiguous outcome is recorded rather than retried. `jobscout status` shows the next run and the failure-notice outcome per source.

**Shared connectivity outages (D-086):** when several sources fail because the connection never opened — DNS failures, a refused connection — they are one problem, so they produce one message naming the affected employers instead of one message each, and further sources caught by the same outage are recorded silently for two hours. Timeouts, HTTP errors and parser failures keep their own notice, because those can belong to one employer. Per-source health is unaffected: `status` and `report` still show every source's own state.

**Trial measurement:** `jobscout report --days 7` reads the database and prints a scorecard per source — scans against the cycles its cadence implies, complete/partial/failed counts, longest gap and missed episodes, mean scheduling delay, consecutive failures, and delivery outcomes for the window. It writes nothing and contacts nothing. Availability is inferred from scans per expected cycle; nothing records a process heartbeat, so the report says so rather than claiming the process was up. See [docs/runbook.md](docs/runbook.md) for the operating procedure and the Windows Task Scheduler preparation.


## Measuring coverage

`jobscout report --days 7` is a read-only scorecard over what is stored: scans per expected cycle
per source, gaps, delivery outcomes, and now what each source contributes — roles stored,
current matches, roles in review, and matches first seen in the window. It names the enabled
sources that contribute nothing, which is the signal to narrow the scope or drop an employer
before the next batch.

## Data retention

`jobscout prune` applies the retention policy in D-025. It never runs during a scan or from the scheduler: you run it. The horizons below are engineering defaults that the Product Owner has not yet ratified, so it is deliberately not scheduled; see [docs/runbook.md](docs/runbook.md) for the dry-run-first workflow.

| Data | Kept | Why |
|---|---|---|
| Sources, open roles, and any scan a role or source points at | Forever | Needed for deduplication, reopening, closure, and status |
| Closed role description and salary | 30 days after closure | Source-derived text is the bulkiest, least useful data; title, location, dates, URL, and the match decision stay |
| Closed role identity row (and its notifications) | 12 months after closure | Stops a reposted role re-alerting too soon; after that a repost can alert once |
| Unreferenced scan history | 90 days | Diagnostic only; referenced scans are always kept |
| Delivery error text | 90 days, then cleared | Outcome and attempt count stay for diagnosis |

`--dry-run` reports exactly what one real run would remove under the same limits and writes nothing; it also prints whether work remains (`truncated`). Runs are batched, idempotent, and safe to interrupt: a second run continues where the first stopped. Override the horizons with `--closed-content-days`, `--closed-job-days`, `--scan-days`, `--notification-error-days`.

## Configuration

Built-in defaults come from the bundled catalogue in `src/jobscout/catalogue/`, one file per
employer (`jobscout sources list` prints the current set). An optional `config.toml` can change the
database path and narrow or extend that list:

```toml
[jobscout]
database = "data/jobscout.db"

[[sources]]
id = "palantir"
employer = "Palantir Technologies"
adapter = "lever"
enabled = true
cadence_minutes = 30

[sources.options]
site = "palantir"

[[sources]]
id = "barclays"
employer = "Barclays"
adapter = "workday"
enabled = true
cadence_minutes = 30

[sources.options]
host = "barclays.wd3.myworkdayjobs.com"
tenant = "barclays"
site = "External_Career_Site_Barclays"
locale = "en-US"

[[sources]]
id = "monzo"
employer = "Monzo"
adapter = "greenhouse"
enabled = true
cadence_minutes = 30

[sources.options]
board = "monzo"

[[sources]]
id = "wise"
employer = "Wise"
adapter = "sitemap_html"
enabled = true
cadence_minutes = 30

[sources.options]
sitemap_urls = ["https://wise.jobs/sitemap.xml"]
sitemap_child_pattern = "vacanc"
job_url_pattern = '^https://wise\.jobs/job/[A-Za-z0-9\-]+?-jid-(?P<id>\d+)/?$'

# Optional, additive-only matching overrides (defaults stay unchanged if omitted)
[matching]
extra_exclude_terms = ["unpaid", "commission only"]

[matching.extra_family_terms]
data = ["bioinformatics analyst"]
```

Workday sources need `host`, `tenant`, and `site`; `locale`, `page_size`, `max_pages` and `search_text` are optional. Sitemap sources need `sitemap_urls` (or `sitemap`) and a `job_url_pattern` containing a named `(?P<id>...)` group; `sitemap_child_pattern`, `location_from`, `max_sitemap_documents`, `max_detail_fetches` and `request_delay_seconds` are optional. Every option is validated at load.

### Your job-search scope: `[preferences]`

Matching keywords live in code and are versioned (`matching.RULES_VERSION`), but *which* of
the approved rules apply is yours to choose in configuration. Omitting `[preferences]` gives the
shipped defaults, which are the pre-0.5 behaviour exactly.

```toml
[preferences]
# Which approved role families may alert. Unknown names are rejected.
role_families = ["data", "software", "quant", "finance", "consulting", "business_analysis"]

# Any recognised class the user switched off is excluded; `unknown` and
# `experienced_hire` are shown for review instead, so the classifier stays visible.
employment_types = ["graduate_scheme", "internship", "placement", "apprenticeship", "entry_level_permanent"]

# Extra title exclusions, checked before matches.
title_exclusions = []

# Optional: start from the approved seniority list and add to it, or pass [] to allow
# senior titles through.
# seniority_exclusions = ["senior", "lead", "principal"]

[preferences.locations]
# "uk" (default): any UK location. "pinned": only the cities listed below.
# "uk_plus_pinned": any UK location, plus the listed cities outside the UK.
mode = "uk"
cities = []
remote_uk = true
hybrid_uk = true
```

`[matching]` remains for *additive* terms inside an approved family
(`extra_family_terms`) and legacy `extra_exclude_terms`. The difference matters: `[matching]`
adds phrases to a reviewed category, while `[preferences]` chooses among reviewed categories
and sets the location scope. Neither can invent a category, and unknown keys or values are
rejected when the configuration loads.

Preference changes take effect on the next scan of each source, because every scan re-decides
every role it sees. `jobscout config show` prints the effective settings, the sources, and the
fingerprint of the active preference set; `jobscout config validate` loads the file and warns
about values that could never take effect (an unknown adapter, the same employer twice, a
pinned town the city list does not know).

When overrides or non-default preferences are active the rules version gains a digest (for
example `m1.1+1a2b3c4d`), which is stored with every match decision, so any decision can be
traced back to the scope that produced it.

The full key-by-key reference is [docs/configuration.md](./docs/configuration.md).

## Development

```bash
python -m pytest          # fixture-based; no network access
python -m ruff check .
python -m ruff format --check .
```

Tests use temporary databases, a fake HTTP client, and a fake Telegram transport; nothing is sent and no network call is made. An autouse fixture also keeps the suite away from your real `.env`.

## Access and compliance

Technical reachability, HTTP 200 responses, and permissive `robots.txt` rules do **not** establish permission for recurring automated access.

- Recurring polling is controlled by configuration, and this repository ships with sources enabled; running it against an employer is your own risk decision.
- The operator of this deployment has accepted that risk on a blanket personal-use basis (D-066). That is a recorded risk acceptance, not a claim that any site's terms grant permission.
- The access evidence gathered during discovery is kept in [docs/source-access.md](./docs/source-access.md). It found that Palantir and Wise prohibit automated monitoring or copying without written permission, that Barclays, Lloyds and Arm permit personal use but condition copying or storing their content on written permission, and that Monzo publishes no website-terms document at all.
- If you clone this repository, read that evidence and make your own decision. Robots rules, rate limits, and no evasion remain engineering requirements: run at a polite cadence and do not use this tool to get around bot protection.

Job-Scout never bypasses CAPTCHAs, authentication, or bot protection, and it only links to official employer or ATS listings.
