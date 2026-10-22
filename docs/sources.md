# Job source inventory — Stage 0

**Status:** validated evidence for MVP source selection  
**Last checked:** 2026-10-22  
**Scope:** 12 Tier A employers, plus two high-value Tier B discoveries  
**Not included yet:** production code, dependencies, Telegram integration, or a full legal review

## Purpose

This document records what was actually observed when checking whether UK employers' official careers pages and ATS endpoints can be monitored reliably, legitimately, and usefully for the MVP.

It is a source-selection document, not a promise of coverage. Job counts and role titles are point-in-time snapshots and will change.

## Decision summary

**Locked MVP source set:**

1. Wise
2. Palantir
3. Barclays
4. Lloyds Graduate
5. Monzo
6. Arm

**Why this set:** it combines strong early-career volume (Wise, Palantir, Barclays, Lloyds Graduate) with clean public JSON or sitemap access, UK/London coverage, and a manageable number of adapter patterns. Monzo and Arm add fintech and engineering coverage.

**Implementation order:**

1. Palantir — Lever JSON, simplest end-to-end adapter
2. Barclays and Lloyds Graduate — Workday JSON
3. Wise and Arm — sitemap plus HTML job-detail parsing
4. Monzo — Greenhouse JSON

**Deferred, not rejected:**

- Anthropic — strong AI brand, but mostly senior roles
- Bank of England — good data/analyst roles, but no current early-career roles
- Synthesia — AI employer, but no current early-career roles
- HSBC — feed works, but UK coverage and completeness are unproven
- Accenture — Workday tenant identified, but the endpoint returned HTTP 429

**Not MVP-ready:**

- Revolut — 403 security check; no public ATS board found
- Skyscanner — CAPTCHA on the careers page; Workable board is empty
- Faculty — valid Workable board, but 0 current openings

## Method

- Sent a descriptive user agent: `JobScoutResearch/0.1`.
- Checked HTTP status and redirects for careers pages and robots.txt.
- Inspected careers-page HTML for ATS signatures: Greenhouse, Lever, Ashby, Workable, Workday, Taleo, iCIMS, Oracle Cloud, SmartRecruiters, TalentBrew.
- Tested public JSON APIs and sitemaps where available.
- Parsed sample records to confirm stable IDs and field availability.
- Did not bypass CAPTCHAs, authentication, or bot protection.
- Did not use aggregators or third-party job databases.

**Important limitation:** robots.txt permission is not the same as contractual permission. Each selected employer's terms of service must be reviewed before production use.

## MVP source matrix

| Employer | Access pattern | Discovery endpoint | Live roles | UK / London | Early-career evidence | Key fields | Main risks |
|---|---|---|---:|---|---|---|---|
| **Wise** | Attrax sitemap + HTML job pages | `https://wise.jobs/vacanciessitemap.xml` | 430 | 195 London | 7 graduate schemes + 2 internships | `jid-####`, title, location, salary, description | HTML parsing; no closing date; robots disallows `/jobs?*` |
| **Palantir** | Lever public JSON | `https://api.lever.co/v0/postings/palantir?mode=json` | 306 | 45 London | Many new-grad and internship roles | UUID, title, categories, `createdAt`, `hostedUrl`, `workplaceType` | No salary; `Crawl-delay: 1` |
| **Barclays** | Workday CXS JSON + sitemap | `https://barclays.wd3.myworkdayjobs.com/wday/cxs/barclays/External_Career_Site_Barclays/jobs` | 731 | 94 early-career results; London, Glasgow, Knutsford, Manchester | 30 graduate, 34 intern, 19 apprentice | title, `externalPath`, `locationsText`, `postedOn`, requisition ID | Relative posted dates; 429 risk; detail page needed for salary/closing date |
| **Lloyds Graduate** | Workday CXS JSON + sitemap | `https://lbg.wd3.myworkdayjobs.com/wday/cxs/lbg/Graduate_careers/jobs` | 33 graduate schemes | London, Bristol, Leeds, Manchester, Edinburgh | 33 graduate schemes; 5 Data Science & AI, 7 Software Engineering | title, `externalPath`, `locationsText`, `postedOn`, requisition ID | Graduate-only site; general jobs are on a separate `LBG_Careers` site |
| **Monzo** | Greenhouse public JSON | `https://boards-api.greenhouse.io/v1/boards/monzo/jobs?content=true` | 73 | 55 London | 2 UK internships | numeric ID, title, location, `first_published`, apply URL, description | No salary or deadline in structured fields; mostly experienced roles |
| **Arm** | TalentBrew sitemap + HTML job pages | `https://careers.arm.com/sitemap.xml` | 426 | 97 UK-slug roles | 5 UK early-career roles | Job ID, date posted, location, salary range, description | Robots disallows `/search-jobs/`; use sitemap; HTML parsing |

## Tier A detail

### Monzo — usable

- Careers page: `https://monzo.com/careers` returned HTTP 200.
- Provider: Greenhouse. `https://boards-api.greenhouse.io/v1/boards/monzo/jobs?content=true` returned 73 jobs.
- UK focus: 55 jobs include London in the location string; several are Cardiff/London/Remote.
- Early-career: 2 UK internships — Associate Data Scientist and Associate Software Engineer.
- Fields: numeric job ID, title, location, `first_published`, `updated_at`, `absolute_url`, description, departments, offices, metadata.
- Gaps: no `application_deadline` values in the sample; no structured salary in metadata.
- Robots: Monzo allows the careers path. Greenhouse robots allows everything except `/embed/`.

### Anthropic — conditional

- Careers page: `https://www.anthropic.com/careers` returned HTTP 200.
- Provider: Greenhouse. `https://boards-api.greenhouse.io/v1/boards/anthropic/jobs?content=true` returned 680 jobs.
- UK focus: 51 jobs include London.
- Early-career: three London-based Anthropic Fellows programmes; otherwise the board is heavily senior/research-oriented.
- Fields: same Greenhouse structure as Monzo — numeric ID, title, location, `first_published`, `updated_at`, apply URL, description.
- Gaps: no structured salary; no deadlines.
- Robots: Anthropic allows all; Greenhouse API allows except `/embed/`.

### Palantir — strong

- Careers page: `https://www.palantir.com/careers/` returned HTTP 200.
- Provider: Lever. `https://api.lever.co/v0/postings/palantir?mode=json` returned 306 jobs.
- UK focus: 45 jobs with London, United Kingdom as the category location.
- Early-career: a large new-grad and internship pipeline, including Software Engineer New Grad, Forward Deployed Software Engineer New Grad, and internships in London.
- Fields: UUID, title, category location/team/commitment, `createdAt` (epoch ms), `hostedUrl`, `applyUrl`, `workplaceType`, description; no `salaryRange` in the sample.
- Robots: Lever API allows all with `Crawl-delay: 1`.

### Synthesia — low fit

- Careers page: `https://www.synthesia.io/careers` returned HTTP 200.
- Provider: Ashby. `https://api.ashbyhq.com/posting-api/job-board/synthesia?includeCompensation=true` returned 33 jobs.
- UK focus: 12 London roles.
- Early-career: none detected by title.
- Fields: UUID, title, location, secondary locations, job/apply URLs, `publishedAt`, department, team, employment type, workplace type, compensation object.
- Gaps: compensation summary was empty for almost all sampled jobs.
- Robots: Ashby's robots endpoint returned 401; terms need separate review.

### Faculty — empty

- Careers page redirects to `https://faculty.ai/en-gb/join-the-team`; job board is `/en-gb/job-board`.
- Provider: Workable, `https://apply.workable.com/faculty/`.
- Current openings: 0. The Workable `llms.txt` explicitly reports 0 current openings.
- Robots: Faculty allows all except preview paths; Workable allows crawling.
- Verdict: valid adapter, but not useful for live MVP testing today.

### Accenture UK — deferred

- Careers page: `https://www.accenture.com/gb-en/careers` returned HTTP 200.
- Provider: Workday tenant `accenture.wd103.myworkdayjobs.com`, site `AccentureCareers`.
- API attempt: POST to `/wday/cxs/accenture/AccentureCareers/jobs` returned HTTP 500 once, then HTTP 429 on retries. The sitemap also returned HTTP 429.
- Workday itself is viable: Barclays and Lloyds both returned HTTP 200 with job data.
- Robots: Workday robots allows `/AccentureCareers/` and disallows `/refreshFacet/`.
- Verdict: tenant identified, but data could not be validated from this environment. Retry later with backoff or from a different network.

### HSBC — conditional / unproven for UK

- Careers page: `https://www.hsbc.com/careers` returned HTTP 200; `find-a-job` links to `https://mycareer.hsbc.com/en_GB/external/SearchJobs/`.
- Feed: `https://mycareer.hsbc.com/en_GB/external/SearchJobs/feed/?pipelineRecordsPerPage=100` returned an RSS feed with only 3 items, regardless of page-size parameters. All 3 were Poland-based.
- Detail page fields: Job ID, location, area of interest, job type, work style, opening date, closing date, description.
- UK coverage: no United Kingdom country label or facet was found in the search page. UK roles may live elsewhere or require a country filter.
- Completeness risk: a 3-item RSS feed cannot guarantee that all new roles are seen at 30-minute polling.
- Robots: HSBC allows all.
- Verdict: technically usable, but not a primary MVP source until UK coverage and completeness are proven.

### Bank of England — conditional / low fit now

- Careers page: `https://www.bankofengland.co.uk/careers` returned HTTP 200.
- Provider: Oracle Cloud HCM at `https://eoff.fa.em1.ukg.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001`.
- API: `https://eoff.fa.em1.ukg.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList&finder=findReqs;siteNumber=CX_1001&limit=50` returned 24 jobs.
- Fields: numeric ID, title, `PostedDate`, `PostingEndDate`, primary location, country, short description, job type, worker type, study level.
- UK focus: all 24 were UK-based — London, Leeds, and Loughton.
- Early-career: no current early-career titles; the board is mostly senior/experienced.
- Robots: BoE allows the careers path and disallows `/search`, `/forms`, and similar paths. Oracle Cloud returned 404 for robots.txt, so terms need separate review.
- Verdict: good future source for data/analyst roles, but low early-career yield right now.

### Arm — good

- Careers page: `https://www.arm.com/careers` redirects to `https://careers.arm.com/`, which returned HTTP 200.
- Provider: TalentBrew (Radancy) front end, with iCIMS application links.
- Discovery: `https://careers.arm.com/sitemap.xml` returned 883 URLs, including 426 job URLs with `lastmod`.
- UK focus: 97 UK-slug roles — Cambridge 89, Manchester 5, Bristol 2, Sheffield 1.
- Early-career: 5 UK roles, including Data Analytics Intern, Quality and Compliance Intern, Quality and Reliability Engineering Intern, and Intern Program Engineering Pathways.
- Job-page fields: Job ID, date posted, location, category, salary range, description.
- Robots: `https://careers.arm.com/robots.txt` disallows `/search-jobs/`; the sitemap and individual `/job/` pages are allowed.
- Verdict: usable via sitemap discovery. Do not crawl the disallowed search path.

### Wise — strong

- Careers page: `https://wise.com/careers/` returned HTTP 404; the real site is `https://wise.jobs/`.
- Provider: Attrax.
- Discovery: `https://wise.jobs/sitemap.xml` is a sitemap index; `https://wise.jobs/vacanciessitemap.xml` contains 430 job URLs with `lastmod`.
- UK focus: 195 job URLs contain London.
- Early-career: at least 7 graduate schemes and 2 internships, including:
  - Analytics Intern
  - Data Science Intern
  - Graduate Data Scientist
  - Graduate Software Engineer
  - Graduate Product Analyst
  - Graduate Product Designer
  - Graduate UX Researcher
  - Graduate Content Designer
  - Design and Research Operations Graduate
- Job-page fields: title, salary, location, description, `jid-####`.
- Example: Graduate Data Scientist, London, salary £58,000.
- Robots: `https://wise.jobs/robots.txt` disallows `/jobs?*`, `/admin*`, `/workflow*`, and similar paths. The vacancies sitemap is allowed.
- Verdict: strongest early-career source in the Tier A set. Use the sitemap, not query-string pagination.

### Revolut — blocked

- Careers page: `https://www.revolut.com/careers/` returned HTTP 403 with a security-check page.
- Provider: no public Greenhouse, Lever, or Ashby board found; SmartRecruiters returned 0 results.
- Robots: Revolut disallows `/api/`, `*.json$`, and `*?*`.
- Verdict: not MVP-ready. Do not bypass the security check.

### Skyscanner — blocked / unresolved

- Careers page: `https://www.skyscanner.net/careers` returned HTTP 200 but redirected to a CAPTCHA page.
- Workable: `https://apply.workable.com/skyscanner/` exists but reports 0 current openings.
- Other ATS guesses: Greenhouse, Lever, and Ashby returned 404.
- Verdict: unresolved. The public Workable board is empty and the main site is CAPTCHA-gated.

## Additional validated sources (Tier B)

### Barclays — strong

- Careers page: `https://www.barclays.co.uk/careers/` redirects to `https://search.jobs.barclays/`.
- Provider: Workday, `https://barclays.wd3.myworkdayjobs.com/External_Career_Site_Barclays`.
- API: POST to `/wday/cxs/barclays/External_Career_Site_Barclays/jobs` returned 731 jobs.
- Early-career: `searchText: "graduate"` returned 94 results. Facets showed 30 graduate, 34 intern, 19 apprentice, and 11 permanent/regular roles.
- Locations: London/Canary Wharf, Glasgow, Knutsford, Manchester, and others.
- Fields: title, `externalPath`, `timeType`, `locationsText`, `postedOn`, requisition ID in `bulletFields`.
- Sitemap: `https://barclays.wd3.myworkdayjobs.com/External_Career_Site_Barclays/siteMap.xml` returned 731 URLs.
- Robots: Workday allows `/External_Career_Site_Barclays/`; sitemap is published.
- Verdict: promoted from Tier B to the MVP set.

### Lloyds Graduate — strong

- General careers page: `https://www.lloydsbankinggroup.com/careers.html`.
- General Workday site: `https://lbg.wd3.myworkdayjobs.com/LBG_Careers` returned 78 jobs.
- Graduate site: `https://lbg.wd3.myworkdayjobs.com/Graduate_careers` returned 33 graduate schemes.
- Early-career fields: title, `externalPath`, `locationsText`, `postedOn`, requisition ID.
- Graduate coverage: 5 Data Science & AI roles, 7 Software Engineering roles, plus Risk, Finance, Product, and HR schemes.
- Locations: London, Bristol, Leeds, Manchester, Edinburgh, Chester, Halifax, Cardiff, Birmingham.
- Sitemap: `https://lbg.wd3.myworkdayjobs.com/Graduate_careers/siteMap.xml` returned 33 URLs.
- Robots: Workday allows `/Graduate_careers/`, `/LBG_Careers/`, `/Undergraduate_careers/`, and related paths; sitemaps are published.
- Verdict: promoted from Tier B to the MVP set.

### Quantexa and ElevenLabs — promising AI alternates

- Quantexa: Ashby board returned 30 jobs.
- ElevenLabs: Ashby board returned 160 jobs.
- Neither has been checked for UK/London coverage, early-career supply, or field completeness yet.
- They are candidates to replace low-yield AI sources later, not part of the locked MVP set.

## Cross-cutting findings

### Access patterns

The validated sources use eight distinct patterns:

1. **Greenhouse** — Monzo, Anthropic
2. **Lever** — Palantir
3. **Ashby** — Synthesia, Quantexa, ElevenLabs
4. **Workday** — Barclays, Lloyds, Accenture (rate-limited)
5. **Sitemap plus HTML** — Wise (Attrax), Arm (TalentBrew)
6. **Oracle HCM REST** — Bank of England
7. **Custom portal plus RSS** — HSBC
8. **Workable** — Faculty (empty)

### Stable identifiers

Every usable source exposes a stable ID:

- Greenhouse: numeric job ID
- Lever: UUID
- Ashby: UUID
- Workday: requisition ID in `bulletFields`
- Oracle HCM: numeric requisition ID
- Arm: numeric job ID plus `data-job-id`
- Wise: `jid-####` in the URL

This supports deduplication by `(source, source_job_id)`.

### Field availability

| Field | Availability | Notes |
|---|---|---|
| Stable job ID | Strong across usable sources | Primary deduplication key |
| Title | Strong | Sometimes includes level or location |
| Employer | Strong | Comes from the source config |
| Location | Strong but inconsistent | Multi-location strings, "2 Locations", remote semantics vary |
| URL | Strong | Apply/detail URL available everywhere |
| Description | Strong | Available via JSON or detail page |
| Posted date | Common | Workday often returns relative text like "Posted 9 Days Ago" |
| Closing date | Weak | HSBC detail pages and some Workday records; mostly absent |
| Salary | Weak | Structured at Wise and Arm; otherwise absent or buried in description |
| Remote/hybrid | Inconsistent | Lever/Ashby expose workplace type; Workday and Arm do not reliably |

### Compliance and access constraints

- **Revolut**: 403 security check; do not bypass.
- **Skyscanner**: CAPTCHA; Workable board empty; unresolved.
- **Arm**: `/search-jobs/` disallowed; use the sitemap.
- **Wise**: `/jobs?*` disallowed; use the vacancies sitemap.
- **Accenture**: `/careers/jobsearch?` disallowed on the main site; Workday API returned 429.
- **Lever**: `Crawl-delay: 1`.
- **Greenhouse**: allows all except `/embed/`.
- **Workable**: allows crawling, but signals `ai-train=no`.
- **Ashby**: robots endpoint returned 401; terms need review.
- **Oracle Cloud**: robots.txt returned 404; terms need review.

### Polling implications

- JSON sources (Greenhouse, Lever, Ashby, Workday) can be polled as full snapshots every 30 minutes.
- Sitemap sources (Wise, Arm) should cache the sitemap and fetch detail pages only for new or changed IDs.
- Workday should paginate with `limit`/`offset` and back off on 429.
- HSBC's 3-item RSS feed is not sufficient for reliable 30-minute coverage.
- Accenture needs a retry strategy or a different network before it can be considered.

### Early-career supply

- **Strong:** Wise, Palantir, Barclays, Lloyds Graduate.
- **Moderate:** Monzo, Arm.
- **Weak currently:** Anthropic, Bank of England, Synthesia, HSBC.

Source access is not the same as useful role supply. The MVP should prioritise the strong sources rather than spreading requests evenly across all 50 employers.

### Removal and update detection

- Prefer full-snapshot comparison per source: mark a role closed when its ID disappears from a complete fetch.
- Do not infer closure from a single failed request or a partial page.
- Sitemap `lastmod` helps detect updates but does not prove a role still exists.
- The HSBC RSS feed is too small to support reliable closure detection.

## Tier B backlog

The original 50-employer universe included 38 Tier B employers. Barclays and Lloyds have been promoted to the MVP set, leaving 36 for later triage.

**Tech, AI, data, and digital (13)**
Starling Bank, Checkout.com, Deliveroo, Ocado Technology, BT Group, Sky, BBC, Financial Times, Quantexa, ElevenLabs, Wayve, Graphcore, BenevolentAI.

**Consultancy (8)**
Deloitte UK, PwC UK, EY UK, KPMG UK, McKinsey UK, BCG London, Bain London, Capgemini UK.

**Finance and insurance (9 remaining)**
NatWest Group, Santander UK, Nationwide, Aviva, Legal & General, Schroders, Man Group, JP Morgan UK, Goldman Sachs London.

**Public sector and research (6)**
Financial Conduct Authority, Alan Turing Institute, UK AI Safety Institute, NHS England, ONS, Met Office.

These have not been validated. Treat them as candidates, not supported sources.

## Open validation gaps

1. **Terms of service** — robots.txt was checked; contractual terms were not fully reviewed.
2. **Tier B triage** — 36 employers remain.
3. **Workday detail pages** — confirm salary and closing-date availability for Barclays and Lloyds.
4. **HSBC UK** — find the UK-specific portal or prove that the current feed covers UK roles.
5. **Accenture** — retry Workday after a longer backoff or from a different network.
6. **Quantexa and ElevenLabs** — validate UK/location and early-career coverage.
7. **Location normalisation** — define how to treat multi-location roles, "2 Locations", and remote/hybrid variants.
8. **Removal semantics** — decide how many consecutive misses mark a role closed.

## Reproduction notes

- Counts are snapshots from 2026-10-22 and will change.
- No credentials, bot tokens, or personal data were used.
- No CAPTCHA, authentication, or bot-protection bypass was attempted.
- The next implementation step should start with the Lever adapter for Palantir, then Workday for Barclays and Lloyds Graduate, then the sitemap/HTML adapters for Wise and Arm.
