# Job-Scout

A personal UK early-career job monitor. It reads **official employer and ATS job boards**, records every role it observes, and alerts on Telegram when a **new** role matches the target role families and locations.

**Current state: M1 + M2.3 + M3 + M4 (offline) — local, all six sources configured.** Discovery, SQLite persistence, deterministic matching, silent baseline, new-role detection, update and closure handling, read-only `summary`/`status` views, a notification pipeline (queued alerts, formatting, delivery state, duplicate prevention), a **Telegram Bot API transport**, **additive-only matching-filter overrides in configuration**, and **adapters for all six intended sources** (Lever for Palantir, Workday for Barclays and Lloyds Graduate, Greenhouse for Monzo, sitemap+HTML for Wise and Arm) are implemented. Delivery is opt-in via `--deliver` and needs credentials in a local `.env` file (or in real environment variables). Scheduling and deployment are absent. Only Palantir has ever been read with recorded payloads from the live service; **the other five sources have only run against synthetic fixtures**, and no live request has been made for them.

Product requirements: [docs/prd.md](./docs/prd.md). Technical design: [docs/architecture.md](./docs/architecture.md). Decisions: [docs/decisions.md](./docs/decisions.md). Delivery plan: [docs/roadmap.md](./docs/roadmap.md). Source research: [docs/sources.md](./docs/sources.md). Access terms per source: [docs/source-access.md](./docs/source-access.md).

## Requirements

- Python 3.12 or newer. No runtime dependencies beyond the standard library.

## Install

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

python -m pip install -e ".[dev]"
```

## Usage

```bash
# Recorded snapshot, no network access (recommended for a first look)
python -m jobscout scan --source palantir --dry-run --fixture tests/fixtures/palantir_jobs.json

# Recorded Workday snapshot (synthetic fixture: proves parsing, not live shapes)
python -m jobscout scan --source lloyds_graduate --dry-run --fixture tests/fixtures/workday_lloyds_graduate.json

# Recorded Greenhouse snapshot (synthetic fixture: proves parsing, not live shapes)
python -m jobscout scan --source monzo --dry-run --fixture tests/fixtures/greenhouse_monzo.json

# Live scan of the Palantir Lever board (only where the source's terms permit it)
python -m jobscout scan --source palantir --dry-run

# Scan and deliver alerts for new matching roles via Telegram
python -m jobscout scan --source palantir --deliver

# Retry alerts that are still queued (no scan, no source request)
python -m jobscout deliver

# Read-only views of stored state
python -m jobscout summary --current
python -m jobscout status
```

Common options: `--db PATH`, `--config PATH`, `--fixture PATH`, `--accept-empty`.

Exit codes: `0` complete scan and clean delivery, `1` partial scan or a delivery failure, `2` failed scan or usage error.

`--dry-run` and `--deliver` are mutually exclusive, and one of them is required: a scan never sends anything unless you ask it to.

## Sources

| Source id | Employer | Adapter | State |
|---|---|---|---|
| `palantir` | Palantir Technologies | `lever` | Implemented; exercised against recorded payloads |
| `barclays` | Barclays | `workday` | Implemented; synthetic fixtures only |
| `lloyds_graduate` | Lloyds Banking Group | `workday` | Implemented; synthetic fixtures only; the `Graduate_careers` site only |
| `monzo` | Monzo | `greenhouse` | Implemented; synthetic fixtures only |
| `wise` | Wise | `sitemap_html` | Implemented; synthetic fixtures only; the vacancies sitemap only |
| `arm` | Arm | `sitemap_html` | Implemented; synthetic fixtures only; never the `/search-jobs/` path |

`--source` is required while more than one source is configured. The two Workday sources share one adapter: the listing is a read-only POST query that is paginated to the reported `total`, the requisition id is the job identity, and a detail page is fetched only when the listing shows a placeholder location such as "2 Locations".

Wise and Arm share the sitemap+HTML adapter: the sitemap supplies every role URL and its `lastmod`, and a detail page is fetched only when a role is new or its `lastmod` changed. Unchanged roles are recorded as observed without being re-fetched, so an unchanged role is never mistaken for a removed one. Pages are parsed from schema.org `JobPosting` JSON-LD; a page without it contributes only a title, which leaves the location unknown and the role in `needs_review` rather than guessing. Detail requests are spaced by 0.5s and capped per scan.

A `--fixture` file feeds list-shaped sources only (Palantir JSON, Workday listing, Monzo board), and a paginated Workday fixture stops as `partial` with a visible note instead of looping. Sitemap-driven sources need several documents, so their offline coverage lives in `tests/test_adapter_sitemap_html.py`.

**No live request has been made for Barclays, Lloyds Graduate, Monzo, Wise, or Arm.** Their fixtures are synthetic, so they prove parsing, pagination, discovery, failure handling, and scan safety — not that the real services return those shapes. See [docs/roadmap.md](./docs/roadmap.md) for what still needs permitted live validation.

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


## Configuration

Built-in defaults cover all six sources: Palantir, Barclays, Lloyds Graduate, Monzo, Wise, and Arm. An optional `config.toml` can change the database path and source settings:

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
profile = "wise"

# Optional, additive-only matching overrides (defaults stay unchanged if omitted)
[matching]
extra_exclude_terms = ["unpaid", "commission only"]

[matching.extra_family_terms]
data = ["bioinformatics analyst"]
```

Workday sources need `host`, `tenant`, and `site`; `locale` and `page_size` are optional. Sitemap sources select an in-code profile (`wise` or `arm`); `sitemap`, `max_detail_fetches`, and `request_delay_seconds` may override the defaults. Listing a `[[sources]]` block replaces the built-in source list, so keep every source you want to run.

Matching keywords are code-defined and versioned (`matching.RULES_VERSION`); reviewing them is tracked as decision D-026 in the [decision log](./docs/decisions.md).

Filter overrides can only **add**: extra title phrases inside an approved family (`data`, `software`, `quant`, `finance`, `consulting`, `business_analysis`) and extra title exclusions. Terms must be at least two characters and contain a letter or digit. Overrides cannot invent a family, change seniority or location rules, or cancel an approved exclusion — unknown families and unknown keys are rejected when the config loads, and exclusions are checked before matches. When overrides are active the rules version gains a digest (for example `m1.1+1a2b3c4d`), which is stored with every match decision and shown by `jobscout status`.

## Development

```bash
python -m pytest          # fixture-based; no network access
python -m ruff check .
python -m ruff format --check .
```

Tests use temporary databases, a fake HTTP client, and a fake Telegram transport; nothing is sent and no network call is made. An autouse fixture also keeps the suite away from your real `.env`.

## Access and compliance

Technical reachability, HTTP 200 responses, and permissive `robots.txt` rules do **not** establish permission for recurring automated access.

- This build is fixture-first and runs no schedule.
- A manual live smoke test is acceptable only where the source's terms permit it.
- **No source is currently approved for recurring production polling.** Terms review is required per source before M2/M5.
- The six-source terms review is in [docs/source-access.md](./docs/source-access.md). It found that Palantir and Wise prohibit automated monitoring or copying without written permission, that Barclays, Lloyds and Arm permit personal use but condition copying or storing their content on written permission, and that Monzo publishes no website-terms document at all.

Job-Scout never bypasses CAPTCHAs, authentication, or bot protection, and it only links to official employer or ATS listings.
