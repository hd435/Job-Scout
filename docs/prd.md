# Job-Scout — Product Requirements Document

**Status:** Draft for product-owner approval  
**Last updated:** 2026-10-22  
**Owner:** Product Owner  
**Companion evidence:** [docs/sources.md](./sources.md)  
**Authority:** This PRD is the product source of truth. Discovery evidence remains in `docs/sources.md`; implementation choices remain for engineering planning.  
**Repository state:** discovery documentation only; no application code exists as of this draft.

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

- The user does not need to check the six selected employers manually.
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

### 4.2 Release definitions

- **First usable version:** one source (Palantir) plus Telegram delivery, proving the end-to-end user experience. This is an incremental release, not a reduction in MVP scope.
- **MVP complete:** all six intended sources are operational, or the Product Owner has approved documented exceptions with reasons. Acceptance criteria in section 12 must be met.

A Palantir-first implementation sequence is a delivery strategy only. It does not change the intended six-source product scope.

### 4.3 Exclusions

The MVP does not include:

- Saved jobs, application tracking, or interview tracking.
- AI ranking, semantic recommendations, or generated application content.
- A web or mobile application.
- Multi-user accounts, subscriptions, or shared deployments.
- Job aggregators, LinkedIn, Indeed, or other third-party job databases.
- Browser automation, CAPTCHA bypass, login bypass, or evasion of bot protection.
- Automatic applications or recruiter contact.
- A user-facing source-health dashboard. A minimal status check is sufficient.
- Salary or visa-sponsorship filtering. Salary is displayed when available; sponsorship is recorded when exposed but does not filter alerts.
- Coverage of employers outside the six named sources without Product Owner approval.
- A digest mode (agreed out of scope). Quiet hours are not planned initially; that is a proposed default in section 5.3 and remains subject to approval.

## 5. Decision status

This section separates what is evidenced from what is agreed, proposed, or unresolved.

### 5.1 Verified findings

Verified as of 2026-10-22; detailed evidence is in [docs/sources.md](./sources.md):

- The six intended sources are technically accessible through official employer or ATS endpoints or sitemaps.
- Every usable source exposes a stable job identifier, enabling deduplication by source and source job ID.
- Early-career volume is concentrated in Wise, Palantir, Barclays, and Lloyds Graduate; Monzo and Arm provide smaller but relevant coverage.
- Field completeness is strong for title, employer, location, URL, and description; salary and closing-date coverage is uneven.
- Access constraints exist: Wise's query-string job pages and Arm's search-results path are disallowed by robots.txt; Workday endpoints carry a rate-limit risk; Palantir's Lever API requests a crawl delay.
- Several researched employers are not MVP-ready: Revolut and Skyscanner are blocked by bot protection; Faculty has no current openings; HSBC's UK coverage and feed completeness are unproven; Accenture's Workday endpoint returned HTTP 429; Anthropic, Bank of England, and Synthesia currently offer little early-career volume.

**Important distinction:** successful technical access is not permission for recurring automated access. Terms-of-service review for the six selected sources remains outstanding.

### 5.2 Agreed product decisions

| Decision | Status |
|---|---|
| One Telegram chat for the MVP | Agreed |
| Six-employer intended MVP coverage: Wise, Palantir, Barclays, Lloyds Graduate, Monzo, Arm | Agreed |
| Official employer careers pages and ATS endpoints only; no aggregators | Agreed |
| Immediate alert per new matching role; no digest | Agreed |
| Alert fields: title, employer, location, salary if available, closing date if available, link | Agreed |
| A role is new when its stable source job ID is first seen | Agreed |
| Target seniority: graduate, entry-level, early-career | Agreed |
| Target location: London plus UK remote/hybrid | Agreed at a high level; interpretation proposed below |
| Target role families: data/AI/software, quantitative/risk, general finance, consulting, business analysis | Agreed |
| Visa sponsorship is recorded when exposed but does not filter alerts | Agreed |
| Personal, single-user MVP; friends may self-host later | Agreed |

### 5.3 Proposed defaults requiring approval

These are recommendations, not agreed requirements.

| Proposed default | Why it matters |
|---|---|
| **First run is a silent baseline.** Record all current roles, send no individual alerts for them, and offer an optional one-time "current matches" summary. | Avoids a launch flood while still allowing the user to inspect the current market. |
| **Existing roles are updated but not re-announced.** | Prevents repeated alerts when salary, description, or closing date changes. |
| **Closure is tracked but not alerted.** Treat a role as closed after it is absent from two consecutive successful full scans. | Avoids false closure from a single failed fetch while keeping state useful. |
| **Internships are included only when explicitly labelled as intern/industrial placement and within a target role family.** | Internships may be relevant to an early-career user, but should not be confused with graduate schemes. |
| **Non-London UK hybrid roles are included if the source explicitly describes UK hybrid/remote eligibility.** | Interprets "London plus UK remote/hybrid" broadly; if the intent was London-only hybrid, this default must change. |
| **Global or non-UK remote roles are excluded unless the source explicitly states UK eligibility.** | Prevents roles the user cannot practically take. |
| **Matching is driven primarily by title and structured fields; descriptions are used only for explicit signals** such as "graduate", "entry level", or sponsorship. | Keeps matching explainable and avoids noisy semantic ranking in the MVP. |
| **No quiet hours initially.** Alerts are sent whenever a match is found; the user can mute Telegram if needed. | Consistent with immediate delivery; revisit after observing real volume. |
| **Failure notification after three consecutive failed scans per source**, with no repeated failure spam until recovery. | Makes silent breakage visible without turning the bot into an alert storm. |
| **No hard daily alert cap initially.** Measure volume for the first two weeks and tune the filters. | Avoids dropping relevant roles before real volume is known. |
| **Minimal status check:** last successful scan and last error per source, plus recent alert count. | Gives the user confidence without building a dashboard. |

### 5.4 Unresolved assumptions, risks, and dependencies

**Assumptions**

- A roughly 30-minute scan rhythm is useful and acceptable to the selected sources.
- Title-based matching can identify early-career roles with acceptable precision after tuning.
- The user will review early alerts and help tune the filters.
- Telegram is sufficient for delivery; no separate interface is needed.
- The selected sources remain structurally stable enough for maintainable integrations.

**Risks and dependencies**

- Terms-of-service review for all six sources has not been completed.
- Workday rate limits and pagination may affect Barclays and Lloyds reliability.
- Wise and Arm depend on HTML page structure, which can change without notice.
- Source identifiers can change when an employer reposts a role; this may produce an alert for an existing job.
- Broad role-family matching may create noise; narrow matching may miss relevant roles.
- A silent baseline may make the bot appear inactive until a new role is published.
- Telegram token and chat ID must be kept out of the repository and logs.
- The continuous-running host, scheduling, storage, HTTP/parsing, and library choices are engineering decisions, not PRD decisions.
- Repository visibility and licensing for future self-hosting are unresolved; they do not block the personal MVP.

## 6. Intended user journey

1. **One-time setup.** The user configures the six sources, the Telegram destination, and the role/location preferences.
2. **Baseline.** On the first successful scan, the system records existing roles without sending a flood of individual alerts. The user may request a one-time summary of current matches.
3. **Ongoing monitoring.** The system checks the sources on a roughly 30-minute rhythm and compares each role's stable source ID with previously seen IDs.
4. **Matching.** New roles are evaluated against the agreed role families, seniority, and location rules. A role that does not match is stored but not announced.
5. **Alert.** A matching new role produces one concise Telegram message with the agreed fields and a link to the official listing.
6. **Action.** The user opens the official listing and decides whether to apply outside Job-Scout.
7. **Review and tune.** The user reviews alert relevance and adjusts filters or source coverage. A minimal status check shows whether each source is healthy.
8. **Failure visibility.** If a source fails repeatedly, the user receives a concise failure notice and can see the last successful scan.

## 7. Functional requirements

| ID | Requirement |
|---|---|
| **FR-1 Source coverage** | The system must monitor the six intended employers and must not silently omit a configured source. Coverage changes require Product Owner approval. |
| **FR-2 Role representation** | Each role must retain its source, stable source job ID, employer, title, location, official URL, description, and, where available, posted date, closing date, salary, and remote/hybrid status. |
| **FR-3 New-role detection** | A role is new only when its stable source job ID has not been seen before for that source. |
| **FR-4 Matching** | The system must apply the agreed role-family, seniority, and location rules and must be able to explain why a role was included or excluded. |
| **FR-5 First-run behaviour** | The first successful full scan of a source must establish a baseline without sending individual alerts for pre-existing roles. A partial or failed scan must not be treated as a completed baseline. |
| **FR-6 Deduplication** | The system must send at most one alert per source job ID per Telegram chat. Retries and restarts must not create duplicate alerts. |
| **FR-7 Notification content** | Each alert must contain title, employer, location, salary if available, closing date if available, and a link to the official listing. Missing optional fields must be omitted cleanly. |
| **FR-8 Notification timing** | A matching new role must be delivered as soon as practical after detection, without waiting for a digest. If delivery pacing is required, alerts must not be dropped. |
| **FR-9 Failure isolation** | A failure in one source must not prevent the other sources from being checked. Retries must not create duplicate alerts or overwhelm the source. |
| **FR-10 Status visibility** | The user must be able to check the last successful scan, last error, and recent alert activity per source. This does not require a dashboard. |
| **FR-11 Persistence** | Seen source IDs, notification history, and source status must survive restarts. |
| **FR-12 Configuration** | Routine changes to sources, filters, and the Telegram destination should be possible through configuration rather than code changes. |
| **FR-13 Closure handling** | A role absent from two consecutive successful full scans should be marked closed. Closure alerts are not required in the MVP. |
| **FR-14 Security and compliance** | Secrets must not enter source control or logs. The system must respect source access restrictions and must not bypass CAPTCHAs, authentication, or bot protection. |
| **FR-15 Auditability** | The system should retain enough match/decision context to review why a role was or was not alerted. This supports tuning and debugging without building a dashboard. |

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

**Proposed inclusion signals:** graduate, entry-level, early-career, junior, intern/industrial placement, analyst without a senior qualifier, and associate only when the role is explicitly early-career.

**Proposed exclusions:** senior, lead, principal, staff, head, director, vice president, and manager roles unless they are explicitly part of a graduate programme; sales, marketing, human resources, legal, and pure accounting/audit roles outside the target families; roles requiring a PhD unless explicitly early-career.

The exact keyword and synonym list is a **proposed default** to be validated during the first implementation increment. The PRD defines the categories and intent, not an exhaustive dictionary.

### 8.2 Location rules

**Agreed intent:** London plus UK remote/hybrid roles.

**Proposed interpretation:**

- Include a role if any listed location is London.
- Include a role if it is explicitly UK remote or UK hybrid.
- Include a multi-location role if at least one listed location qualifies.
- Exclude global remote, non-UK remote, and roles whose only locations are outside the UK.
- Treat "remote-friendly" as insufficient unless UK eligibility is explicit.
- If the only qualifying arrangement is a non-London UK hybrid role, include it under the proposed interpretation above; this needs Product Owner confirmation.

### 8.3 Other matching inputs

- **Visa sponsorship:** record when exposed; do not filter.
- **Salary:** display when available; do not filter.
- **Closing date:** display when available; do not filter.
- **Description:** use only for explicit signals, not open-ended semantic ranking.
- **Ambiguous titles:** when seniority is unclear, prefer excluding the role from the first alert and reviewing the dry-run sample.

## 9. First-run and deduplication behaviour

### 9.1 First run

- The first successful full scan of each source establishes a baseline.
- Roles found during baseline are stored but do not produce individual alerts.
- The user may request a one-time summary of current matches.
- If a source scan fails or is visibly incomplete, the baseline is not marked complete and the source is retried.
- A source that cannot be baselined must be visible in the status check rather than silently treated as empty.

### 9.2 Deduplication

- The primary key is `(source, stable_source_job_id)`.
- The notification log records successful delivery per Telegram chat.
- Retries are idempotent: a failed or repeated send must not create a duplicate alert.
- A role that disappears and later reappears with the same source ID is not re-announced by default.
- A repost with a new source ID may be announced as new. This is an accepted MVP limitation and should be monitored for duplicate-like noise.
- Cross-source duplicates are not expected for the six-employer set, but if the same employer appears through two sources, a duplicate heuristic may be needed later.

## 10. Notification expectations

- **Channel:** one Telegram chat.
- **Cadence:** immediate message per new matching role; no digest.
- **Content:** title, employer, location, salary if available, closing date if available, and official link.
- **Format:** concise and scannable; optional fields omitted rather than shown as empty labels.
- **Link:** must open the official employer or ATS listing, not an aggregator or tracked redirect.
- **Quiet hours:** none initially, subject to approval.
- **Volume:** no hard cap initially; monitor and tune after the first two weeks.
- **Failure notice:** one concise message after three consecutive failed scans of a source; no repeated failure spam until recovery. A recovery notice is optional.
- **No re-alerts:** updates, salary changes, description changes, and closures do not trigger alerts by default.

## 11. Operational, security, and compliance requirements

- **Single-user operation:** one Telegram chat; no multi-user account model in the MVP.
- **Continuous operation:** the service should run continuously and check sources on a roughly 30-minute rhythm. Delivery is best-effort, not a real-time guarantee.
- **Source access:** use official employer or ATS endpoints only. Do not use aggregators or third-party job databases.
- **Permission versus access:** technical accessibility does not establish permission for recurring automated access. Terms of service for all six sources must be reviewed before production use.
- **Source-specific constraints:** Wise query-string job pages and Arm search-results paths must not be crawled; Palantir's crawl delay must be respected; Workday rate limits must be respected.
- **Secrets:** the Telegram token and chat ID must be stored outside source control and must not appear in logs or error messages.
- **Data minimisation:** store only what is needed for job state, deduplication, notification history, and basic source status. Do not collect CVs, application data, or unrelated personal information.
- **Retention:** the exact retention period is unresolved. Job descriptions and notification history should be retained only as long as needed for deduplication, debugging, and user review.
- **Failure isolation:** one source failure must not stop the others; the status check must make the failure visible.
- **Restart safety:** persisted state must prevent duplicate alerts after a restart.
- **Least exposure:** no public administrative interface is required for the MVP; inbound exposure should be minimised.
- **Future self-hosting:** repository visibility and licensing must be decided before a public clone-and-run release, but do not block the personal MVP.

## 12. Acceptance criteria

### 12.1 First usable version (Palantir + Telegram)

| ID | Criterion |
|---|---|
| **AC-1** | A Palantir scan returns normalized roles, or explicitly reports why records were skipped. |
| **AC-2** | The first scan establishes a baseline without individual alerts for existing roles; the optional current-matches summary, if enabled, lists those roles. |
| **AC-3** | A previously unseen Palantir role ID produces exactly one Telegram alert. |
| **AC-4** | Re-running the scan does not re-alert the same role ID. |
| **AC-5** | An update to an existing role does not produce a new alert by default. |
| **AC-6** | A positive/negative matching test set produces the expected include/exclude results. |
| **AC-7** | London, UK-remote, UK-hybrid, non-UK, and multi-location test cases produce the expected location decisions. |
| **AC-8** | An alert contains the agreed fields, omits missing optional fields cleanly, and links to the official listing. |
| **AC-9** | A simulated source failure does not stop other work, and a retry does not duplicate an alert. |
| **AC-10** | Restarting the service does not duplicate previously sent alerts. |
| **AC-11** | No Telegram token or chat ID appears in the repository or logs. |
| **AC-12** | A matching new role triggers a Telegram alert after scan completion, without waiting for a digest. Proposed latency thresholds are in section 13. |

### 12.2 MVP complete (all six sources)

| ID | Criterion |
|---|---|
| **AC-13** | All six intended sources are configured and complete at least one successful full scan. |
| **AC-14** | No source is silently omitted; any Product Owner-approved exception is documented with a reason. |
| **AC-15** | Each source's expected job count is reproduced within a documented tolerance, or the discrepancy is explained. |
| **AC-16** | Matching and location rules are applied consistently across sources. |
| **AC-17** | Cross-source and retry duplicate alerts are zero. |
| **AC-18** | A source failure is isolated, visible in status, and does not create duplicate alerts. |
| **AC-19** | Terms-of-service review is documented for each deployed source; unresolved permission blocks deployment of that source. |
| **AC-20** | The service completes a seven-day unattended run; the proposed reliability threshold is in section 13. |
| **AC-21** | The user can inspect last successful scan, last error, and recent alert activity per source. |

## 13. Proposed success measures

These are **proposals for approval**, not commitments.

| Measure | Proposed target | How it would be measured |
|---|---|---|
| Matching precision | ≥70% of alerts judged relevant over the first two weeks | Weekly manual review of the alert log |
| Matching recall | ≥90% on a weekly sample of 20 roles that meet the agreed rules | Manual comparison of source roles with alerts |
| Duplicate alerts | 0 per source job ID and chat | Notification log |
| Alert delivery latency | Median under 2 minutes from scan completion; p95 under 5 minutes | Timestamped logs |
| Scan rhythm | At most 30 minutes plus jitter | Scheduler/status log |
| Reliability at MVP complete | ≥95% of scheduled scans successful; no silent gap longer than one cycle | Seven-day unattended run |
| Alert volume | Within a Product Owner-agreed daily range | Daily count; tune after week one |
| Time to first relevant alert | Under 24 hours after deployment | First alert timestamp and user review |
| Directional outcome | At least one application or interview traceable to an alert within four weeks | Self-reported |

The number of sources, number of stored jobs, and number of messages sent are **not** success measures by themselves.

## 14. Risks, dependencies, and open questions

### 14.1 Material risks

| Risk | Impact | Current position |
|---|---|---|
| Terms of service do not permit recurring automated access | A source may need to be removed or accessed differently | Not yet reviewed; must be resolved before production |
| Workday rate limits or pagination changes | Barclays/Lloyds scans may be incomplete or fail | API worked in validation; 429 behaviour must be handled |
| Wise or Arm page structure changes | Parsing may silently miss or misread roles | HTML-based access; needs fixtures and failure detection |
| Matching is too broad or too narrow | Noise or missed roles undermines the product | Needs a dry-run sample and tuning |
| First-run baseline is misunderstood | User may think the bot is broken or receive a flood | Proposed silent baseline plus optional summary |
| Telegram token leakage | Security and privacy incident | Secrets must stay out of source control and logs |
| Host or scheduler failure | Alerts stop silently | Needs status visibility and failure notification |
| Source ID changes on repost | Duplicate-like alerts | Accepted MVP limitation; monitor |
| Broad scope creep | Delays a usable personal MVP | Six sources only; no multi-user, AI ranking, or dashboard |

### 14.2 Dependencies

- Telegram bot token and chat ID.
- Confirmed permission or acceptable terms for each of the six sources.
- Agreement on matching keywords, internship inclusion, location semantics, and first-run behaviour.
- A continuously available host and a reliable way to run the service unattended.
- A persistent store for seen roles and notification history.

### 14.3 Open questions

1. Should internships be included, excluded, or treated as a separate preference?
2. Does “London plus UK remote/hybrid” include hybrid roles outside London?
3. Should existing roles be silently baselined, summarised once, or alerted individually on first run?
4. Should role updates or closures ever produce alerts?
5. What daily alert volume is acceptable?
6. What retention period is appropriate for job descriptions and notification history?
7. What failure threshold should trigger a Telegram notice?
8. Should the repository become public or licensed for friends to clone, and when?
9. Which exact keyword lists and seniority exclusions should be used?
10. Which technical choices — storage, scheduling, HTTP/parsing, deployment, libraries — should engineering make after product approval?

## 15. Technical considerations for engineering planning

These are constraints and questions for the next stage, not architecture decisions in this PRD.

- Source access is heterogeneous: structured JSON, Workday APIs, and sitemap-plus-HTML pages. The engineering plan must account for different failure modes and field completeness.
- Stable source IDs are available, but some sources expose only relative posted dates or omit salary and closing dates.
- Workday rate limiting, pagination, and HTML drift are material reliability risks.
- The product requires persistent seen-state and a notification log that survives restarts.
- The product requires retry/backoff, source isolation, and a user-checkable status view.
- The product requires secure handling of the Telegram token and chat ID.
- Matching and location normalisation need a reviewable ruleset and fixture-based tests before source expansion.
- The implementation stack, storage mechanism, scheduling approach, deployment model, and library choices remain open for the subsequent engineering assessment.
