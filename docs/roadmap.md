# Job-Scout — Roadmap and M1 plan

**Status:** Approved; M1 delivered   
**Last updated:** 2026-10-22  
**Product authority:** [docs/prd.md](./prd.md)  
**Technical design:** [docs/architecture.md](./architecture.md)  
**Decision log:** [docs/decisions.md](./decisions.md)

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
| **M3** | Barclays and Lloyds Graduate added through Workday | M2 accepted; terms review; pagination/429 fixtures; cross-source dedupe |
| **M4** | Wise, Arm, and Monzo added; all six sources operational | M3 accepted; terms review; parser fixtures; all six acceptance checks |
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
| **M2.2** | Real Telegram transport with environment-variable credentials, redaction, and one test message | Not started: needs credentials and explicit approval to send |
| **M2.3** | Matching-filter overrides in configuration (FR-12 complete) | Not started |

M2.1 decisions: ambiguous outcomes are recorded and never retried automatically (D-024); transient failures stay `pending` for a later pass; permanent rejections become `failed`; `sent` rows are never re-sent (D-036); the transport and any CLI delivery switch arrive in M2.2 (D-037); proactive failure notices stay in M5 (D-038).

M2.1 acceptance evidence: `tests/test_notifier.py` (formatting, queueing, dedupe, restart, sent/failed/ambiguous/retry paths, redaction) and `tests/test_cli.py::test_status_reports_delivery_state_without_the_chat_id`.

### M3 — Workday: Barclays and Lloyds Graduate

- Add the Workday adapter only after M2.
- Validate pagination, 429 handling, relative dates, multi-location detail lookup, and partial-scan safety.
- Require terms review before recurring production use.
- Confirm expected counts with fixtures and a permitted live smoke test.

### M4 — Wise, Arm, and Monzo

- Add sitemap/HTML and Greenhouse adapters after M3.
- Validate sitemap caching, detail-page fetch rules, parser fixtures, and parse-failure reporting.
- Never request Wise query-string job pages or Arm search-results paths.
- Confirm all six sources and full acceptance matrix.

### M5 — Unattended operation and MVP completion

- Deploy only after M4 and source-permission review.
- Approve the retention policy.
- Run for seven days; verify restart recovery, failure notices, status, log redaction, backup/restore, and no silent gap longer than one scheduled cycle.
- MVP complete only when all six sources and the reliability gate pass.

### M6 — Self-host readiness

- Decide repository visibility and licence.
- Add packaging/quickstart and optional container support.
- Validate a fresh install without personal data or secrets.
- This is post-MVP and does not block the personal deployment.
