# Job-Scout — Roadmap and M1 plan

- **Status:** Approved; M1, M2, M3, M4 (offline), M5.1 (tooling + live validation), M5.2 (scheduler), M5.3 (retention), M5.4.0 (offline trial preparation), M5.4.1 (Monzo validation scope), M5.4.2 (Lloyds Graduate validation profile) and M5.4.3 (UK-employer location context) delivered
- **Last updated:** 2026-10-23
- **Product authority:** [docs/prd.md](./prd.md)
- **Technical design:** [docs/architecture.md](./architecture.md)
- **Decision log:** [docs/decisions.md](./decisions.md)

## 1. Release definitions

- **M1 — Local Palantir dry-run:** proves discovery, baseline, new-role detection, matching, and persistence. No Telegram, scheduler, deployment, or other sources.
- **First usable version — M2:** Palantir plus Telegram delivery.
- **Functional MVP coverage — M4:** all six intended sources operational.
- **MVP complete — M5 gate:** six-source coverage plus a seven-day unattended reliability run and approved source permission.
- **Self-host readiness — M6:** optional post-MVP packaging for friends.

M1 is the first implementation milestone. It is not the completed product.

## 2. Milestone overview

| Milestone | Outcome | Exit gate |
|---|---|---|
| **M1** | Local Palantir scan, SQLite persistence, deterministic matching, baseline, dry-run and status CLI | M1 acceptance criteria in section 3 |
| **M2** | Palantir Telegram alerts with formatting, dedupe, and failure visibility | M1 accepted; Telegram token and chat ID; notification policy approved; Palantir permission sufficient for live delivery |
| **M3** | Barclays and Lloyds Graduate added through Workday (adapter delivered offline; live validation outstanding) | M2 accepted; pagination/429 fixtures; cross-source dedupe addressed; terms review still open for live use |
| **M4** | Wise, Arm, and Monzo added; all six sources operational (adapters delivered offline; live validation outstanding) | M3 accepted; parser fixtures; all six acceptance checks; terms review still open for live use |
| **M5** | Unattended operation, seven-day run, status and failure notices | M4 accepted; host/deployment chosen; retention approved; reliability threshold met |
| **M6** | Friend can clone and run an independent deployment | Post-MVP: packaging, licence/visibility, quickstart |

## 3. M1 — implementation-ready plan

### 3.1 Goal

Build a local, Palantir-only, dry-run application that:

1. Fetches and normalizes Palantir roles.
2. Persists source, scan, and job state.
3. Establishes a silent baseline on the first complete scan.
4. Detects roles first observed in later complete scans.
5. Applies deterministic matching and location rules.
6. Reports match, no-match, and needs-review outcomes.
7. Handles updates, partial scans, closures, and restarts safely.
8. Exposes status and a one-time current-matches summary.

### 3.2 In scope

- Python package and CLI skeleton.
- Palantir Lever adapter.
- Normalized `Job`, `ScanResult`, `LocationResult`, and `MatchDecision` contracts.
- SQLite schema and migration runner for sources, scans, and jobs.
- Deterministic matcher and location normalizer.
- Baseline, new-role, update, closure, and restart state transitions.
- Scan status classification: `success`, `success_empty`, `partial`, `failed`.
- `scan --source palantir --dry-run`.
- `summary --current` or equivalent read-only baseline summary.
- `status`.
- Fixture-based unit and integration tests.

### 3.3 Explicit non-goals

- Telegram or any notification delivery.
- Scheduling, daemons, cron, systemd, or deployment.
- Workday, Wise, Arm, Monzo, or any other source adapter.
- A notifications table or outbox.
- Multi-user support.
- Web UI, dashboard, or Telegram commands.
- AI or semantic ranking.
- Automatic applications or saved jobs.
- Recurring live polling.
- Production retention policy.
- Any claim that Palantir permits recurring automated access.

### 3.4 Implementation sequence

1. Create the Python package, CLI entry point, configuration loading, and fixture layout.
2. Add the normalized contracts and explicit skip-reason types.
3. Add SQLite schema/migrations for `sources`, `scans`, and `jobs`.
4. Implement the Palantir Lever adapter and trimmed public fixtures.
5. Implement the pure location normalizer and deterministic matcher.
6. Implement the scan orchestrator with baseline, new, update, partial, closure, and restart rules.
7. Add `scan --dry-run`, `summary --current`, and `status` output.
8. Add tests covering adapter mapping, matcher/location, state transitions, and CLI behaviour.
9. Run a manual live smoke test only if Palantir's terms permit it; otherwise use fixtures.
10. Review the dry-run sample with the Product Owner and tune the keyword defaults without changing approved categories.

### 3.5 M1 acceptance criteria

| ID | Acceptance criterion |
|---|---|
| **M1-A1** | `scan --source palantir --dry-run` against a representative fixture exits successfully and reports scan status, completeness, records seen, records parsed, and skipped count. |
| **M1-A2** | Every parsed record has the mandatory source ID, title, and official URL. Every skipped record has a reason; there are no silent drops. |
| **M1-A3** | The first complete scan establishes a baseline. It produces no alert candidates. The optional current-matches summary lists matching baseline roles and is read-only. |
| **M1-A4** | A role first observed in a later complete scan produces exactly one `match` candidate in dry-run output when it satisfies the approved rules. |
| **M1-A5** | Re-running the same complete snapshot produces zero new match candidates. No duplicate candidate is produced. |
| **M1-A6** | Changing a stored role's title, salary, location, closing date, or description updates the stored fields and content hash without creating a new match candidate. |
| **M1-A7** | A role missing from one complete scan remains open; missing from two consecutive complete scans becomes closed; missing from a partial or failed scan does not increment the miss count. |
| **M1-A8** | A role first observed only in a partial scan is not a match candidate. A later complete scan that sees it can make it eligible. |
| **M1-A9** | A valid empty response is recorded as `success_empty`, not failure. A suspicious empty response from a previously non-empty source is recorded as partial and does not close roles. |
| **M1-A10** | Restarting the CLI preserves baseline, job, scan, and match state. No duplicate candidate is produced. |
| **M1-A11** | The matching/location matrix produces the expected `match`, `no_match`, and `needs_review` outcomes with reasons. |
| **M1-A12** | `status` reports baseline state, last scan status/completeness, records seen/parsed, consecutive failures, last error, and needs-review count without exposing secrets. |
| **M1-A13** | The complete test suite passes without network access. Fixtures cover representative Palantir records and edge cases. |
| **M1-A14** | M1 requires no Telegram token or configuration and makes no requests to other sources. |
| **M1-A15** | No recurring live polling is performed. Any live smoke test is manual and permitted by the source's terms. |

### 3.6 Test cases

**Adapter and normalization**

- Valid Palantir record maps to all expected fields.
- Missing source ID, title, or URL produces a skip with a reason.
- Malformed JSON or non-list payload produces `failed`.
- HTTP 429/5xx produces a retryable failure; permanent 4xx produces `failed`.
- Empty list produces `success_empty` only when validated.
- Duplicate source IDs within one snapshot are handled deterministically.
- Missing salary, closing date, or posted date remains `None`.
- Relative or unparseable posted dates are not guessed.
- Multi-location or ambiguous location is flagged.

**Baseline and state**

- First complete scan establishes baseline; no candidates.
- Later complete scan with a new ID creates a candidate.
- Same ID on rerun creates no new candidate.
- Update changes fields/hash but no candidate.
- Partial scan with a new ID does not create a candidate; later complete scan does.
- One complete miss keeps a role open; two complete misses close it.
- Partial or failed miss does not increment the count.
- Empty success follows the guarded closure rule.
- Restart preserves state and prevents duplicates.
- Closed role seen again reopens without re-alerting.

**Matching and location**

- Graduate, entry-level, junior, and explicitly labelled internship roles match.
- Senior, lead, principal, staff, head, director, and VP roles do not match.
- Manager role matches only when explicitly part of a graduate programme.
- Unrelated families (sales, marketing, HR, legal, pure accounting/audit) do not match.
- Ambiguous title returns `needs_review`.
- London matches.
- UK-wide remote matches.
- UK hybrid outside London matches.
- Non-UK remote does not match.
- Multi-location matches if any location qualifies.
- Ambiguous or unresolved location returns `needs_review` and is not alerted.
- Description is used only for explicit early-career or sponsorship signals.

**CLI and status**

- Dry-run output distinguishes match, no-match, needs-review, and skipped records.
- Summary is read-only and does not change state.
- Status output is readable and contains no token or chat ID.
- Non-zero exit code on failed or partial scan where appropriate.
- Tests use a temporary database and leave no production state behind.

### 3.7 Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Palantir schema drift | Adapter mapping breaks | Trimmed public fixtures, explicit skip reasons, later live smoke test |
| Matching rules are too broad or narrow | Noise or missed roles | Dry-run sample review; versioned rules; no silent broadening |
| Baseline/partial transitions are wrong | Duplicate or missed alerts later | State-transition tests; complete/partial statuses; restart tests |
| Overfitting to Palantir | Other adapters need rework | Small common contract; source-specific mapping; no premature generic crawler |
| Location ambiguity | False positives | `needs_review`; no guessing; explicit reasons |
| SQLite migration/unique constraints | State corruption or duplicates | Small schema, migrations, unique identity, temporary DB tests |
| Source permission | Live use blocked | Fixture-first M1; manual live smoke only if permitted; terms review before recurring use |
| Scope creep | Delays first usable version | M1 non-goals are explicit; Telegram and scheduling stay out |

### 3.8 Dependencies and conditions

- Product Owner approval of the revised PRD, architecture, decisions, and this roadmap.
- Palantir terms review started; no recurring live polling until permitted.
- Python 3.12+ environment and test tooling available at implementation time.
- Exact keyword defaults reviewed during M1; categories and exclusions remain fixed.
- No Telegram token required.

### 3.9 Definition of done

M1 is done when:

- All M1 acceptance criteria pass.
- The test suite passes locally without network access.
- A reviewed Palantir dry-run sample has been shown to the Product Owner.
- No Telegram, scheduler, deployment, or other source adapter has been added.
- The implementation has not silently expanded product scope.
- The PRD/architecture/decisions documents remain consistent with the code.

### 3.10 M1 implementation status

Delivered 2026-10-22. All work is local; no scheduler, notifier, or second source was added.

| Area | Status |
|---|---|
| Package, CLI entry point, configuration | `jobscout scan`, `jobscout summary --current`, `jobscout status`; optional `config.toml`; stdlib-only runtime |
| Contracts (`Job`, `ScanResult`, `LocationResult`, `MatchDecision`, `ScanOutcome`) | Done |
| SQLite schema and ordered migration runner | `sources`, `scans`, `jobs`, `PRAGMA user_version`; WAL and foreign keys on |
| Palantir Lever adapter | One request per scan, explicit skip reasons, no raw payload storage |
| Location normalizer and deterministic matcher | `matching.RULES_VERSION = "m1.1"`; reasons recorded per role |
| Baseline, new-role, update, partial, closure, restart transitions | Done, covered by state-transition tests |
| Suspicious-empty guard | Done, with an explicit `--accept-empty` override |
| Fixtures and tests | 121 tests, no network access |

Acceptance evidence:

| Criterion | Where it is verified |
|---|---|
| M1-A1 | `test_cli.py::test_scan_dry_run_reports_a_baseline` |
| M1-A2 | `test_adapter_lever.py::test_unusable_records_are_skipped_with_a_reason` |
| M1-A3 | `test_scanner.py::test_first_complete_scan_establishes_a_silent_baseline`, `test_cli.py::test_summary_lists_current_matches_and_is_read_only` |
| M1-A4 | `test_scanner.py::test_new_role_after_baseline_produces_one_candidate` |
| M1-A5 | `test_scanner.py::test_repeating_the_same_snapshot_produces_no_duplicate_candidate` |
| M1-A6 | `test_scanner.py::test_changed_role_details_update_without_re_alerting` |
| M1-A7 | `test_scanner.py::test_role_closes_after_two_consecutive_complete_misses`, `test_partial_scan_never_increments_closure_misses`, `test_failed_scan_never_increments_closure_misses` |
| M1-A8 | `test_scanner.py::test_role_first_seen_in_a_partial_scan_becomes_eligible_in_a_complete_scan` |
| M1-A9 | `test_scanner.py::test_valid_empty_first_scan_is_a_complete_baseline`, `test_suspicious_empty_result_from_non_empty_source_is_partial`, `test_accepted_empty_result_follows_the_normal_closure_rule` |
| M1-A10 | `test_scanner.py::test_restart_preserves_state_and_prevents_duplicate_candidates` |
| M1-A11 | `test_matching.py`, `test_location.py` |
| M1-A12 | `test_cli.py::test_status_reports_operational_state_without_secrets` |
| M1-A13 | `test_adapter_lever.py` and the fixture set; `test_cli.py::test_fixture_scan_uses_no_network` |
| M1-A14 | `test_cli.py::test_scan_requires_dry_run_in_m1`; no Telegram code or configuration exists |
| M1-A15 | No scheduler or loop exists; only the explicit single `scan` command |

Known deviations and limitations:

- `scan` without `--dry-run` exits with code 2 rather than silently recording. M1 has no notifier, so the flag is mandatory.
- `--accept-empty` was added to make the suspicious-empty guard overridable by an explicit operator decision.
- Because any unmappable record makes a scan `partial` (D-033), a persistently malformed record would block baseline and alerting. It is always visible in `status` and dry-run output, but resolving it requires an adapter fix.
- Matching precision has only been exercised on curated fixtures, not on a live Palantir snapshot. D-026 remains pending review.
- Filter terms are code-defined in M1; configurable filters (PRD FR-12) are scheduled for M2 under D-035.
- M1 sign-off still requires Product Owner review of this dry-run sample and of the default keyword list.

## 4. Post-M1 gates

### M2 — Telegram delivery for Palantir

- Add notification state and delivery only after M1 is accepted.
- Approve the ambiguous-delivery policy before implementation.
- Test fake success, 429, 5xx, permanent 4xx, timeout-after-send, restart, and duplicate suppression.
- Deliver one live test alert if Palantir permission allows.
- Complete FR-12: the Telegram destination and matching filter overrides become configuration, joining the already configurable database path and source settings. Overrides may add terms but must not remove approved categories or exclusions.
- First usable version is complete when Palantir alerts work reliably enough for daily use.

**M2 increments**

| Increment | Outcome | Status |
|---|---|---|
| **M2.1** | Offline pipeline: `notifications` table, message formatting, delivery state machine, duplicate prevention, recording fake transport, delivery visibility in `status` | Delivered 2026-10-23 |
| **M2.2** | Real Telegram transport with `.env` or environment credentials, redaction, and delivery CLI paths | Delivered 2026-10-23; live test message still needs explicit approval |
| **M2.3** | Matching-filter overrides in configuration (FR-12 complete) | Delivered 2026-10-23 |

M2.1 decisions: ambiguous outcomes are recorded and never retried automatically (D-024); transient failures stay `pending` for a later pass; permanent rejections become `failed`; `sent` rows are never re-sent (D-036); the transport and any CLI delivery switch arrive in M2.2 (D-037); proactive failure notices stay in M5 (D-038).

M2.2 decisions: one POST per alert with no internal HTTP retry and explicit outcome mapping (D-039); credentials only from environment variables, with shape checking and full redaction (D-040); `scan --deliver` and `deliver` as the only sending paths (D-041); the send-then-record crash window is accepted (D-042).

M2.1 acceptance evidence: `tests/test_notifier.py` (formatting, queueing, dedupe, restart, sent/failed/ambiguous/retry paths, redaction) and `tests/test_cli.py::test_status_reports_delivery_state_without_the_chat_id`.

M2.2 acceptance evidence: `tests/test_telegram.py` (outcome mapping, message id, redaction, transport-to-state-machine integration), `tests/test_http.py` (POST never retries; timeout vs connection failure classification), `tests/test_config.py` (environment-only credentials), and `tests/test_cli.py` (missing configuration, dry-run/`--deliver` exclusivity, delivery reporting, idempotent `deliver`). No live Telegram message has been sent.

M2.3 decisions: additive-only filter overrides with rejected unknown keys and families, and an override digest in the rules version (D-044); FR-12 closed (D-035).

M2.3 acceptance evidence: `tests/test_matching_overrides.py` (normalisation, validation, added family terms, added exclusions, and proof that overrides cannot cancel family, seniority, location, or PhD exclusions) and `tests/test_config.py` (parsing, unknown family, unknown key, type errors), `tests/test_cli.py` (override config changes scan decisions; `status` shows the active rules version).

### M3 — Workday: Barclays and Lloyds Graduate

- Add the Workday adapter only after M2.
- Validate pagination, 429 handling, relative dates, multi-location detail lookup, and partial-scan safety.
- Require terms review before recurring production use.
- Confirm expected counts with fixtures and a permitted live smoke test.

**M3 delivery (offline, synthetic fixtures)**

| Item | Status |
|---|---|
| Shared `WorkdayAdapter` for `barclays` and `lloyds_graduate`, registered in the adapter registry and added to the default source set | Delivered 2026-10-23 |
| Listing pagination against the reported `total`, including over-fetch protection, page cap, and short/empty-page detection | Delivered (fixtures) |
| Requisition-id identity (`bulletFields[0]`), skip reasons for unfixable records, in-scan duplicate suppression | Delivered (fixtures) |
| Placeholder-location resolution through the detail endpoint, bounded per scan, failing safe to `partial` | Delivered (fixtures) |
| Relative `postedOn` interpretation for explicit forms only; buckets stay `None` | Delivered (fixtures) |
| 429/5xx and malformed-payload handling; partial scans cannot close roles or alert | Delivered (fixtures) |
| Lloyds scope held to the `Graduate_careers` site | Delivered; the general LBG careers site is out of scope |
| Live validation against real payloads | **Not done**; requires permitted access (D-048) |

M3 decisions: one shared adapter with requisition-id identity, `total`-based completeness, and bounded detail lookups (D-045); the read-only listing POST is the only retried request (D-046); the sitemap fallback is deferred (D-047); fixture-only evidence is recorded as an open validation gap (D-048).

M3 acceptance evidence: `tests/test_adapter_workday.py` (pagination, request shape, retry opt-in, identity, Lloyds site scope, relative dates, multi-location resolution, resolution failure, and the lookup cap, detail responses with missing/empty/non-string/placeholder-only locations, pagination edge cases, malformed payloads, skips, duplicates, missing options, transport failures, scanner integration for partial-scan safety and new-role alerting, restart dedupe), `tests/test_config.py` (default source set and Workday options), `tests/test_http.py` (POST retries only on explicit opt-in). No live employer request has been made.

M3 cross-source deduplication gate: the only cross-source overlap risk is the same role advertised by two employers, and Barclays and Lloyds are separate employers, so no shared identity exists to deduplicate on. Deduplication therefore stays per `(source_id, source_job_id)` as designed, and no cross-source subsystem is introduced. This is a reasoned no-op, not validated against live listings; revisit only if live data shows duplicated roles across sources.

Live validation still required before these sources can be trusted or run unattended:

1. Confirm the listing and detail response shapes, the requisition-id field, and the locale-prefixed public URL pattern.
2. Confirm real wording for multi-location placeholders and relative posted dates.
3. Confirm observed 429/5xx behaviour and whether `total` reflects the full result set.
4. Complete terms review for recurring automated access (D-027); reachability is not permission.

### M4 — Wise, Arm, and Monzo

- Add sitemap/HTML and Greenhouse adapters after M3.
- Validate sitemap caching, detail-page fetch rules, parser fixtures, and parse-failure reporting.
- Never request Wise query-string job pages or Arm search-results paths.
- Confirm all six sources and full acceptance matrix.

**M4 delivery (offline, synthetic fixtures)**

| Item | Status |
|---|---|
| `GreenhouseAdapter` for `monzo`: one board response per scan, HTML-escaped content unescaped, location/offices mapping, salary fallback | Delivered 2026-10-23 (fixtures) |
| `SitemapHtmlAdapter` for `wise` and `arm`: in-code profiles, index or urlset discovery, job-URL identity, JSON-LD fields | Delivered (fixtures) |
| Incremental refresh: only new, unrevisioned, or changed roles are fetched; unchanged roles are reported as observed-only and keep their stored details | Delivered; contract covered end to end |
| Politeness: configurable request delay (0.5s default), sitemap document cap, detail-fetch cap, no disallowed paths | Delivered (defaults unvalidated) |
| Partial-scan safety: malformed sitemap, unmatched URL pattern, detail failure, and cap exhaustion all make the scan `partial` and never close roles | Delivered (fixtures) |
| All six intended sources configured (Palantir, Barclays, Lloyds Graduate, Monzo, Wise, Arm) | Delivered; none validated live |
| Live validation against real payloads for Monzo, Wise, and Arm | **Not done**; requires permitted access (D-055) |

M4 decisions: observation separated from refresh with a stored revision marker (D-049); Greenhouse mapping and double-escaped content (D-050); in-code sitemap profiles with JSON-LD first and a title-only fallback (D-051); discovery constraints and loud failure on unmatched patterns (D-052); request spacing and caps (D-053); cross-source dedupe closed as a reasoned no-op (D-054); fixture-only evidence recorded as an open gap (D-055).

M4 acceptance evidence: `tests/test_adapter_greenhouse.py` (field mapping, HTML unescaping, location and offices fallback, salary sources, malformed payloads, transport failure, skips, duplicates, empty board), `tests/test_adapter_sitemap_html.py` (index traversal, child-sitemap filtering, non-job URL handling, JSON-LD and fallback mapping, multi-location, duplicate ids, malformed sitemaps, detail failure, revision-skip/changed/unrevisioned refresh, fetch caps, request spacing, scanner integration for unchanged roles, new-role alerting, and two-scan closure), `tests/test_incremental_scan.py` (the observed-versus-refreshed contract), `tests/test_http.py` (text GET, fixture text), and `tests/test_config.py` (six default sources).

Live validation still required before these sources can be trusted or run unattended:

1. Confirm the Greenhouse board response for Monzo and the real `location`/`content`/salary shapes.
2. Confirm the Wise and Arm sitemap structure, the job URL patterns, and JSON-LD availability per page.
3. Confirm whether `lastmod` really changes when a role's details change; if it does not, refresh must fall back to a different signal.
4. Confirm observed request-tolerance and rate limits for the 0.5s delay and the fetch caps.
5. Complete terms review for recurring automated access (D-027); reachability is not permission.

### M5 — Unattended operation and MVP completion

- Deploy only after M4 and source-permission review.
- Approve the retention policy.
- Run for seven days; verify restart recovery, failure notices, status, log redaction, backup/restore, and no silent gap longer than one scheduled cycle.
- MVP complete only when all six sources and the reliability gate pass.

**M5 increments**

| Increment | Outcome | Status |
|---|---|---|
| **M5.1** | Bounded live validation of one source (Palantir): one request per run, no retries, no database, fixture comparison, capped local artifacts | Delivered 2026-10-23 with offline tests; the single authorised live run was executed and passed |
| **M5.4.1** | Validation scope extended to Monzo (Greenhouse envelope): the same one-request check, a per-source shape profile, a synthetic Monzo baseline fixture | Delivered 2026-10-23 with offline tests; the single authorised live Monzo request was executed and passed |
| **M5.4.2** | Validation scope extended to Lloyds Graduate (Workday): per-profile request budget, POST support, bounded detail lookups | Delivered 2026-10-23 with offline tests; the single authorised live request set was executed and passed |
| **M5.4.3** | UK-employer location context (D-065): ambiguous UK town names qualify for boards that only advertise UK roles | Delivered 2026-10-23 with offline tests; the live Lloyds board previously left four graduate schemes in review |
| **M5.2** | Scheduler and proactive failure notices (D-038), per-source cadence and jitter | Delivered 2026-10-23 with offline tests; unattended operation not started |
| **M5.3** | Retention policy (D-025) and enforcement | Delivered 2026-10-23 with offline tests; implemented as an explicit command. The four retention horizons remain engineering defaults awaiting explicit Product Owner ratification |
| **M5.4.0** | Offline reliability preparation for the trial: scheduler failure hardening, restart and duplicate-process behaviour, host operating procedure, trial measurement, retention operations | Delivered 2026-10-23 with offline tests and documentation. No trial started, no scheduled task created |
| **M5.4** | Seven-day unattended run and the reliability gate | Not started; blocked on the per-source permission decisions |

M5.2 decisions: `sources.next_run_at` is the only schedule state, a missed cycle is never replayed as a burst, and each source runs and reschedules in isolation (D-059). A source that fails three consecutive scans triggers exactly one notice per failure episode, claimed before sending, with a single send attempt and no retry on an ambiguous outcome; a completed scan ends the episode (D-060).

M5.2 acceptance evidence: `tests/test_scheduler.py` (due-ness, cadence, jitter, disabled sources, per-source failure isolation, one notice per streak, recovery then a second streak, no notice without a chat id, ambiguous notice recorded and not retried, partial scans not counted as failures, restart resuming from persisted `next_run_at`, no burst after ten missed cycles, queued alerts delivered after a restart, bounded loop with capped sleeps, baseline-then-new-role flow) and `tests/test_cli_schedule.py` (`schedule --once`, nothing due on the second pass, bounded loop, `status` showing the next run, Telegram optional).

Blockers and limits for M5.2:

- **Unattended polling is not started.** `schedule` without `--once` runs a loop, but nothing runs it in the background and no deployment exists.
- **The retention horizons (D-025) are implemented but not ratified**, so the seven-day run cannot yet be judged against an agreed retention policy.
- **Per-source permission decisions (D-027) are unresolved**; AC-19 still blocks deployment of Palantir and any other source.
- The loop is single-process and single-threaded by design; there is no supervisor, restart-on-crash, or alerting if the process itself dies. That belongs to M5.4's reliability gate.

M5.3 decisions: the retention policy in D-025 follows the shape already proposed in architecture 5.3, and pruning is an explicit, bounded, idempotent command that never runs during a scan (D-061). Batches advance in a deterministic order, notification error text ages from the latest delivery attempt, a dry run predicts exactly one real invocation, and `truncated` reports remaining work in every phase (D-062). Implementation status is not approval: the 30 / 365 / 90 / 90-day horizons are engineering defaults that the Product Owner has not yet ratified.

M5.3 acceptance evidence: `tests/test_retention.py` (open jobs never pruned or stripped; closed content cleared only past 30 days while identity stays; closed identity and its notifications deleted past 12 months; referenced scans — first-seen and baseline — preserved while unreferenced old scans are deleted; delivery error text cleared with the outcome retained; idempotent second run; batch limits with truncation reporting; dry run changes nothing; invalid policies rejected; a retained closed role and a retained open role both avoid re-alerting after pruning; a scan never prunes; CLI dry-run, apply, and invalid-policy paths). The D-062 corrective pass adds: batches advancing past roles that never had notifications (the case that previously stopped early); a batch limit stopping between batches with `PRAGMA foreign_key_check` clean; seven expiring roles converging over repeated bounded runs; a recent error on an old notification retained while an old error is cleared and a NULL `last_attempt_at` falling back to creation time; dry-run counts equal to the same real run with nothing written; and `truncated` true then false in each of the four cleanup phases.

M5.3 limits:

- Pruning commits batch by batch rather than in one transaction, so an interrupted run leaves partial progress — idempotent and resumable by design. A bounded run reports `truncated`; repeat the command until it reports completion.
- `--dry-run` predicts one real invocation under the current limits, not the whole backlog, so a large backlog needs either one dry run per pass or a larger `--max-batches`.
- Open jobs from a disabled source are never pruned and never auto-closed. Closing them would change product behaviour and is left as a decision.
- A closed role reposted more than 12 months after closure can alert again; that is the accepted cost of purging its identity.
- Nothing schedules `prune`; running it periodically (e.g. alongside the seven-day trial) is an operational decision for M5.4.


Blockers and limits for M5.3:

- The four horizons (30 / 365 / 90 / 90 days) are implemented defaults, not a ratified product decision (D-025).
- Nothing schedules `prune`; that stays an operator action.

M5.4.0 decisions: a scheduled turn claims the source by advancing `next_run_at` before the scan (compare-and-swap that also requires the value to be due), an exception raised outside the adapter's error handling is recorded as a failed scan with a `scheduler error:` summary, and delivery plus failure-notice work is wrapped per source so neither can abort a pass (D-063). Trial measurement is a read-only scorecard over persisted scans and notifications with no heartbeat state (D-064). The operating procedure lives in [docs/runbook.md](./runbook.md) and is documentation only.

M5.4.0 acceptance evidence: `tests/test_scheduler.py` (an unexpected exception recorded as a failed scan with the streak, error summary, and scan row it implies; three such failures producing exactly one notice and a fourth producing none; a success ending the streak and clearing the episode; a source claimed by another process skipped rather than scanned; a claim error isolated to its source; queued alerts delivered even when the scan raises; delivery and notice errors reported as `post_run_error` without aborting the pass; a run interrupted after claiming retried at the next cadence with no burst) and `tests/test_report.py` (window filtering, expected cycles from cadence, coverage capped at 1, status split, gaps and missed episodes, mean delay, single-scan case, current source state, delivery tallies inside and outside the window, per-source filter, empty database, non-positive window rejected, no writes, text format, CLI success, invalid window, missing database).

M5.4.0 limits:

- The claim guards against duplicate *scans*, not duplicate *processes*: two schedulers still both exist, and each simply skips what the other claimed.
- Availability in the report is a proxy derived from scans per expected cycle; a process that was up but silently idle is indistinguishable from one that was down.
- The Task Scheduler procedure is untested on a real scheduled task, because creating one is out of scope until the trial is approved.
- `prune` remains opt-in: it is documented in the runbook and deliberately not scheduled.

M5.1 decisions: the check makes exactly one read-only request with retries disabled, never opens the database, writes only capped and trimmed artifacts under `data/validation/`, and exits non-zero unless the live payload is shape-compatible **and** the real adapter parses every record with zero skips (D-057). The personal-use risk acceptance for this check is narrow and does not authorise recurring or unattended access to any source (D-058).

M5.1 acceptance evidence: `tests/test_validate.py` (payload comparison, sample trimming, one-request budget, failure-without-artifacts, shape-drift-keeps-evidence, CLI refusal without `--confirm-live`, source-scope rejection, missing fixture, non-positive sample size, and a run that proves the database is never opened). `python -m jobscout validate` without `--confirm-live` exits `2` and constructs no client.

M5.1 live evidence (Palantir, one request, exit 0): 307 live records against the 22-record fixture, 307 parsed, 0 skipped; 10 field names present that the fixture did not model, and `salaryRange` absent from the live payload, which confirms the fixture's salary field was invented. The check therefore proves reachability, envelope shape, and full parsing for that board on that day; it proves nothing about permission, value correctness, or unattended behaviour.

M5.4.1 scope and limits: `validate` gained a `ValidationProfile` per source, so an enveloped payload (roles under `jobs`, as Greenhouse returns them) is compared and sampled by its role records rather than by the envelope. Monzo's baseline is `tests/fixtures/monzo_validation.json`, synthetic and limited to the fields the adapter reads, so a field it omits surfaces as a `WARN` rather than a failure. A `PASS` for Monzo would prove one GET reached `boards-api.greenhouse.io`, the envelope carried a `jobs` array, the watched field types matched, and every role mapped with zero skips. It would **not** prove permission for recurring access, that salary text is extracted correctly (the pay-range and metadata fallbacks remain unvalidated assumptions), that HTML-to-description stripping is faithful, that location names are populated, that matching produces useful alerts for this employer, or anything about scheduling, deduplication, retention, or delivery.

M5.4.1 live evidence (Monzo, one request, exit 0, 2026-10-23T17:08Z): 72 live roles against the 3-record baseline, 72 parsed, 0 skipped, complete scan. Confirmed against the live payload:

- the envelope is `{meta, jobs[]}` as assumed, and every record carried `title`, `absolute_url`, `location.name`, `offices`, `content`, `updated_at`, and `first_published` with the types the adapter expects;
- `location.name` is populated with real values (`London`, `Barcelona`, `Cardiff, London or Remote (UK)`), and the existing location rules classify them correctly — multi-site UK strings qualify as UK remote, `Barcelona` is excluded as a non-UK city;
- 10 field names the baseline never modelled are present (`ai_disclaimer`, `ai_opt_out_request_url`, `application_deadline`, `company_name`, `data_compliance`, `departments`, `include_ai_disclaimer`, `internal_job_id`, `language`, `requisition_id`); the adapter ignores them safely;
- `metadata` is present but **null** in every record, and `application_deadline` was null in all sampled roles, so the salary-from-metadata fallback and any closing-date support are dead ends on this board today;
- `pay_input_ranges` does **not** appear at all, so Monzo roles carry no structured salary: alerts will omit salary for this source, and the pay-range formatter is unexercised in production;
- `absolute_url` uses the `job-boards.greenhouse.io` host (the baseline had `boards.greenhouse.io`); the adapter stores whatever it is given, so this was cosmetic, and the baseline now matches.

That run also exposed one diagnostic inaccuracy: a field present with a null value was reported as
`optional field absent from every live record`. The report now distinguishes `is null in every live
record` from `absent from every live record`, with a regression test built from the observed shape.

Still unverified for Monzo: permission for recurring automated access (no published website terms),
faithfulness of HTML-to-text description stripping, whether departments would improve matching, and
everything about scheduling, deduplication, retention and delivery.

M5.4.1 acceptance evidence: `tests/test_validate.py` (per-source profile resolution and the `jobs` extraction, an enveloped payload passing with a role-level sample rather than the envelope, an envelope without a `jobs` array rejected by the adapter with no artifact, a record missing `absolute_url` failing on the skip count, type drift in `updated_at` failing, an extra live field such as `pay_input_ranges` warning without failing, a null field reported differently from a missing one, and the CLI resolving each source's own default fixture while still never opening a database).

M5.4.2 scope and limits (Lloyds Graduate, Workday): the check gained a per-source request budget, because a Workday scan is one listing POST plus one detail GET per posting whose location is a placeholder. `RecordingClient` now counts POSTs against the same budget and remembers the first response, which is the listing the shape contract describes. `ValidationProfile` carries `request_budget` (Lloyds: 6), `require_complete` (Lloyds: false, because a capped or disabled detail lookup leaves the scan `partial` by design) and `adapter_options`, so the check can run the real adapter with a smaller bound than production (`page_size` 20, `max_detail_lookups` 0). A budget violation is refused before the request reaches the network.

What a Lloyds PASS would prove: the listing POST returned `{total, jobPostings[]}`, the watched fields (`title`, `externalPath`, `locationsText`, `postedOn`, `bulletFields`) had the expected types, and every posting mapped with zero skips. It would **not** prove: that real postings use the `Graduate_careers` site rather than the general LBG careers site, that relative `postedOn` strings (`Posted 5 Days Ago`) are parsed as intended on every wording, that the requisition id in `bulletFields[0]` is stable across refreshes, that multi-site placeholders resolve through detail lookups within the cap, that the derived public URL pattern is correct, or permission for recurring access.

M5.4.2 live evidence (Lloyds Graduate, exit 0, 2026-10-23T17:53Z): two listing POSTs (pages of 20) returned 33 graduate postings, all 33 mapped with 0 skips, and `Shape: identical to the recorded fixture` ? the watched fields matched the synthetic baseline exactly, with no new or missing fields. The sampled roles are real graduate schemes (Software Engineer, Occupational Psychology, People Science) with city-level `locationsText` (`London`, `Edinburgh`, `Bristol`, `Leeds`), numeric requisition ids in `bulletFields[0]`, and `postedOn: "Posted Today"`, which exercised a relative-date wording the fixture never had.

A first attempt with the earlier profile (budget 4, three detail lookups allowed) was refused by the budget after four requests and wrote no artifact: the board is paginated beyond one page and carries more multi-site postings than that budget could resolve. That refusal is itself the evidence that the budget is enforced before the request reaches the network. The profile was then narrowed to listing pages only (`max_detail_lookups` 0, budget 6), which is why this run reports `partial` with the note `detail lookups capped at 0`.

Open question this raised, not yet measured: production scans allow 10 detail lookups (`WORKDAY_MAX_DETAIL_LOOKUPS`) and an unresolved placeholder makes a scan partial, which refuses to baseline or alert. At least three multi-site postings exist on the two listing pages seen, so a live dry-run scan is needed to learn whether 10 is enough for this board before Lloyds can be relied on for alerts.

Still unverified for Lloyds: that detail lookups resolve real placeholders on this site, that relative `postedOn` wordings other than today/5 days parse as intended, that the derived public URL pattern resolves, whether the 33 postings are all in the UK, and permission for recurring access (LBG permits personal use but conditions copying or storage of content).

M5.4.2 acceptance evidence: `tests/test_validate.py` (the Lloyds profile's budget and options, a single POST with no detail lookups when every location is concrete, exactly one detail lookup for one placeholder, a budget violation refused before the second detail request reaches the network, a resolving profile proving one detail lookup per placeholder, a capped lookup reported as a partial-scan warning rather than a failure, `externalPath` type drift failing, and the CLI resolving the Workday baseline fixture while never opening a database).



M5.4.3 notes (D-065): the Lloyds baseline scan was complete (33 roles, 0 skipped) and produced 11 alert-eligible graduate schemes, but four more - `Manchester` x3 and `Halifax` - sat in `needs_review` because those town names also exist abroad. Sources that only advertise UK roles can now set `options.uk_employer = true`, which resolves those names as UK while an explicit foreign country or city still excludes. Applied to Barclays, Lloyds, Monzo, Wise and Arm; not to Palantir, whose Lever board advertises worldwide. Known limit: a string like `Manchester, NH` would now qualify on a UK-only board, because US state abbreviations are not modelled as countries; that shape was not observed on any configured board.

Related blind spot, still open: a `partial` scan neither alerts nor counts as a failure, so a source that returned partial every cycle (for example if Lloyds ever exceeded the 10 detail lookups) would look healthy in `status` and silently stop alerting.

M5.1 fixture caveat found during implementation: `tests/fixtures/palantir_jobs.json` is a **synthetic** trimming, not a faithful recording — 12 of 22 records have no `createdAt` and one carries the literal string `"not-a-timestamp"` (a deliberate "never guess a date" case). The fixture therefore serves as a key-shape baseline only; the type contract is enforced on live data. A live sample is what makes the baseline trustworthy.

### M6 — Self-host readiness

- Decide repository visibility and licence.
- Add packaging/quickstart and optional container support.
- Validate a fresh install without personal data or secrets.
- This is post-MVP and does not block the personal deployment.
