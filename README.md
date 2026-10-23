# Job-Scout

A personal UK early-career job monitor. It reads **official employer and ATS job boards**, records every role it observes, and will alert on Telegram when a **new** role matches the target role families and locations.

**Current state: M1 + M2.1 — local, Palantir-only.** Discovery, SQLite persistence, deterministic matching, silent baseline, new-role detection, update and closure handling, read-only `summary`/`status` views, and an **offline notification pipeline** (queued alerts, formatting, delivery state, duplicate prevention) are implemented. There is **no Telegram transport yet**: M2.1 sends nothing. Scheduling, deployment, and the other five sources are not implemented.

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

# Read-only views of stored state
python -m jobscout summary --current
python -m jobscout status
```

Common options: `--db PATH`, `--config PATH`, `--fixture PATH`, `--accept-empty`.

Exit codes: `0` complete scan, `1` partial scan, `2` failed scan or usage error.

`--dry-run` is mandatory in M1: it records scan and role state but sends nothing anywhere. There is no notifier yet.

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

## Notifications (M2.1, offline)

Matching new roles are queued as `pending` notification rows, one per `(chat_id, job_id)`, inside the same transaction that records the scan. A `Notifier` formats and delivers them through an injected `Transport` and records every attempt:

| Outcome | Recorded as | Retried? |
|---|---|---|
| Delivered | `sent` (with message id and timestamp) | Never |
| Ambiguous (e.g. timeout after send) | `ambiguous` | Never, by decision (D-024) |
| Transient (429, 5xx, network) | stays `pending` with attempt count and error | Yes, next delivery pass |
| Permanent rejection (other 4xx) | `failed` | No |

There is no Telegram transport in M2.1 and no credential is read, so nothing is sent anywhere. Tests drive the pipeline with a recording fake transport. `jobscout status` reports delivery counts and the last delivery error, with chat ids and bot tokens redacted. The real transport, environment-variable credentials, and a CLI delivery switch arrive in M2.2.


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
```

Matching keywords are code-defined and versioned (`matching.RULES_VERSION`); reviewing them is part of the M1 dry-run review.

## Development

```bash
python -m pytest          # fixture-based; no network access
python -m ruff check .
python -m ruff format --check .
```

Tests use temporary databases and a fake HTTP client only.

## Access and compliance

Technical reachability, HTTP 200 responses, and permissive `robots.txt` rules do **not** establish permission for recurring automated access.

- M1 is fixture-first and must not run a schedule.
- A manual live smoke test is acceptable only where the source's terms permit it.
- **No source is currently approved for recurring production polling.** Terms review is required per source before M2/M5.

Job-Scout never bypasses CAPTCHAs, authentication, or bot protection, and it only links to official employer or ATS listings.
