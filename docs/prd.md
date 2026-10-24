# Job-Scout — Product Requirements Document

- **Status:** Approved. A revision for **v0.5 (configurable scope)** was accepted on 2026-10-24; the changes are specified in [iterations/v0.5-configurable-scope.md](./iterations/v0.5-configurable-scope.md) and are applied to this document in increment 0.1
- **Last updated:** 2026-10-24
- **Owner:** Product Owner
- **Companion evidence:** [docs/sources.md](./sources.md)
- **Technical design:** [docs/architecture.md](./architecture.md)
- **Decision log:** [docs/decisions.md](./decisions.md)
- **Delivery plan:** [docs/roadmap.md](./roadmap.md)
- **Documentation map:** [docs/README.md](./README.md)
- **Authority:** This PRD is the product source of truth. Discovery evidence remains in `docs/sources.md`; technical design and delivery planning derive from this document.
- **Repository state:** see [iterations/v0.5-configurable-scope.md](./iterations/v0.5-configurable-scope.md) for the current state and this iteration's plan, and [docs/README.md](./README.md) for which document owns what. The release history is in [roadmap.md](./roadmap.md).

## 1. Product vision

Job-Scout is a personal UK job-monitoring service. It watches a curated set of official employer careers pages and applicant-tracking-system (ATS) job boards, detects newly published roles that match the user's early-career profile, and sends a concise Telegram alert with a direct link to the official listing.

The product is a **discovery aid**, not an application platform, career adviser, or job database. Its value is timely, relevant, low-noise alerts that help the user act on roles they would otherwise miss.

## 2. Target user

**Primary user:** one UK-based early-career job seeker — the product owner.

Profile assumptions:

- Background in mathematics, finance, data, and AI.
- Open to graduate, entry-level, and early-career roles across data/AI/software, quantitative/risk, general finance, consulting, and business analysis.
- Interested in London-based roles and UK remote/hybrid roles.
- Uses Telegram as the notification channel.
- Wants a small, low-maintenance personal tool rather than a multi-user service.

**Future direction, not MVP:** friends may clone the repository and run independent deployments. Multi-user accounts, shared infrastructure, and a hosted service are not in scope.

## 3. Problem and intended outcome

Relevant early-career roles are scattered across employer career sites and ATS boards. Checking them manually is repetitive, easy to forget, and poorly suited to a 30-minute monitoring rhythm.

The intended outcome is:

- The user does not need to check each employer's careers page manually every day.
- New matching roles arrive as clear, actionable Telegram messages.
- The same role is not repeatedly announced.
- The user can trust that a quiet period means no new matches, not a broken monitor.
- The user can open the official listing immediately and decide whether to apply.

## 4. MVP scope

### 4.1 Intended employer coverage

The MVP is intended to cover these six source entries:

| Employer | Source basis | Why it is in the MVP | Early-career evidence from discovery |
|---|---|---|---|
| **Wise** | Wise careers site / sitemap plus job pages | Strong graduate and internship volume; salary data | 7 graduate schemes and 2 internships; 195 London roles |
| **Palantir** | Palantir careers / Lever job board | Strong new-grad and internship pipeline; clean structured data | 306 roles; 45 London; many new-grad/intern roles |
| **Barclays** | Barclays careers / Workday | Large UK early-career pipeline | 94 results for a graduate search: 30 graduate, 34 intern, 19 apprentice, 11 permanent/regular |
| **Lloyds Graduate** | Lloyds graduate careers / Workday | Directly relevant graduate schemes in data/AI and software | 33 graduate schemes; 5 Data Science & AI, 7 Software Engineering |
| **Monzo** | Monzo careers / Greenhouse | Fintech coverage; internship roles | 73 roles; 55 London; 2 UK internships |
| **Arm** | Arm careers / sitemap plus job pages | Engineering and technology coverage; salary data | 426 roles; 97 UK-slug roles; 5 UK early-career roles |

These are **source entries**, not guarantees that a matching role is open at any particular time. Counts and titles change.

They are the **seeded starting set**, kept because each was researched and validated during discovery. The MVP coverage target is roughly 100 enabled employers (D-066), reached by adding configuration rather than code from v0.5 onwards; the six above are therefore no longer the definition of "MVP complete".

### 4.2 Release definitions

Releases are cumulative; each one is usable on its own and none reduces MVP scope.

| Release | Definition | Status |
|---|---|---|
| **v0.4 — personal beta** | Two sources (Lloyds Graduate, Monzo) scanning on a local schedule with Telegram delivery, an on-demand current-matches digest, and a read-only operating scorecard. Proves the end-to-end experience for one operator. | Delivered |
| **v0.5 — configurable scope** | Sources, role families, seniority, employment types, locations and exclusions are chosen in configuration rather than code; failure reports are visible and recoverable; documentation describes the product as it is. This is the release that makes a clone usable by someone else. | **Delivered 2026-10-24** (increments 0.1–0.4) |
| **v0.6 — scale-out machinery** | The engineering capability to reach the coverage target without a code change per employer: catalogue files, per-host pacing, compact status, and per-source coverage measurement. It does **not** add employers. | **Delivered 2026-10-24** |
| **MVP complete — coverage target** | Roughly 100 enabled employers, satisfying section 12. Removing or substituting a source requires an explicit Product Owner re-scope; the coverage target is a milestone, not a stopping point, because adding employers is ongoing operational work (D-066). | **Outstanding** — 17 catalogued, 14 enabled as of 2026-10-24 |

### 4.2a Deployment modes

The same product runs in two modes, sharing one matching engine, one set of adapters and one
database schema (D-091):

| Mode | Who uses it | How they configure it | Surface |
|---|---|---|---|
| **Local CLI** | The operator/maintainer, and anyone self-hosting from a clone | `config.toml`, catalogue files, a scheduled task | `jobscout …` commands |
| **Hosted service** | Invited users with no installation | A Telegram button wizard | A Telegram bot: alerts plus `/status`, `/pause`, `/digest`, `/delete` |

The hosted service does not replace the CLI: scanning, source validation, retention and reporting
stay operator commands, and a service deployment is operated through them. Inviting a user is a
deployment decision, not a claim that the MVP coverage or reliability gates have been met.

Delivered engineering capability and MVP completion are two different things, and the
distinction matters here: v0.5 and v0.6 were both delivered while the coverage target — the
thing that makes the product *minimally useful* — is still 14 employers into roughly 100.
Coverage progress is tracked by the report's per-source contribution counts, not by a release
name, and expansion is measured in batches rather than in milestones (D-066).

Iteration detail: v0.5's increments and acceptance criteria are in
[iterations/v0.5-configurable-scope.md](./iterations/v0.5-configurable-scope.md); the v0.6
capability and its exit criteria are in
[iterations/v0.6-scale-out.md](./iterations/v0.6-scale-out.md). Both files are historical
records and are not rewritten after delivery.

### 4.3 Exclusions

The MVP does not include:

- Saved jobs, application tracking, or interview tracking.
- AI ranking, semantic recommendations, or generated application content.
- A web or mobile application, **and no Telegram Mini App** for now: the bot's buttons must prove sufficient first (D-096).
- **Per-user custom sources**: a hosted user chooses from the operators's curated employers, because a source an operator has not vetted cannot be polled on someone else's behalf (D-094). Self-hosters keep full catalogue freedom.
- Job aggregators, LinkedIn, Indeed, or other third-party job databases.
- Browser automation, CAPTCHA bypass, login bypass, or evasion of bot protection.
- Automatic applications or recruiter contact.
- A user-facing source-health dashboard. Each user gets a one-line status; the detailed health view stays with the operator.
- Salary or visa-sponsorship filtering. Salary is displayed when available; sponsorship is recorded when exposed but does not filter alerts.
- Coverage of employers outside the intended set without Product Owner approval.
- **Batched alerting:** no digest of *new* alerts, no quiet hours, no daily roll-up. Both remain out of scope (D-070). An **on-demand** current-matches digest is in scope (`jobscout digest` in the CLI, `/digest` in the bot), because it reports roles already discovered rather than batching alerts, and it does not create notification rows or alter baseline state.

**Revised 2026-10-25 (D-091):** "multi-user accounts, subscriptions or shared deployments" was
previously excluded. A hosted, invited-user service is now in scope for the reasons in section
4.2a. Multi-user *accounts in the sense of passwords, profiles or roles* remain out of scope:
identity is the Telegram account (D-093).

## 5. Decision status

This section separates what is evidenced from what is approved or still unresolved.

### 5.1 Verified findings

Verified as of 2026-10-22; detailed evidence is in [docs/sources.md](./sources.md):

- All six researched sources are live-validated and running: Palantir (Lever, 309 roles), Lloyds Graduate (Workday, 33), Monzo (Greenhouse, 72), Barclays (Workday, 92 for a `graduate` search), Wise (sitemap/JSON-LD, 427) and Arm (sitemap/JSON-LD, 434). Two of the researched URL shapes were wrong: Wise uses `/job/` not `/jobs/`, and Arm carries two trailing numbers.
- Every usable source exposes a stable job identifier, enabling deduplication by source and source job ID.
- Early-career volume is concentrated in Wise, Palantir, Barclays, and Lloyds Graduate; Monzo and Arm provide smaller but relevant coverage.
- Field completeness is strong for title, employer, location, URL, and description; salary and closing-date coverage is uneven.
- Access constraints exist: Wise's query-string job pages and Arm's search-results path are disallowed by robots.txt; Workday endpoints carry a rate-limit risk; Palantir's Lever API requests a crawl delay.
- Several researched employers are not MVP-ready: Revolut and Skyscanner are blocked by bot protection; Faculty has no current openings; HSBC's UK coverage and feed completeness are unproven; Accenture's Workday endpoint returned HTTP 429; Anthropic, Bank of England, and Synthesia currently offer little early-career volume.

**Important distinction:** successful technical access is not permission for recurring automated access. The Product Owner has accepted this risk on a blanket personal-use basis (D-066); the earlier per-source review in [docs/source-access.md](./source-access.md) remains as evidence of what was found, but it is no longer a production gate.

### 5.2 Approved product decisions

| Decision | Status |
|---|---|
| One Telegram chat for the MVP | Approved |
| Coverage target of roughly 100 employers, seeded by Wise, Palantir, Barclays, Lloyds Graduate, Monzo and Arm; expansion is ongoing operational work (D-066) | Approved |
| Official employer careers pages and ATS endpoints only; no aggregators | Approved |
| Baseline: the first complete scan per source is silent; an optional current-matches summary is allowed; alerts are only for matching roles first observed in later complete scans | Approved |
| A role is new when first observed in a complete scan after that source's baseline scan has completed; a partial-scan observation does not make it alert-eligible | Approved |
| Immediate alert per new matching role; no batched digest of new alerts (an on-demand digest of current matches is allowed) | Approved |
| Alert fields: title, employer, location, salary if available, closing date if available, link | Approved |
| Target seniority: graduate, entry-level, early-career; approved seniority and unrelated-family exclusions | Approved |
| Location rule: London roles, UK-wide remote roles, and UK hybrid roles anywhere in the UK qualify; non-UK remote roles are excluded. The *scope* a user monitors is a preference ? pinned cities, the whole UK, or UK plus pinned cities abroad (D-069) ? while the shipped default stays the whole UK | Approved |
| Internships and industrial placements included only when explicitly labelled and within a target role family | Approved |
| Matching is deterministic and explainable; ambiguous cases are flagged for review rather than silently broadened | Approved |
| Target role families: data/AI/software, quantitative/risk, general finance, consulting, business analysis | Approved |
| Visa sponsorship is recorded when exposed but does not filter alerts | Approved |
| Updates to stored role details do not re-alert by default | Approved |
| A role is marked closed after two consecutive complete scans miss it; failed or partial scans never trigger closure | Approved |
| No quiet hours or hard daily alert cap initially | Approved |
| Notify after three consecutive source failures, without repeated failure-notice spam | Approved |
| Data minimisation: store only what is needed for deduplication, operational diagnosis, and notification history; a retention policy is required before unattended production | Approved |
| Personal, single-user MVP; friends may self-host later | Approved |

### 5.3 Outstanding decisions and unresolved defaults

These items are not yet approved and must not be treated as settled implementation requirements.

| Open item | Why it matters | Needed by |
|---|---|---|
| **Exact matching keyword and synonym list** | The product categories and exclusions are approved, but the exact terms remain an implementation default; they are now extended per clone through `[matching]` configuration rather than edited in code (D-026 stays open). | Ongoing tuning |
| **Alert volume shaping at ~100 sources** | Immediate per-role alerting is approved for two sources. At ~100 employers the volume may become unusable; re-open only when the observed volume justifies it (D-070). | When the observed daily volume justifies it, not before |

Resolved since the previous revision, recorded here so the section is not silently stale:

- **Ambiguous Telegram outcomes:** never retried; a `failed` send is retried up to three attempts per failure episode before the notice is abandoned (D-024, D-060). Implemented in v0.5 increment 0.1.
- **Retention periods:** ratified as implemented — closed-role content 30 days, closed identity 365 days, scans 90 days, delivery error text 90 days (D-067).
- **Repository visibility and licence:** private during the personal beta, MIT at the self-host release (D-071).
- **Access posture at scale:** blanket personal-use risk acceptance by the Product Owner (D-066). This is a risk decision, not a claim that a site's terms grant permission, and robots rules, rate limits and non-evasion remain engineering requirements.

### 5.4 Unresolved assumptions, risks, and dependencies

**Assumptions**

- A roughly 30-minute scan rhythm is useful and acceptable to the selected sources.
- Title-based matching can identify early-career roles with acceptable precision after tuning.
- The user will review early alerts and help tune the filters.
- Telegram is sufficient for delivery; no separate interface is needed.
- The selected sources remain structurally stable enough for maintainable integrations.

**Risks and dependencies**

- Terms-of-service review for the researched sources has not been completed; the Product Owner accepted this risk on a blanket personal-use basis instead (D-066).
- Workday rate limits and pagination may affect Barclays and Lloyds reliability.
- Wise and Arm depend on HTML page structure, which can change without notice.
- Source identifiers can change when an employer reposts a role; this may produce an alert for an existing job.
- Broad role-family matching may create noise; narrow matching may miss relevant roles.
- A silent baseline may make the bot appear inactive until a new role is published.
- Partial scans can resemble source shrinkage unless completeness is tracked explicitly.
- Telegram delivery can be ambiguous after a timeout; the policy is now decided and implemented: ambiguous outcomes are recorded and never retried, and a failed send retries at most three times per episode (D-024, D-060).
- Telegram token and chat ID must be kept out of the repository and logs.
- The continuous-running host, scheduling, storage, HTTP/parsing, and library choices are engineering decisions, not PRD decisions.
- Repository visibility and licensing for future self-hosting are decided: private during the personal beta, MIT at the self-host release (D-071).

## 6. Intended user journey

1. **One-time setup.** The user configures the enabled sources, the Telegram destination, and the role, seniority, employment-type and location preferences.
2. **Baseline.** On the first complete scan, the system records existing roles without sending individual alerts. The user may request a one-time summary of current matches.
3. **Ongoing monitoring.** The system checks the sources on a roughly 30-minute rhythm and compares each role's stable source ID with previously seen IDs. Only complete scans can make a newly discovered role alert-eligible.
4. **Matching.** New roles are evaluated against the agreed role families, seniority, and location rules. A role that does not match is stored but not announced.
5. **Alert.** A matching new role produces one concise Telegram message with the agreed fields and a link to the official listing.
6. **Action.** The user opens the official listing and decides whether to apply outside Job-Scout.
7. **Review and tune.** The user reviews alert relevance and adjusts filters or source coverage. A minimal status check shows whether each source is healthy.
8. **Failure visibility.** If a source fails repeatedly, the user receives a concise failure notice and can see the last successful scan.

## 7. Functional requirements

| ID | Requirement |
|---|---|
| **FR-1 Source coverage** | The system must monitor every configured source, must reach the coverage target of roughly 100 enabled employers, and must not silently omit a configured source. Sources are added through configuration, not code, from v0.5 onwards. |
| **FR-2 Role representation** | Each role must retain its source, stable source job ID, employer, title, raw and normalized location, official URL, description, and, where available, posted date, closing date, salary, and remote/hybrid status. |
| **FR-3 New-role detection** | A role is new only when it is first observed in a complete scan after that source's baseline scan has completed. A role first observed only in a partial scan is not alert-eligible until a complete scan observes it. |
| **FR-4 Matching** | The system must apply the approved role-family, seniority, internship, and location rules and must explain why a role was included, excluded, or flagged as ambiguous. Ambiguous cases are not silently broadened into matches. |
| **FR-5 First-run behaviour** | The first complete scan of a source must establish a silent baseline without individual alerts for existing roles. A partial or failed scan must not establish or complete a baseline. An optional one-time current-matches summary may be requested. |
| **FR-6 Deduplication** | The system must send at most one alert per source job ID per Telegram chat in normal operation and must not create duplicates in deterministic retry and restart paths. An ambiguous Telegram outcome is recorded and never retried; a failed send is retried at most three times per failure episode; exactly-once external delivery is not guaranteed. |
| **FR-7 Notification content** | Each alert must contain title, employer, location, salary if available, closing date if available, and a link to the official listing. Missing optional fields must be omitted cleanly. |
| **FR-8 Notification timing** | A matching new role must be delivered as soon as practical after detection, without waiting for a digest. If delivery pacing is required, alerts must not be dropped. |
| **FR-9 Failure isolation** | A failure in one source must not prevent the other sources from being checked. Retries must not create duplicate alerts or overwhelm the source. |
| **FR-10 Status visibility** | The user must be able to check the last successful scan, last scan completeness, last error, consecutive failures, and recent alert activity per source. This does not require a dashboard. |
| **FR-11 Persistence** | Seen source IDs, baseline state, scan history, notification history, and source status must survive restarts. |
| **FR-12 Configuration** | A clone must be able to choose its sources, role families, seniority, employment types, locations and exclusions through configuration rather than code. Only adding a structurally new *kind* of source may still require an adapter. |
| **FR-13 Closure handling** | A role absent from two consecutive complete scans must be marked closed. Failed or partial scans never increment closure misses. Closure alerts are not required in the MVP. |
| **FR-14 Security and compliance** | Secrets must not enter source control or logs. The system must respect source access restrictions and must not bypass CAPTCHAs, authentication, or bot protection. |
| **FR-15 Auditability** | The system should retain enough match/decision context to review why a role was or was not alerted. This supports tuning and debugging without building a dashboard. |
| **FR-16 Scan completeness** | Every source scan must report success, empty success, partial, or failure. Only a complete scan may establish baseline, make a newly discovered role alert-eligible, or count toward closure. |
| **FR-17 Service identity** | In service mode a user is identified by their Telegram account in a private chat. First contact creates them; no password, email or name is collected. Group and channel chats are refused. |
| **FR-18 Self-service configuration** | A user must be able to choose role families, employment types, location scope and employers entirely through chat, review the result, and change it later, without installing anything or contacting the operator. |
| **FR-19 Shared scanning, per-user decisions** | Each source must be scanned once per cadence regardless of how many users subscribe, and each user's own rules must decide what they are told. A user must never receive another user's decisions, preferences or notifications. |
| **FR-20 Subscription semantics** | Subscribing or changing preferences must not alert roles that already existed. Alerts apply to roles first observed in a complete scan after the user's subscription watermark; roles already open are available through an on-demand digest. |
| **FR-21 User control** | A user must be able to check whether monitoring is working, pause and resume it, request the current matches, and delete their account and data without operator involvement. |
| **FR-22 Operator control** | The operator must be able to choose which employers the hosted service offers, scan and validate sources, inspect per-user notification history, and delete a user on request. |

## 8. Matching and location rules

### 8.1 Role families

**Agreed categories:**

- Data science and analytics
- AI and machine learning
- Software engineering and development
- Quantitative and risk analysis
- General finance analysis
- Consulting
- Business analysis and data-adjacent analyst roles

**Approved inclusion signals:** graduate, entry-level, early-career, junior, explicitly labelled intern/industrial placement, analyst without a senior qualifier, and associate only when the role is explicitly early-career.

**Approved exclusions:** senior, lead, principal, staff, head, director, vice president, and manager roles unless they are explicitly part of a graduate programme; sales, marketing, human resources, legal, and pure accounting/audit roles outside the target families; roles requiring a PhD unless explicitly early-career.

The exact keyword and synonym list is an implementation default, reviewed against live roles and extendable per clone through `[matching]` configuration. Changes to that list must not expand the approved categories or remove the approved exclusions.

### 8.2 Location rules

**Approved location rules:**

- Include a role if any listed location is London.
- Include UK-wide remote roles.
- Include UK hybrid roles anywhere in the UK.
- Include a multi-location role if at least one listed location qualifies.
- Exclude non-UK remote roles and roles whose only locations are outside the UK.
- Treat "remote-friendly" as insufficient unless UK eligibility is explicit.
- Do not guess ambiguous locations. Record the reason the role could not be matched; it is not alert-eligible until the location can be resolved.

**Scope on top of those rules:** a user narrows the rules above with
`[preferences.locations]` ? `uk` (the default, which is exactly the rules above), `pinned` (only
the listed cities, plus UK remote/hybrid when enabled), or `uk_plus_pinned` (the rules above plus
pinned cities abroad). A narrowed scope produces explicit exclusions, never silence, and pinning
does not override the ambiguity rule. See [configuration.md](./configuration.md).

### 8.3 Other matching inputs

- **Visa sponsorship:** record when exposed; do not filter.
- **Salary:** display when available; do not filter.
- **Closing date:** display when available; do not filter.
- **Description:** use only for explicit early-career or sponsorship signals; do not use open-ended semantic ranking.
- **Ambiguous titles or locations:** flag the role for review and keep it out of alerts until the ambiguity is resolved; never silently broaden a match.
- **Internships:** include explicitly labelled internships and industrial placements only when they fall within a target role family.

## 9. First-run and deduplication behaviour

### 9.1 First run

- The first complete scan of each source establishes a silent baseline.
- Roles found during baseline are stored but do not produce individual alerts.
- A role first observed in a later complete scan is alert-eligible if it matches; a role first observed only in a partial scan is not alert-eligible until a complete scan observes it.
- The user may request a one-time summary of current matches.
- If a source scan is partial or failed, the baseline is not marked complete and the source is retried.
- An empty success is a valid complete scan, not a failure.
- A source that cannot be baselined must be visible in the status check rather than silently treated as empty.

### 9.2 Deduplication

- The primary key is `(source, stable_source_job_id)`.
- The notification log records delivery outcomes per Telegram chat: pending, sent, ambiguous, or failed.
- Deterministic retries and restarts must not create duplicate alerts. Ambiguous external delivery outcomes are recorded and handled under the approved notification policy; exactly-once delivery is not promised.
- A role that disappears and later reappears with the same source ID is not re-announced by default.
- A repost with a new source ID may be announced as new. This is an accepted MVP limitation and should be monitored for duplicate-like noise.
- Cross-source duplicates are not expected across the seeded sources, but if the same employer appears through two sources, a duplicate heuristic may be needed later.

### 9.3 Updates, closures, and scan completeness

- Updates to title, location, salary, closing date, or description are stored; they do not trigger a new alert by default.
- A role is marked closed only after two consecutive complete scans miss it.
- A failed or partial scan never increments the miss count and never causes closure.
- A scan is complete only when the adapter can establish that the expected full result set was retrieved and parsed. Partial scans may update observed records but cannot establish baseline, make a role alert-eligible, or close roles.

## 10. Notification expectations

- **Channel:** one Telegram chat.
- **Cadence:** immediate message per new matching role; no digest.
- **Content:** title, employer, location, salary if available, closing date if available, and official link.
- **Format:** concise and scannable; optional fields omitted rather than shown as empty labels.
- **Link:** must open the official employer or ATS listing, not an aggregator or tracked redirect.
- **Quiet hours:** none; approved for the MVP.
- **Volume:** no hard cap initially; approved. Monitor and tune after the first two weeks.
- **Alert eligibility:** only matching roles first observed in a complete scan after baseline are alerted.
- **Failure notice:** one concise message after three consecutive failed scans of a source; no repeated failure spam until recovery. A recovery notice is optional.
- **Shared connectivity outage:** when several sources fail because the connection never opened, they produce one aggregated notice rather than one each, with a two-hour quiet window for further sources caught by the same outage (D-086). Timeouts and per-source errors keep their own notice. Individual source health in `status` and the report is unaffected.
- **Delivery self-check:** `jobscout notify-test` proves the configured Telegram path with one labelled message and records the result outside the alert history (D-088). It does not substitute for a real alert.
- **Journey rehearsal:** `jobscout rehearsal` runs detection, matching, queueing, formatting, delivery and recording against synthetic records in a throwaway database (D-089). It is a pre-flight check for a new deployment, not evidence for AC-3: only a genuinely new matching role from a live source satisfies that.
- **No re-alerts:** updates, salary changes, description changes, and closures do not trigger alerts by default.

## 11. Operational, security, and compliance requirements

- **Single-user operation:** one Telegram chat; no multi-user account model in the MVP.
- **Continuous operation:** the service should run continuously and check sources on a roughly 30-minute rhythm. Delivery is best-effort, not a real-time guarantee.
- **Source access:** use official employer or ATS endpoints only. Do not use aggregators or third-party job databases.
- **Permission versus access:** technical accessibility does not establish permission for recurring automated access. The Product Owner has accepted this risk on a blanket personal-use basis for this single-user deployment (D-066); each deployed source records its access basis in its catalogue entry (`access_note`, D-087), and robots rules, rate limits and non-evasion remain engineering requirements.
- **Source-specific constraints:** Wise query-string job pages and Arm search-results paths must not be crawled; Palantir's crawl delay must be respected; Workday rate limits must be respected.
- **Secrets:** the Telegram token and chat ID must be stored outside source control and must not appear in logs or error messages.
- **Data minimisation:** store only what is needed for job state, deduplication, notification history, and basic source status. Do not collect CVs, application data, or unrelated personal information.
- **Retention:** the implemented horizons are ratified — closed-role content cleared 30 days after closure, closed identity deleted 365 days after closure, scans 90 days, delivery error text 90 days (D-025, D-067). Pruning is an explicit operator command, never automatic.
- **Failure isolation:** one source failure must not stop the others; the status check must make the failure visible.
- **Restart safety:** persisted state must prevent duplicate alerts in deterministic restart paths; ambiguous Telegram outcomes must be recorded and handled under the approved notification policy.
- **Least exposure:** no public administrative interface is required for the MVP; inbound exposure should be minimised.
- **Future self-hosting:** repository visibility and licensing must be decided before a public clone-and-run release, but do not block the personal MVP.

## 12. Acceptance criteria

### 12.1 First usable version (Palantir + Telegram)

| ID | Criterion |
|---|---|
| **AC-1** | A Palantir scan returns normalized roles or explicit skip reasons, and reports whether the scan was complete, partial, empty-success, or failed. |
| **AC-2** | The first complete scan establishes a baseline without individual alerts for existing roles; the optional current-matches summary lists baseline matches when requested. |
| **AC-3** | A matching Palantir role first observed in a complete scan after baseline produces exactly one Telegram alert. |
| **AC-4** | Re-running the scan does not re-alert the same role ID. |
| **AC-5** | An update to an existing role does not produce a new alert by default. |
| **AC-6** | A positive/negative/ambiguous matching test set produces the expected include, exclude, and flagged-for-review results; ambiguous cases are not silently broadened into matches. |
| **AC-7** | London, UK-remote, UK-hybrid, non-UK, multi-location, and ambiguous-location cases produce the expected decisions; ambiguous locations are recorded with a reason and are not alerted. |
| **AC-8** | An alert contains the agreed fields, omits missing optional fields cleanly, and links to the official listing. |
| **AC-9** | A simulated partial or failed source scan does not stop other sources, does not establish or complete a baseline, does not close roles, and does not duplicate an alert on retry. |
| **AC-10** | Restarting the service does not duplicate previously sent alerts in deterministic restart paths. |
| **AC-11** | No Telegram token or chat ID appears in the repository or logs. |
| **AC-12** | A matching new role triggers a Telegram alert after scan completion, without waiting for a digest. Proposed latency thresholds are in section 13. |

### 12.2 MVP complete (coverage target reached)

| ID | Criterion |
|---|---|
| **AC-13** | Every source enabled in configuration is configured, enabled, and completes at least one complete scan; the six researched sources all count towards the coverage target, which is reached at roughly 100 enabled employers (D-066). |
| **AC-14** | No enabled source is silently omitted; a source that is disabled or failing is visible in `jobscout status` and in the operating report, and adding coverage never breaks an existing source. |
| **AC-15** | For each source, the scanner reports records seen and parsed and either matches the source-reported total or sitemap count, or records an explicit discrepancy reason; fixture counts are exact. |
| **AC-16** | A cross-source fixture matrix shows matching and location rules applied consistently across adapters. |
| **AC-17** | No duplicate alerts occur in deterministic retry/restart and cross-source fixture tests; an ambiguous Telegram outcome is recorded and never retried, and a failed one is retried at most three times per failure episode (D-024, D-060). |
| **AC-18** | A source failure is isolated, visible in status, and does not create duplicate alerts. |
| **AC-19** | Each deployed source records an access basis: its public endpoint, the engineering hygiene applied (robots rules, rate limits, no evasion), and the Product Owner's blanket personal-use risk acceptance (D-066). This is recorded rather than reviewed per source, and it does not assert that any site's terms grant permission. |
| **AC-20** | The service completes a seven-day unattended run and the operating report states measured cycle coverage, recording the hours the host was actually available — host downtime is reported separately from scheduler failure, and the reliability target in section 13 is not claimed unless it is measured. |
| **AC-21** | The user can inspect last successful scan, last scan completeness, last error, consecutive failures, and recent alert activity per source. |
| **AC-22** | A role first observed only in a partial scan is not alerted until a complete scan sees it. |
| **AC-23** | A partial or failed scan never increments closure misses or marks a role closed. |
| **AC-24** | A valid empty source response is recorded as empty success, not failure, and does not by itself close existing roles. |

### 12.3 Hosted service (D-091…D-096)

| ID | Criterion |
|---|---|
| **AC-25** | A new Telegram user completes `/start` → preferences → confirmation in one conversation, with no operator involvement, and ends with a saved scope and an explicit statement of what will happen next. |
| **AC-26** | A saved scope sets the subscription watermark to the newest stored role, so nothing already open is announced as new; `/digest` lists the roles already matching under that scope. |
| **AC-27** | Two users with overlapping and disjoint scopes receive only their own decisions: fixtures assert that neither is notified for the other's matches and that neither can read the other's preferences or notification history. |
| **AC-28** | Scanning one source once serves every subscriber of it: with N users on one employer the pass makes one set of source requests, and each user's alert count is independent of N. |
| **AC-29** | `/pause` stops alerts without losing the subscription or duplicate-alerting on resume; `/delete` removes the user's preferences, subscriptions, notifications and session, and nothing of theirs remains. |
| **AC-30** | A non-private chat is refused with an explanation and no user record is created. |

## 13. Proposed success measures
These are **proposals for approval**, not commitments.

| Measure | Proposed target | How it would be measured |
|---|---|---|
| Matching precision | ≥70% of alerts judged relevant over the first two weeks | Weekly manual review of the alert log |
| Matching recall | ≥90% on a weekly sample of 20 roles that meet the agreed rules | Manual comparison of source roles with alerts |
| Duplicate alerts | 0 in normal operation and deterministic retry/restart tests; ambiguous outcomes tracked separately | Notification log |
| Alert delivery latency | Median under 2 minutes from scan completion; p95 under 5 minutes | Timestamped logs |
| Scan rhythm | At most 30 minutes plus jitter | Scheduler/status log |
| Reliability at MVP complete | ≥95% of scheduled scans successful; no silent gap longer than one cycle | Seven-day unattended run |
| Alert volume | Within a Product Owner-agreed daily range | Daily count; tune after week one |
| Time to first relevant alert | Under 24 hours after deployment | First alert timestamp and user review |
| Directional outcome | At least one application or interview traceable to an alert within four weeks | Self-reported |

Service-mode measures (they need the hosted deployment, and are only measurable there):

| Measure | Proposed target | How it would be measured |
|---|---|---|
| Setup completion unaided | ≥80% of invited users finish `/start` and confirm a scope without contacting the operator | User records with saved preferences; operator's message log |
| Time to a configured scope | Under 5 minutes from first contact | `created_at` to preferences `updated_at` |
| Alert relevance | ≥70% positive on a one-tap 👍/👎 reply added when the pilot needs it | Replies stored against the notification |
| 14-day retention | ≥50% of invited users still unpaused after two weeks | `paused` and last-interaction timestamps |
| Operator support load | Under one message per user per week | Operator's own count |

The number of sources, number of stored jobs, and number of messages sent are **not** success measures by themselves.

## 14. Risks, dependencies, and open questions

### 14.1 Material risks

| Risk | Impact | Current position |
|---|---|---|
| Terms of service do not permit recurring automated access | A source may need to be removed or accessed differently | Accepted as a personal-use risk by the Product Owner (D-066); not a claim that any site grants permission |
| Workday rate limits or pagination changes | Barclays/Lloyds scans may be incomplete or fail | Both validated live on 2026-10-24; 429 handling implemented; Barclays is narrowed by its own `graduate` search (D-080) |
| Wise or Arm page structure changes | Parsing may silently miss or misread roles | HTML-based access; needs fixtures and failure detection |
| Matching is too broad or too narrow | Noise or missed roles undermines the product | Dry-run samples reviewed; tuning continues as observations arrive |
| First-run baseline is misunderstood | User may think the bot is broken or receive a flood | Approved silent baseline plus an on-demand current-matches digest |
| Telegram delivery ambiguity | A timeout can cause a rare duplicate or missed alert | Ambiguous outcomes are recorded and never retried; failed sends retry up to three times (D-024, D-060) |
| Partial scan mistaken for source shrinkage | Roles could be incorrectly closed | Only complete scans count toward closure; partial and failed scans never do |
| Telegram token leakage | Security and privacy incident | Secrets must stay out of source control and logs |
| Host or scheduler failure | Alerts stop silently | Failure notices, `jobscout status` and the operating report; host sleep still reduces cycle coverage |
| Source ID changes on repost | Duplicate-like alerts | Accepted MVP limitation; monitor |
| Broad scope creep | Delays a usable personal MVP | Configuration over code; no multi-user, AI ranking, or dashboard |

### 14.2 Dependencies

- Telegram bot token and chat ID: in place for the personal beta.
- A recorded access basis per deployed source (endpoint, hygiene, risk acceptance); no per-source permission gate (D-066).
- The notification delivery policy for ambiguous Telegram outcomes: decided and implemented (D-024, D-060).
- The retention policy: ratified as implemented (D-067).
- A continuously available host and a reliable way to run the service unattended: a local Windows scheduled task for the personal beta only.
- A persistent store for seen roles, baseline/scan state, and notification history.
- Configurable matching terms, so a clone can change scope without editing code (v0.5).

### 14.3 Open questions

1. At roughly 100 sources, does immediate per-role alerting still serve the user, or is batching needed (D-070)?
2. Which exact keyword and synonym list should become the shipped default for a new clone, as distinct from this operator's preferences?
3. When should the repository become public and MIT-licensed for self-hosting (D-071)?
4. How much per-source access evidence is worth keeping now that access is blanket-accepted (D-066)?

## 15. Technical considerations for engineering planning

The approved architecture direction is recorded in [docs/architecture.md](./architecture.md), with decisions in [docs/decisions.md](./decisions.md) and delivery gates in [docs/roadmap.md](./roadmap.md). This PRD remains the product authority.

- Source access is heterogeneous: structured JSON, Workday APIs, and sitemap-plus-HTML pages. Engineering must account for different failure modes and field completeness.
- Stable source IDs are available, but some sources expose only relative posted dates or omit salary and closing dates.
- Workday rate limiting, pagination, and HTML drift are material reliability risks.
- The product requires persistent scan/job state and a notification log that survives restarts.
- The product requires source isolation, bounded retries, and a user-checkable status view.
- The product requires secure handling of the Telegram token and chat ID.
- Matching and location normalisation need a reviewable ruleset and fixture-based tests before source expansion.
- Telegram delivery ambiguity must be handled explicitly; exactly-once external delivery is not assumed.
- Implementation status, current scope and the next increment are tracked in [docs/roadmap.md](./roadmap.md) and [docs/iterations/](./iterations); this section lists constraints, not a delivery plan.
