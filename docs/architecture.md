# Job-Scout — Architecture

**Status:** Approved; M1 implementation in place   
**Last updated:** 2026-10-22  
**Product authority:** [docs/prd.md](./prd.md)  
**Source evidence:** [docs/sources.md](./sources.md)  
**Decision log:** [docs/decisions.md](./decisions.md)  
**Delivery plan:** [docs/roadmap.md](./roadmap.md)

## 1. Scope and constraints

Job-Scout is a single-user Python application that monitors official employer careers pages and ATS job boards, detects new early-career roles, and later delivers Telegram alerts. The approved architecture direction is:

- Python 3.12 or newer.
- SQLite for persistent state.
- A modular monolith running as one process.
- Source-specific adapters behind a small common contract.
- Deterministic, explainable matching.
- A CLI for scan, dry-run, summary, and status.
- Fixture-based automated tests.

M1 is local and Palantir-only. It includes persistence and dry-run behaviour. It does **not** include Telegram, scheduling, deployment, or any other source adapter.

Explicit non-goals: microservices, message queues, an ORM, a web UI, AI ranking, multi-user accounts, browser automation, CAPTCHA bypass, and application tracking.

**Permission boundary:** technical access, HTTP 200 responses, and permissive robots rules do not establish permission for recurring automated access. No source is approved for recurring production polling until its terms are reviewed and accepted. M1 should be fixture-first; a manual live smoke test is acceptable only where the source's terms permit it. M1 must not run a recurring schedule.

## 2. System boundaries and data flow

```text
External sources                 Job-Scout process                    Local state
-----------------                ------------------                   -----------
Lever / Greenhouse JSON  --->  source adapter  --->  ScanResult  --->  SQLite
Workday JSON             --->  source adapter  --->  ScanResult  --->  (sources,
Sitemap + HTML           --->  source adapter  --->  ScanResult       scans, jobs,
                                                                       notifications)
                                        |
                                        v
                              deterministic matcher
                                        |
                          match / no_match / needs_review
                                        |
                     CLI dry-run + summary  [M1]
                     Telegram notifier      [M2, deferred]
```

Boundaries:

- **Source adapters** own HTTP access, raw parsing, source-specific field mapping, and source-specific completeness checks. They do not write to the database and do not send notifications.
- **The scan orchestrator** invokes one adapter, persists the scan and observed jobs, and applies baseline/closure rules. It lives in the CLI in M1 and in the scheduler later.
- **The matcher** is a pure decision layer. It does not fetch, persist, or notify.
- **SQLite** is the only durable state. It holds source status, scan history, job identity, baseline state, match decisions, and later notification history.
- **The notifier** (M2.1) reads queued notification rows from state, formats them, delivers them through an injected `Transport`, and records the outcome of every attempt. It is never called by adapters, and it does not decide what is new — the scan orchestrator queues work inside its own transaction. The production transport (M2.2) is the Telegram Bot API: one POST per alert, no internal HTTP retry, with ambiguous outcomes left untouched.
- There is no public inbound interface and no multi-user service boundary.

## 3. Contracts

### 3.1 Job

```text
Job:
  source_id: str
  source_job_id: str
  employer: str
  title: str
  location_raw: str
  location_normalized: LocationResult
  url: str
  description: str | None
  posted_at: date | None
  closing_at: date | None
  salary_text: str | None
  workplace_type: str | None
  content_hash: str
```

`LocationResult` should carry at least:

```text
LocationResult:
  qualifies: bool
  reason: str
  city: str | None
  country: str | None
  workplace_type: str | None
  ambiguous: bool
```

Rules:

- `source_job_id`, `title`, and `url` are mandatory. A record missing any of them is skipped with a reason and makes the scan partial.
- `source_job_id` is scoped to the source. It is the primary identity for deduplication.
- Location is never guessed. Ambiguous locations remain ambiguous and are not alert-eligible.
- Description is optional; its absence must not prevent identity or matching.

### 3.2 ScanResult

```text
ScanResult:
  source_id: str
  started_at: datetime
  finished_at: datetime
  status: success | success_empty | partial | failed
  complete: bool
  records_seen: int
  records_parsed: int
  source_reported_total: int | None
  jobs: list[Job]
  skipped: list[SkipReason]
  error_summary: str | None
```

Semantics:

| Status | Complete? | Meaning |
|---|---|---|
| `success` | Yes | The expected full result set was retrieved and parsed. |
| `success_empty` | Yes, with guard | The source validly reports zero jobs. |
| `partial` | No | Some records or detail pages could not be retrieved or parsed. |
| `failed` | No | No usable result set was obtained. |

`success_empty` must be treated cautiously. If a source that previously returned jobs suddenly returns zero, the adapter must verify that the source explicitly reports zero before classifying it as `success_empty`; otherwise the scan is `partial`. This prevents an upstream outage from looking like mass closure.

### 3.3 MatchDecision

```text
MatchDecision:
  decision: match | no_match | needs_review
  reasons: list[str]
  rules_version: str
```

`needs_review` is for ambiguous seniority, ambiguous location, or other uncertain cases. It is stored and shown in dry-run output, but it is not alert-eligible.

For M1, the latest decision can be stored on the job row (`match_decision`, `match_reason`, `rules_version`). A separate historical decision table can be added later if tuning evidence requires it.

### 3.4 Source adapter contract

```text
SourceAdapter.fetch_scan(config, http_client) -> ScanResult
```

The contract is intentionally small. Adapters:

- know their source's HTTP shape and field names;
- map records into `Job`;
- report source-specific completeness;
- return explicit skip reasons;
- do not access SQLite, the matcher, or Telegram.

A registry maps a configured source ID to an adapter class. There is no dynamic plugin discovery and no generic CSS-selector crawler.

## 4. Baseline, new-role, update, and closure transitions

State lives on `sources` and `jobs`:

```text
sources:
  baseline_scan_id
  baseline_completed_at
  last_success_at
  last_scan_status
  last_scan_complete
  consecutive_failures

jobs:
  first_seen_scan_id
  first_complete_scan_id
  last_seen_scan_id
  status: open | closed
  consecutive_misses
  content_hash
  match_decision
```

### 4.1 Transitions

| Event | Condition | Action |
|---|---|---|
| **First complete scan** | Source has no `baseline_scan_id` | Record the scan as baseline, set `baseline_scan_id` and `baseline_completed_at`, insert jobs with `first_seen_scan_id = scan` and `first_complete_scan_id = scan`. Send no individual alerts. The optional summary may list matching baseline jobs. |
| **Later complete scan, new ID** | `source_job_id` not present for this source | Insert with `first_seen_scan_id = scan` and `first_complete_scan_id = scan`. If the matcher returns `match`, the role is alert-eligible. |
| **Partial scan, new ID** | ID not previously present | Insert with `first_seen_scan_id = scan`, `first_complete_scan_id = NULL`. Store observed fields. Do not alert or close anything. A later complete scan sets `first_complete_scan_id` and may make it alert-eligible. |
| **Complete scan, existing ID** | ID already present | Update `last_seen_scan_id`, fields, and `content_hash`; reset `consecutive_misses` to zero. A changed hash updates stored details but does not create a new alert. |
| **Complete scan, existing open ID missing** | ID absent from a complete scan | Increment `consecutive_misses`. At two, set `status = closed` and record `closed_at`. No closure alert in MVP. |
| **Partial or failed scan, existing ID missing** | ID absent from an incomplete scan | Do not change `consecutive_misses` and do not close. |
| **Closed job observed again** | ID appears again | Set `status = open` and reset misses. Do not re-announce by default; the existing notification record prevents a duplicate. |
| **Repost with new ID** | New `source_job_id` | Treat as a new role and evaluate for alerting. This is an accepted duplicate-like limitation. |

### 4.2 Invariants

- A partial or failed scan cannot establish a baseline.
- A partial or failed scan cannot make a newly discovered role alert-eligible.
- A partial or failed scan cannot increment closure misses.
- A complete empty scan can close roles only through the normal two-complete-miss rule and only if the empty result was validated.
- The baseline scan itself never produces individual alerts.
- A matching role first observed only in a partial scan is not alerted until a complete scan observes it.

## 5. SQLite minimum state

Use SQLite with:

- `PRAGMA journal_mode = WAL`
- `PRAGMA foreign_keys = ON`
- `PRAGMA busy_timeout` set to a short bounded wait
- one writer (the scan orchestrator)

Migrations use `PRAGMA user_version` and a small ordered migration runner. No ORM.

### 5.1 Tables

**sources**

```text
id TEXT PRIMARY KEY
employer TEXT NOT NULL
adapter TEXT NOT NULL
config_json TEXT NOT NULL
enabled INTEGER NOT NULL DEFAULT 1
cadence_minutes INTEGER NOT NULL
baseline_scan_id INTEGER NULL
baseline_completed_at TEXT NULL
last_success_at TEXT NULL
last_scan_status TEXT NULL
last_scan_complete INTEGER NULL
last_error_at TEXT NULL
last_error_summary TEXT NULL
consecutive_failures INTEGER NOT NULL DEFAULT 0
next_run_at TEXT NULL
```

**scans**

```text
id INTEGER PRIMARY KEY
source_id TEXT NOT NULL REFERENCES sources(id)
started_at TEXT NOT NULL
finished_at TEXT NOT NULL
status TEXT NOT NULL          -- success | success_empty | partial | failed
complete INTEGER NOT NULL     -- 1 only for success/success_empty
records_seen INTEGER NOT NULL
records_parsed INTEGER NOT NULL
source_reported_total INTEGER NULL
skipped_count INTEGER NOT NULL DEFAULT 0
error_summary TEXT NULL
is_baseline INTEGER NOT NULL DEFAULT 0
```

**jobs**

```text
id INTEGER PRIMARY KEY
source_id TEXT NOT NULL REFERENCES sources(id)
source_job_id TEXT NOT NULL
employer TEXT NOT NULL
title TEXT NOT NULL
location_raw TEXT NOT NULL
location_normalized_json TEXT NULL
city TEXT NULL
country TEXT NULL
workplace_type TEXT NULL
url TEXT NOT NULL
description TEXT NULL
posted_at TEXT NULL
closing_at TEXT NULL
salary_text TEXT NULL
content_hash TEXT NOT NULL
first_seen_scan_id INTEGER NOT NULL REFERENCES scans(id)
first_complete_scan_id INTEGER NULL REFERENCES scans(id)
last_seen_scan_id INTEGER NOT NULL REFERENCES scans(id)
status TEXT NOT NULL          -- open | closed
consecutive_misses INTEGER NOT NULL DEFAULT 0
closed_at TEXT NULL
match_decision TEXT NULL      -- match | no_match | needs_review
match_reason TEXT NULL
rules_version TEXT NULL
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
UNIQUE(source_id, source_job_id)
```

**notifications** — created in M2, not required for M1

```text
id INTEGER PRIMARY KEY
chat_id TEXT NOT NULL
job_id INTEGER NOT NULL REFERENCES jobs(id)
status TEXT NOT NULL          -- pending | sent | ambiguous | failed
attempt_count INTEGER NOT NULL DEFAULT 0
telegram_message_id TEXT NULL
created_at TEXT NOT NULL
last_attempt_at TEXT NULL
sent_at TEXT NULL
last_error TEXT NULL
UNIQUE(chat_id, job_id)
```

### 5.2 Indices and state queries

- `jobs(source_id, status)` for open-role scans.
- `jobs(source_id, source_job_id)` unique for identity.
- `scans(source_id, started_at)` for status and troubleshooting.
- `notifications(status)` for later outbox processing.
- Status queries should expose `baseline_completed_at`, last scan status/completeness, records seen/parsed, consecutive failures, and recent notification states.

### 5.3 Retention proposal (not yet approved)

The approved principle is data minimisation. A concrete retention policy must be approved before unattended production. Proposed starting point:

- `scans`: 90 days.
- `jobs`: retain minimal identity and status while open; retain minimal closed identity for 12 months to prevent re-alerting, then purge.
- Job descriptions and salary/closing details: delete 30 days after closure.
- `notifications`: retain delivery status and message metadata for 12 months; purge error text after 90 days.
- Logs: 14 to 30 days, subject to host logging configuration.

This is a proposal for the retention decision, not an approved requirement.

### 5.4 Storage principles

- Do not store raw HTML or full source payloads by default. Keep normalized fields, hashes, scan counts, and bounded error summaries; use fixtures for parser tests.
- Do not store CVs, application data, or unrelated personal information.
- Keep the SQLite database outside source control.
- Back up the database with a simple periodic copy/backup mechanism; no database service is needed.

## 6. Adapter boundaries

### 6.1 Adapter set

| Adapter | Sources | Access shape | Completeness rule |
|---|---|---|---|
| `LeverAdapter` | Palantir | Full JSON list | HTTP success, valid list, every record mapped or explicitly skipped |
| `GreenhouseAdapter` | Monzo | Full JSON list | HTTP success, valid jobs list, every record mapped or explicitly skipped |
| `WorkdayAdapter` | Barclays, Lloyds Graduate | Paginated CXS JSON; sitemap fallback | All pages retrieved or total accounted for; required detail lookups successful |
| `SitemapHtmlAdapter` | Wise, Arm | Sitemap discovery plus source-specific HTML detail parsing | Sitemap retrieved; every new/changed detail page fetched and parsed |

### 6.2 Adapter responsibilities

- Build source-specific URLs and request parameters.
- Respect source-specific constraints:
  - Wise: sitemap and job pages; never query-string job pages.
  - Arm: sitemap and job pages; never the disallowed search-results path.
  - Palantir: respect the requested crawl delay.
  - Workday: respect rate limits and retry guidance.
- Map source fields into `Job`.
- Resolve multi-location ambiguity where the source provides a detail page.
- Normalize posted dates where possible; use `None` rather than guessing relative dates.
- Return explicit skip reasons and a precise scan status.

### 6.3 HTTP rules

- One shared HTTP client/session with timeouts.
- Descriptive user agent.
- Bounded retries for 429 and 5xx; respect `Retry-After`. GET requests retry inside the client; POST requests never retry internally, because a repeated POST could duplicate an effect. Retry policy for a send therefore belongs to the delivery state machine.
- No retry for permanent 4xx.
- Low per-host concurrency; one request at a time per host unless evidence supports more.
- Log request failures without secrets or full response bodies.
- Cache sitemap metadata and avoid re-fetching unchanged detail pages.

## 7. Matcher boundaries

The matcher is a pure, deterministic function:

```text
match(job, matching_rules) -> MatchDecision
```

It has no network, database, or notification side effects.

Inputs:

- Job title and structured fields.
- Raw and normalized location.
- Description only for explicit signals such as graduate/entry-level or sponsorship.
- Versioned matching rules.

Outputs:

- `match`, `no_match`, or `needs_review`.
- Human-readable reasons.
- `rules_version`.

Rules:

- Role-family categories and seniority exclusions are approved product requirements.
- Exact keyword and synonym lists are implementation defaults reviewed during M1.
- Ambiguous cases are `needs_review`; they are not silently broadened into matches.
- Location parsing is a separate pure function with a small UK city/remote/hybrid alias table. No external geocoding.
- Multi-location roles qualify if any listed location qualifies.
- "Remote-friendly" without explicit UK eligibility is insufficient.
- Ambiguous locations are recorded with a reason and are not alert-eligible.

## 8. Failure handling and test strategy

### 8.1 Failure handling

- A source failure is isolated; other sources continue.
- A failed or partial scan never establishes baseline, alerts new roles, or increments closure misses.
- Bounded retries run inside a scan; exhausted retries produce a failed scan and a source failure count.
- Three consecutive source failures trigger one failure notice in M2, with no repeated spam until recovery.
- The status command must distinguish success, empty success, partial, and failed.
- Notification failures are handled by a persisted state machine (`pending`, `sent`, `ambiguous`, `failed`). Exactly-once external delivery is not assumed: an ambiguous outcome is recorded and not retried automatically (D-024), transient failures stay `pending` for a later pass, permanent rejections become `failed`, and a `sent` row is never re-sent.

### 8.2 Test strategy

**Unit tests**

- Job normalization and mandatory-field skips.
- Location parsing and matching rules.
- Seniority, internship, role-family, and ambiguity decisions.
- Content hashing and no-re-alert update logic.
- Baseline/new/partial/closure state transitions.

**Fixture tests**

- Trimmed public JSON for Lever and Greenhouse.
- Workday response fixtures covering pagination, 429, relative dates, and multi-location.
- Wise/Arm sitemap XML and representative HTML detail pages.
- Fixtures are committed; no live network access in normal CI.

**Integration tests**

- Temporary SQLite database.
- Fake HTTP transport, fake clock, and fake notifier.
- Scenarios: first complete scan, later complete scan with new role, first observed in partial then complete, update, closure after two complete misses, no closure on partial/failed, restart, empty success, source failure isolation.
- Deterministic assertions on counts, statuses, and match decisions.

**Live smoke tests**

- Optional and separate from CI.
- Verify current source availability and approximate counts.
- Run only where terms permit; do not make live recurring polling part of M1.

**Production-like tests (later)**

- Seven-day unattended run.
- Process/host restart.
- Failure notification and recovery.
- Log redaction, backup/restore, and disk/log growth.

## 9. Deferred decisions

| Decision | Deferred until | Reason |
|---|---|---|
| Telegram delivery implementation | M2 | M1 proves discovery/matching; delivery is a separate failure domain |
| Notification policy for ambiguous Telegram outcomes | Before M2 | Exactly-once is impossible; at-least-once versus at-most-once is a product choice |
| Retention period | Before M5 | Data minimisation approved; concrete duration not yet decided |
| Scheduling and deployment | M5 | First usable version can run manually; unattended operation needs production-like validation |
| Exact HTTP/HTML libraries | M1 | Validate with fixtures; keep the dependency set small |
| Self-host packaging, Docker, licence | M6 | Future direction, not personal MVP scope |
| Multi-user, AI ranking, dashboard | Out of scope | No evidence of need for this MVP |

## 10. Source permission and access status

| Source | Technical access observed | Known access constraint | Recurring automated use |
|---|---|---|---|
| Palantir | Lever JSON reachable | Respect crawl delay | Not approved; terms review required |
| Barclays | Workday API reachable | 429/rate-limit risk | Not approved; terms review required |
| Lloyds Graduate | Workday API reachable | 429/rate-limit risk | Not approved; terms review required |
| Wise | Sitemap and job pages reachable | `/jobs?*` disallowed | Not approved; terms review required |
| Arm | Sitemap and job pages reachable | `/search-jobs/` disallowed | Not approved; terms review required |
| Monzo | Greenhouse JSON reachable | None observed | Not approved; terms review required |

**No source is approved for recurring production access yet.** M1 is local and Palantir-only. Fixture-based development is permitted; a manual live smoke test is allowed only if the source's terms permit it. M1 must not schedule recurring scans.

## 11. Confirmed facts, assumptions, and outstanding validation

**Confirmed from discovery**

- The six selected sources were reachable and exposed stable identifiers at validation time.
- Source families and field gaps are documented in [docs/sources.md](./sources.md).
- Wise and Arm have robots constraints; Workday has observed rate-limit risk.

**Assumptions to validate**

- A 30-minute cadence is useful and operationally acceptable.
- The six sources remain structurally stable enough for maintainable adapters.
- A deterministic keyword and location ruleset can reach acceptable precision after dry-run review.
- SQLite with one writer is sufficient for the single-user workload.
- A single process can isolate source failures effectively.

**Outstanding validation**

- Terms-of-service permission for recurring automated access.
- Current source counts and structural stability.
- Matching precision and recall on real samples.
- Telegram delivery behaviour and ambiguous-outcome handling.
- Seven-day unattended reliability.
- Retention and privacy review before production.
