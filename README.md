# Job-Scout

A personal UK early-career job monitor. It reads **official employer and ATS job boards**, records every role it observes, and alerts on Telegram when a **new** role matches the target role families and locations.

**Current state: M1 + M2.3 — local, Palantir-only.** Discovery, SQLite persistence, deterministic matching, silent baseline, new-role detection, update and closure handling, read-only `summary`/`status` views, a notification pipeline (queued alerts, formatting, delivery state, duplicate prevention), a **Telegram Bot API transport**, and **additive-only matching-filter overrides in configuration** are implemented. Delivery is opt-in via `--deliver` and needs credentials in a local `.env` file (or in real environment variables). Scheduling and deployment are absent, and the other five sources are not implemented.

Product requirements: [docs/prd.md](./docs/prd.md). Technical design: [docs/architecture.md](./docs/architecture.md). Decisions: [docs/decisions.md](./docs/decisions.md). Delivery plan: [docs/roadmap.md](./docs/roadmap.md). Source research: [docs/sources.md](./docs/sources.md).

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

The HTTP client never retries a POST internally, so the state machine is the only retry authority and a retry cannot silently double-send. No `sent` row is ever sent again, which makes `deliver` idempotent.

Known limitation, accepted for the MVP (D-042): if the process dies between Telegram accepting a message and the database recording `sent`, that alert can be sent again later.

`jobscout status` reports delivery counts and the last delivery error, with the chat id and bot token redacted. No notification is sent unless you pass `--deliver` or run `deliver`.


## Configuration

Built-in defaults cover the Palantir source. An optional `config.toml` can change the database path and source settings:

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

# Optional, additive-only matching overrides (defaults stay unchanged if omitted)
[matching]
extra_exclude_terms = ["unpaid", "commission only"]

[matching.extra_family_terms]
data = ["bioinformatics analyst"]
```

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

Job-Scout never bypasses CAPTCHAs, authentication, or bot protection, and it only links to official employer or ATS listings.
