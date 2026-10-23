# Job-Scout — Decision log

**Status:** Living record; approved decisions and open items  
**Last updated:** 2026-10-22  
**Product authority:** [docs/prd.md](./prd.md)  
**Technical design:** [docs/architecture.md](./architecture.md)

This is a concise decision log, not a duplicate of the PRD. Status values:

- **Approved** — accepted by the Product Owner or adopted as the approved architecture direction.
- **Pending** — needs Product Owner approval before the stated milestone.
- **Deferred** — intentionally postponed; not required for M1.
- **Open** — unresolved validation or compliance question.

## Product decisions

| ID | Decision | Status | Rationale / implications |
|---|---|---|---|
| D-001 | Single-user MVP with one Telegram chat | Approved | Keeps the product small and avoids account/subscription complexity. Future friends self-host independently. |
| D-002 | Six-employer MVP coverage: Wise, Palantir, Barclays, Lloyds Graduate, Monzo, Arm | Approved | Chosen for early-career volume, access diversity, and UK coverage. Palantir-first delivery does not reduce this scope. |
| D-003 | Palantir + Telegram is the first usable version, not the completed MVP | Approved | Proves the user experience early while preserving the six-source completion target. |
| D-004 | First complete scan per source is a silent baseline; optional current-matches summary; alert only on roles first observed in later complete scans | Approved | Avoids a launch flood and makes “new since baseline” testable. |
| D-005 | New role means first observed in a complete scan after the source's baseline | Approved | Resolves the FR-3/FR-5 contradiction. A role first observed only in a partial scan is not alert-eligible until a complete scan observes it. |
| D-006 | Location: London roles, UK-wide remote roles, and UK hybrid roles anywhere in the UK; non-UK remote excluded | Approved | Matches the user's stated geography without guessing ambiguous locations. |
| D-007 | Internships and industrial placements are included only when explicitly labelled and within a target role family | Approved | Keeps early-career coverage without conflating internships with graduate schemes. |
| D-008 | Matching is deterministic and explainable; ambiguous cases are flagged for review, not silently broadened | Approved | Supports tuning and avoids opaque ranking in a personal MVP. |
| D-009 | Updates do not re-alert; a role closes after two consecutive complete scans miss it; partial/failed scans never close roles | Approved | Prevents duplicate alerts and false closures from incomplete scans. |
| D-010 | No quiet hours or hard daily alert cap initially; failure notice after three consecutive source failures without repeated spam | Approved | Keeps the first release simple; volume and alerting behaviour will be reviewed with real data. |
| D-011 | Data minimisation: store only what is needed for deduplication, operational diagnosis, and notification history; retention policy required before unattended production | Approved | Limits personal-data and operational risk without over-specifying storage now. |
| D-012 | Official employer/ATS sources only; no aggregators, CAPTCHA bypass, or bot-protection evasion | Approved | Preserves source legitimacy and avoids fragile or disallowed access. |

## Technical decisions

| ID | Decision | Status | Rationale / implications |
|---|---|---|---|
| D-013 | Python 3.12+, modular monolith, one process, CLI, SQLite, source-specific adapters, deterministic matcher, fixture-based tests | Approved architecture direction | Proportionate to a single-user, low-volume service. No microservices, queue, ORM, web UI, or AI ranking. |
| D-014 | Scan result statuses are `success`, `success_empty`, `partial`, and `failed`; only complete scans establish baseline, make roles alert-eligible, or count toward closure | Approved technical design | Makes partial versus complete explicit and prevents incomplete data from causing false alerts or closures. |
| D-015 | Job identity is `(source_id, source_job_id)`; baseline uses `baseline_scan_id`; alert eligibility uses `first_complete_scan_id` after baseline | Approved technical design | Resolves baseline/new-role ambiguity and supports restart-safe dedupe. |
| D-016 | Adapters own HTTP, parsing, source-specific mapping, and completeness; they do not write to SQLite or notify | Approved technical design | Keeps source fragility isolated and the matcher independent of source quirks. |
| D-017 | Matcher is a pure, versioned rule function returning `match`, `no_match`, or `needs_review` with reasons | Approved technical design | Explainable, testable, and tunable without changing source adapters. |
| D-018 | M1 is local and Palantir-only, with persistence and dry-run; Telegram, scheduling, deployment, and other adapters are excluded | Approved milestone scope | Proves the core discovery/matching workflow before adding delivery and operational complexity. |
| D-019 | Telegram delivery is M2; scheduling and unattended deployment are M5 | Approved sequencing | Delivery and scheduling are separate failure domains from discovery and matching. |
| D-020 | Notifications use a persisted outbox with `pending`, `sent`, `ambiguous`, and `failed` states; sent notifications are never re-sent after restart | Approved technical direction | Provides restart safety and a place to handle ambiguous external delivery. |
| D-021 | No raw HTML or full source payloads stored by default; fixtures hold parser samples | Approved technical direction | Supports data minimisation and avoids duplicating public web content in the database. |
| D-022 | SQLite is the only durable state; no ORM; migrations use a simple ordered runner | Approved technical direction | Single writer, low volume, and transactional dedupe are sufficient. |
| D-023 | Source-specific parsers, not a generic CSS-selector crawler or plugin framework | Approved technical direction | Wise and Arm are structurally different; hidden generic abstractions would obscure failures. |

## Open and deferred decisions

| ID | Decision | Status | Rationale / next action |
|---|---|---|---|
| D-024 | Notification policy for ambiguous Telegram outcomes: retry (rare duplicates possible) versus do not retry (rare missed alerts possible) | Approved (M2.1): record `ambiguous`, never retry automatically | Telegram has no idempotency key; exactly-once external delivery is impossible. The user accepted the risk of a rare missed alert in exchange for never duplicating a role. Revisit only with evidence. |
| D-025 | Retention period for job descriptions, jobs, scans, notifications, and logs | Pending | Data minimisation is approved; the concrete period is not. Architecture contains a proposed starting point; approve before M5. |
| D-026 | Exact matching keyword and synonym list | Pending review in M1 | Product categories and exclusions are approved; exact terms are an implementation default to validate against the Palantir dry-run sample. |
| D-027 | Source permission for recurring automated access | Open | Technical access is not permission. Terms review is required per source before recurring production polling; no source is currently approved for that use. |
| D-028 | Repository visibility, licence, and self-host packaging | Deferred to M6 | Personal MVP unaffected. Decide before a public clone-and-run release. |
| D-029 | Historical match-decision storage | Deferred | Store the latest decision on the job for M1. Add history only if tuning evidence requires it. |
| D-030 | Multi-user accounts, AI ranking, dashboard, saved jobs, application tracking | Deferred / out of scope | No evidence of need for this MVP. |
| D-031 | M1 implementation uses a `src/` layout, a standard-library-only runtime, and `pytest`/`ruff` as dev-only tools | Implemented (M1) | Keeps the self-host footprint small and the dependency surface minimal; no HTTP, parsing, or CLI library was needed. |
| D-032 | Matching keywords live in code behind a version string (`matching.RULES_VERSION`), and the version is stored with every match decision | Implemented (M1) | Keeps matching explainable and reproducible; keyword tuning bumps the version. This fixes the versioning mechanism only: the terms themselves still need Product Owner review (D-026), and keyword configurability is D-035, not resolved here. |
| D-033 | Any unmappable record makes the scan `partial`, not complete | Implemented (M1) | Follows architecture 3.1 and guarantees no silent drops or false closures. Known limitation: a persistently malformed record prevents baseline/alerting; it is visible in `status` and dry-run output. Revisit if live data shows this pattern. |
| D-034 | A suspicious empty response (source previously returned roles, now zero) is downgraded to `partial` unless `--accept-empty` is passed | Implemented (M1) | Prevents an upstream outage from looking like mass closure. The override keeps a genuine source shutdown workable without mass false closures. |
| D-035 | PRD FR-12 (config-driven sources, filters, and Telegram destination) is completed in M2; M1 deliberately leaves filter terms in code | Implemented (M2.3) | No M1 acceptance criterion requires configurable filters (M1-A14 assumes no configuration). Sources, `enabled`, and cadence are already configuration. Bundling filter overrides with the M2 Telegram destination keeps one config surface, and overrides may add terms but never remove approved categories or exclusions. |
| D-036 | Delivery state machine: `pending` → `sent` \| `failed` \| `ambiguous`; transient failures (429/5xx/network) retry a bounded number of times inside a pass and stay `pending` for the next pass; permanent 4xx becomes `failed`; `sent` rows are never re-sent | Implemented (M2.1) | Gives restart-safe dedupe (`UNIQUE(chat_id, job_id)`) without an external queue. Transient errors are not dropped, and terminal states are never re-attempted, so retries cannot duplicate. |
| D-037 | M2.1 is a library-level pipeline only; the Telegram transport, its environment-variable credentials, and any CLI delivery switch arrive in M2.2 | Implemented (M2.1) | Keeps M2.1 free of network access and secrets while making formatting, persistence, and duplicate prevention testable. Chat ids and bot tokens are always redacted from stored error text. |
| D-038 | Proactive "three consecutive failed scans" notices remain in M5; M2 exposes delivery and scan failures through `status` only | Approved (M2.1) | Avoids a notification channel that could itself be failing. Keeps proactive alerting in the milestone that runs unattended and can validate it. |
| D-039 | Each Telegram alert is one POST with no internal HTTP retry: timeouts after a send are `ambiguous`, connection failures and 429/5xx are `retryable`, other 4xx and `ok:false` are `permanent` | Implemented (M2.2) | A retried POST could deliver twice, so the delivery state machine is the only retry authority. Timeouts are the one case where the outcome is unknowable, so they are never retried (D-024). |
| D-040 | Telegram credentials come only from `JOBSCOUT_TELEGRAM_TOKEN` and `JOBSCOUT_TELEGRAM_CHAT_ID` (read from `.env`, see D-043); the token is shape-checked, never echoed, and transport errors are scrubbed of token and chat id before storage or display | Implemented (M2.2) | Keeps secrets out of the repository, logs, stored `last_error`, and CLI output. Shape checking gives an actionable error instead of a confusing Telegram 404. |
| D-041 | Delivery is explicit: `scan --deliver` queues and sends, `deliver` retries already-queued alerts without scanning, and `--dry-run`/`--deliver` are mutually exclusive | Implemented (M2.2) | Sending is never accidental, and transient failures can be retried without requesting source data again. |
| D-042 | The crash window between Telegram accepting a message and recording `sent` is accepted for the MVP; such a row can be resent | Accepted (Product Owner) | Closing it needs a pre-send marker plus stale-state recovery on startup. Not worth the complexity now; recorded here as a known duplicate risk. |
| D-043 | Telegram credentials load from a local `.env` file using a small built-in parser; real environment variables take precedence; `.env` is gitignored and `.env.example` holds placeholders | Implemented | Removes manual shell setup without adding `python-dotenv` for two keys. Precedence keeps host/CI secret injection working, and `.env.example` documents the values. Redaction and token-shape validation are unchanged, and the loader never logs values. |
| D-044 | Filter overrides are additive only: extra title phrases per approved role family plus extra title exclusions, declared in `[matching]` in `config.toml`. Unknown families and unknown keys are rejected, and the rules version carries a digest of the overrides | Implemented (M2.3) | Satisfies FR-12 without letting configuration invent categories or cancel approved exclusions (PRD §8.1). Seniority and location rules are untouched, exclusions are evaluated before matches, and the digest keeps every stored decision traceable to the exact ruleset. |
| D-045 | One `WorkdayAdapter` serves Barclays and Lloyds Graduate: job identity is the requisition id (`bulletFields[0]`, falling back to `jobRequisitionId`/`id`), completeness comes from the reported `total`, a detail page is fetched only for placeholder locations and is capped per scan, a detail response resolves the placeholder only when it supplies a usable non-placeholder location, and only explicit relative dates ("today", "yesterday", "N days ago") are interpreted | Implemented (M3, synthetic fixtures only) | Both employers run the same ATS, so two adapters would duplicate parsing and drift apart. Requisition ids are stable across reposts, unlike titles; the `total` comparison is the only completeness signal that needs no extra request. A detail response that is malformed or carries no usable location leaves the placeholder recorded and makes the scan `partial`, so a role with an unknown location can never be treated as fully scanned. Bucket labels such as "30+ days ago" stay `None` rather than being guessed, so posted dates are never fabricated. Nothing here is validated against a real payload yet (see D-048). |
| D-046 | The Workday listing POST opts in to the client's bounded retry (`retry=True`); Telegram sends keep the no-retry default | Implemented (M3) | The listing call is a read-only query with no side effect, so a repeat cannot duplicate anything, while a transient 429 would otherwise turn every Workday scan into a failure. The retry authority for sends is still the delivery state machine (D-039). |
| D-047 | The architecture's Workday sitemap fallback is not implemented in M3; completeness relies on the reported `total` and on detecting a short or empty page before that total | Deferred (M3 deviation) | A sitemap fallback cannot be designed or tested without a real response, and adding it speculatively would create an untested second code path. Revisit only if permitted live validation shows `total` is unreliable or truncated listings are common. |
| D-048 | M3 ships with synthetic fixtures: response shapes, the requisition-id field, the detail endpoint, the locale-prefixed public URL, and relative-date wording are all assumptions pending permitted live validation | Open | Guarded disclosure of what the tests do and do not prove. Fixtures verify parsing, pagination, failure handling, and scan-safety logic, not that the real service returns those shapes. Also unresolved: whether Barclays and Lloyds permit recurring automated access at all (D-027). |

## Change rule

- Product changes require a PRD update and Product Owner approval.
- Technical changes that alter contracts, baseline/closure semantics, notification guarantees, or source coverage require an entry in this log.
- Implementation details that do not change these decisions do not need a new entry.
