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
  source_revision: str | None
  content_hash: str
```

`source_revision` records the source's own version marker for the role (a sitemap `lastmod`
or an `updated_at`). It is deliberately excluded from `content_hash`: a revision bump
alone is not a content change and must not be reported as an update or re-alert.

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
  observed_ids: list[str]
```

Semantics:

| Status | Complete? | Meaning |
|---|---|---|
| `success` | Yes | The expected full result set was retrieved and parsed. |
| `success_empty` | Yes, with guard | The source validly reports zero jobs. |
| `partial` | No | Some records or detail pages could not be retrieved or parsed. |
| `failed` | No | No usable result set was obtained. |

`jobs` holds roles whose details were read in this scan. `observed_ids` holds roles the
source advertised without a detail refresh (an unchanged sitemap-driven role). Both sets
count as observed for closure detection; only a role in `jobs` can be new, updated, or
alert-eligible, and an observed-only role keeps its stored details and match decision. A
role that is absent from both sets is a closure miss, as before.

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
SourceAdapter.fetch_scan(config, http_client, *, known=None) -> ScanResult
```

`known` is an optional read-only mapping of source job id to the revision marker stored for
that role. Adapters that read the whole source in one response (Lever, Greenhouse,
Workday) ignore it; sitemap-driven adapters use it to skip unchanged detail pages. It is a
hint, never write access: adapters still do not touch SQLite.

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
| **Partial scan, existing ID** | ID already present | Update `last_seen_scan_id` and, when the role was refreshed, its fields and `content_hash`. Keep `status`, `closed_at`, and `consecutive_misses` untouched, so a closed role stays closed and an open role keeps its miss count reset. Do not alert. |
| **Complete scan, existing ID** | ID already present | Update `last_seen_scan_id`, fields, and `content_hash`; reset `consecutive_misses` to zero. A changed hash updates stored details but does not create a new alert. |
| **Complete scan, existing open ID missing** | ID absent from a complete scan | Increment `consecutive_misses`. At two, set `status = closed` and record `closed_at`. No closure alert in MVP. |
| **Partial or failed scan, existing ID missing** | ID absent from an incomplete scan | Do not change `consecutive_misses` and do not close. |
| **Closed job observed again in a complete scan** | ID appears again | Set `status = open` and reset misses. Do not re-announce by default; the existing notification record prevents a duplicate. |
| **Closed job observed again in a partial scan** | ID appears again, scan incomplete | Record the observation or refreshed details only. The role stays closed with its `closed_at` and miss count, because closure state changes require a complete scan. |
| **Repost with new ID** | New `source_job_id` | Treat as a new role and evaluate for alerting. This is an accepted duplicate-like limitation. |

### 4.2 Invariants

- A partial or failed scan cannot establish a baseline.
- A partial or failed scan cannot make a newly discovered role alert-eligible.
- A partial or failed scan cannot increment closure misses.
- A partial or failed scan cannot change closure state in either direction: it never closes a role and never reopens one.
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
next_run_at TEXT NULL        -- claimed (advanced) before a run starts, D-063
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

### 5.3 Retention policy (implemented, D-025; horizons awaiting Product Owner ratification)

Active data is never pruned, however old it is: source rows, open jobs, and any scan that a
job or a source still points at. Everything else ages out:

| Data | Retention | Rationale |
|---|---|---|
| `sources` rows | Forever | One row per configured source; needed for scheduling, status, and the baseline reference |
| Open jobs (and the scans they reference) | Forever | Needed for deduplication, reopening, closure accounting, and matching |
| Closed job *content* (`description`, `salary_text`) | 30 days after `closed_at` | Data minimisation: the bulky, source-derived text goes first; identity, title, location, dates, and match decision stay |
| Closed job *identity* row | 12 months after `closed_at` | Prevents re-alerting a reposted role for a year; deleting the row also deletes its notifications, so a role reposted after a year can alert once more (accepted) |
| `notifications` | With their job | Deleted when the job is purged; delivery state and message ids are the audit trail while the job exists |
| Notification error text (`last_error`) | 90 days, then cleared | Outcome and attempt count stay; error strings are the most likely place for incidental content |
| `scans` | 90 days, and only when unreferenced | Operational history for `status` and troubleshooting; a scan referenced by `first_seen_scan_id`, `first_complete_scan_id`, `last_seen_scan_id`, or `sources.baseline_scan_id` is always kept |
| Failure-notice state | Lives on the source row | It is an episode marker, not history |

Mechanics: pruning is an explicit `jobscout prune` command (add `--dry-run` to inspect), run
in bounded batches, idempotent, and never triggered by a scan or the scheduler. Rows are
removed child-first (notification errors, job content, notifications, jobs, scans) so foreign
keys stay satisfied, and each batch commits on its own, so an interrupted run resumes from
where it stopped instead of rolling back a long transaction.

Batches are selected in a deterministic order (`closed_at`, then `id`), so repeated runs
advance through the backlog instead of re-selecting the same rows; a batch's notifications are
deleted before its jobs, and a job is never deleted while any notification still references it.
`--dry-run` executes the same statements inside one transaction and rolls it back, so its
counts predict a *single* real invocation under the same batch limits, not the whole backlog.
The report sets `truncated` whenever eligible work remains in any phase, so a partial run is
never reported as completion. Notification error text ages from the most recent delivery
attempt (`last_attempt_at`, falling back to `created_at`), not from row creation.

Accepted consequences, stated rather than implied: a closed role reposted more than 12 months
after closure alerts again; a scan that an open job or the baseline points at is kept even
when it is older than 90 days; open jobs whose source was disabled are never pruned and never
closed automatically (closing them would be a product decision, not cleanup).

Logs are outside the database; host log rotation (14 to 30 days) remains a deployment concern.

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
| `WorkdayAdapter` | Barclays, Lloyds Graduate | Paginated CXS JSON (listing query POST) plus bounded detail lookups | Every page up to the reported `total` retrieved, no short page before the total, and no required detail lookup failed |
| `SitemapHtmlAdapter` | Wise, Arm | Sitemap discovery plus source-specific HTML detail parsing | Sitemap retrieved and parsed; every role accounted for as refreshed, observed-only, or explicitly skipped |

### 6.2 Adapter responsibilities

- Build source-specific URLs and request parameters.
- Respect source-specific constraints:
  - Wise: sitemap and job pages; never query-string job pages.
  - Arm: sitemap and job pages; never the disallowed search-results path.
  - Palantir: respect the requested crawl delay.
  - Workday: respect rate limits and retry guidance.
  - Lloyds Graduate: the `Graduate_careers` site only; the general LBG careers site is out of scope.
- Map source fields into `Job`.
- Resolve multi-location ambiguity where the source provides a detail page. Workday only requests a detail page for placeholder locations ("2 Locations"), bounded per scan.
- Normalize posted dates where possible; use `None` rather than guessing relative dates.
- Return explicit skip reasons and a precise scan status.
- Report roles observed without a detail refresh in `ScanResult.observed_ids`, so the scanner treats them as present for closure while keeping their stored details. Sitemap-driven adapters fetch a detail page only when the role is unknown, has no stored revision, or the sitemap revision (`lastmod`) changed; they space detail requests by a configured delay.
- Prefer a machine-readable block (`JobPosting` JSON-LD) over heuristics. When a page offers none, recover only the title and leave the location unknown rather than guessing it.

### 6.3 HTTP rules

- One shared HTTP client/session with timeouts.
- Descriptive user agent.
- Bounded retries for 429 and 5xx; respect `Retry-After`. GET requests retry inside the client; POST requests do not retry by default, because a repeated POST could duplicate an effect. Retry policy for a send therefore belongs to the delivery state machine. The one exception is a POST used as a read-only query with no side effect (the Workday listing search), which opts in explicitly with `post_json(..., retry=True)`.
- No retry for permanent 4xx.
- Low per-host concurrency; one request at a time per host unless evidence supports more.
- Log request failures without secrets or full response bodies.
- Cache sitemap metadata and avoid re-fetching unchanged detail pages. The stored
  `source_revision` is the cache key: a role is re-fetched only when it is unknown, has no
  stored revision, or the sitemap revision changed. Detail requests are spaced by a
  configurable delay (default 0.5s) and bounded per scan.

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
- Configuration may extend an approved family with extra title phrases (`[matching.extra_family_terms]`) and add title exclusions (`extra_exclude_terms`). Overrides are additive only: unknown family names and unknown keys are rejected, seniority and location rules cannot be changed, and exclusions are evaluated before matches, so configuration can never remove an approved category or exclusion. A rules version carrying an override digest is stored with every decision.
- Ambiguous cases are `needs_review`; they are not silently broadened into matches.
- Location parsing is a separate pure function with a small UK city/remote/hybrid alias table. No external geocoding.
- Multi-location roles qualify if any listed location qualifies.
- "Remote-friendly" without explicit UK eligibility is insufficient.
- Ambiguous locations are recorded with a reason and are not alert-eligible.

## 8. Failure handling and test strategy

### 8.1 Failure handling

- A source failure is isolated; other sources continue.
- A scheduled turn claims the source by advancing `next_run_at` before scanning (D-063), so two scheduler processes cannot scan the same source in one cycle, and a process killed mid-scan is retried at the next cadence rather than immediately. A claim that fails is backed off in memory for one cadence as well as best-effort in the database, so a failing write cannot make every source look due and spin the loop.
- An exception raised outside the adapter's own error handling is recorded as a failed scan with a `scheduler error:` summary, so the failure streak, the three-failure notice, and `status` all reflect it. Delivery and failure-notice work is wrapped per source, so a delivery or notice problem cannot abort the pass.
- A failed or partial scan never establishes baseline, alerts new roles, or increments closure misses.
- Bounded retries run inside a scan; exhausted retries produce a failed scan and a source failure count.
- Three consecutive source failures trigger one failure notice in M2, with no repeated spam until recovery.
- The status command must distinguish success, empty success, partial, and failed.
- Notification failures are handled by a persisted state machine (`pending`, `sent`, `ambiguous`, `failed`). Exactly-once external delivery is not assumed: an ambiguous outcome is recorded and not retried automatically (D-024), transient failures stay `pending` for a later pass, permanent rejections become `failed`, and a `sent` row is never re-sent.
- Trial measurement is a read-only projection over `scans`, `sources`, and `notifications` (`jobscout report`, D-064). No heartbeat or measurement-only state is recorded, so "availability" means "scans appeared when the cadence said they should".

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
