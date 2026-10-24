# Job-Scout — Source-access review (six MVP sources)

> **Archive (non-normative since D-066).** This review records what each source's published terms say, gathered while per-source review was still a gate. The operator has since accepted a blanket personal-use risk posture for any employer using a public ATS or careers endpoint (D-066). The evidence below remains useful for engineering judgement and is not a permission record.


**Status:** Evidence for a Product Owner decision. **Not** permission to proceed.
**Retrieved:** 2026-10-23
**Related:** [docs/sources.md](./sources.md) (reachability research) · [docs/prd.md](./prd.md) §9 · D-012 · D-027 · D-048 · D-055

## 1. What this review answers

One question per source: **may Job-Scout read this source automatically, repeatedly, as part of a personal job monitor?**

Three layers are kept strictly apart, because conflating them is how "it works" gets mistaken for "it is allowed":

| Layer | Meaning | How established here |
|---|---|---|
| **Technical reachability** | The source returns usable data today | Source research only ([docs/sources.md](./sources.md)). Not re-tested here, because probing job endpoints is the very behaviour under review |
| **Machine-readable crawling rules** | `robots.txt` does or does not exclude the path | Fetched and quoted verbatim |
| **Permission** | Terms, licence, or written consent allow recurring automated access | Retrieved from the official terms pages, quoted verbatim |

## 2. Verdict summary

| Source | Adapter | Request shape | `robots.txt` | Terms on automated access | Verdict |
|---|---|---|---|---|---|
| Palantir | `lever` | 1 × `GET` per scan | Allows, crawl-delay 1s | **Prohibits** scraping/crawling tools without express written permission | ⛔ **Blocked pending written permission** |
| Barclays | `workday` | `POST` listing + bounded `GET`s | Path family not covered | No scraping clause; copying data needs written permission; personal non-commercial use permitted | ⚠️ Unresolved; copying exceeds the permission granted |
| Lloyds Graduate | `workday` | `POST` listing + bounded `GET`s | Path family not covered | No storage in any "private electronic retrieval system" without prior written permission | ⚠️ Unresolved; local storage is engaged |
| Monzo | `greenhouse` | 1 × `GET` per scan | Allows (only `/embed/` disallowed) | **No website-terms document found**; provider API clause binds customers, not the public | ⚠️ Unresolved; least restrictive evidence |
| Wise | `sitemap_html` | Sitemap + job pages | Allows; `/jobs?*` disallowed | **Prohibits** robot/automatic processes to monitor or copy without prior written permission | ⛔ **Blocked pending written permission** |
| Arm | `sitemap_html` | Sitemap + job pages | Allows; `/search-jobs/` disallowed | No automated-access clause; reproducing/exploiting content needs written permission | ⚠️ Unresolved; no prohibition found, copying engaged |

**No source is approved for recurring automated access.** Two publish wording that prohibits the behaviour this product depends on; four are silent or engaged only on content copying. D-027 stays open.

## 3. Method and what was deliberately not done

- Fetched: `robots.txt` for each host an adapter uses, plus official terms/acceptable-use pages, §7.
- **Not** fetched: no job listing API, sitemap, vacancy page, search endpoint, or careers path was requested for this review. Every quoted clause was retrieved and read directly; nothing is quoted from memory or paraphrase.
- No adapter ran against a live source, no Telegram message was sent, no polling was enabled, no unattended mode was started.
- Where a page blocked retrieval or a document does not exist, that is stated as absence of evidence, never as permission.

## 4. Per-source findings

### 4.1 Palantir — `lever` adapter ⛔

- **Intended requests:** `GET https://api.lever.co/v0/postings/palantir?mode=json` — one request per scan.
- **`robots.txt`** (`https://api.lever.co/robots.txt`): `User-agent: *` / `Allow: /` / `Crawl-delay: 1`. The host asks for 1 request per second; Job-Scout makes one per 30 minutes.
- **Terms** (`https://www.palantir.com/terms-and-conditions/`, "Terms and Conditions of Website Use"), §4:
  > "Without Palantir's express prior written permission, you will not use software, devices, scripts, robots, scrapers, crawlers, or other automated tools or processes to scrape or copy the Site or any content available on or through the Site"
- The same section requires that no content be copied "without prior written consent or otherwise permitted by relevant law".
- **Assessment:** the prohibition targets automated tools and covers Palantir content generally, not only pages on `palantir.com`. The adapter fetches Palantir's own postings (title, location, description) on a schedule and stores copies. `robots.txt` permission on Lever's API host does not override Palantir's terms for Palantir content.
- **Open questions:** does Palantir consider its Lever-hosted postings inside "the Site"? Would Palantir grant permission for a single-user, non-commercial monitor? Does Lever's role as host change anything?
- **Lever's own terms** (`https://www.lever.co/legal/terms-of-service`): no scraping, crawling, or rate wording was found anywhere on the page; it is an employer-facing SaaS contract. `api.lever.co/robots.txt` is the only machine-readable permission signal, and Lever publishes no acceptable-use or public-API policy.

### 4.2 Barclays — `workday` adapter ⚠️

- **Intended requests:** `POST https://barclays.wd3.myworkdayjobs.com/wday/cxs/barclays/External_Career_Site_Barclays/jobs` for the listing, plus bounded `GET`s for placeholder locations.
- **`robots.txt`** (`https://barclays.wd3.myworkdayjobs.com/robots.txt`): `Sitemap: …/External_Career_Site_Barclays/siteMap.xml`; `User-agent: *` / `Allow: /External_Career_Site_Barclays/` / `Disallow: /Private/` / `Disallow: /refreshFacet/`.
  - The allow rule covers the public site prefix. The adapter's endpoint family lives under `/wday/cxs/…`, which that rule neither allows nor disallows. With no blanket disallow the path is not excluded, but it is also not covered by any allow rule.
  - The listing is a `POST`. `robots.txt` governs crawler `GET`s, so it cannot be read as permission for a JSON `POST` search either.
- **Terms** (`https://home.barclays/terms-of-use/`, §2.1 "Permitted Use and Access"):
  > "You are permitted to use and access the Website on a personal, non-commercial basis. You may not copy or make commercial use of the Website or any of the content including, but not limited to, information, imagery or data from, or underlying, the Website without obtaining our express written permission to do so."
- **Automated access:** no robot/scraper/crawler/data-mining clause was found in this document (full-text search returned no such hits).
- **Barclays UK site terms** (`https://www.barclays.co.uk/important-information/site-terms-conditions/`): "the information held is for your personal use only … you may not download (all or in part) or change the site without our permission". Also no automated-access clause.
- **Assessment:** personal non-commercial use is expressly permitted, which fits this product's single-user purpose — but storing copies of role data is "copy[ing] … data from, or underlying, the Website" without written permission. That is engaged even for personal use.
- **Workday's own terms** (`https://www.workday.com/en-us/legal/site-terms.html`, "Workday Online Terms of Service"), §2 "Prohibited Conduct":
  > "Use any data mining, robots or similar data gathering or extraction methods designed to scrape or extract data from our Sites"

  > "Develop or use any applications that interact with our Sites without our prior written consent"

  > "Bypass or ignore instructions contained in our robots.txt file"
  - Scope caveat: the terms define "Sites" as `www.workday.com` and associated web pages, the Community, and the Workday APIs. Whether customer-hosted `*.myworkdayjobs.com` career sites fall inside that definition is **not established by the text**, so this clause is a strong signal about Workday's posture rather than proven to bind Barclays' tenant host.
- **Open questions:** does Barclays treat its Workday careers site as covered by home.barclays terms? Is the ATS sitemap the sanctioned route? Who may grant permission — Barclays, Workday, or both?

### 4.3 Lloyds Banking Group (Graduate) — `workday` adapter ⚠️

- **Intended requests:** `POST https://lbg.wd3.myworkdayjobs.com/wday/cxs/lbg/Graduate_careers/jobs` plus bounded detail `GET`s. Only the `Graduate_careers` site is configured.
- **`robots.txt`** (`https://lbg.wd3.myworkdayjobs.com/robots.txt`): sitemaps published for eight career sites including `…/Graduate_careers/siteMap.xml`; `User-agent: *` with `Allow:` prefixes for `/broadbean_external/`, `/LBG_Careers/`, `/LBG_HigherApprenticeCareers/`, `/Undergraduate_careers/`, `/LBG_Singapore/`, `/Graduate_careers/`, `/Lloyds_Technology_Centre/`, `/USA_Careers/`; `Disallow: /refreshFacet/`. Same CXS/POST gap as Barclays.
- **Terms** (`https://www.lloydsbankinggroup.com/site-terms-and-conditions.html`, §2 "Content"):
  > "No part of the Website may be reproduced or stored in any other website or included in any public or private electronic retrieval system or service without our prior written permission. Any use of the Website other than in accordance with these Terms is prohibited."
- **Automated access:** no robot/scraper/crawler clause found; the only occurrence of "automat" is about permission terminating automatically on breach.
- **Assessment:** the clause reaches **storage**, not just publication. A local SQLite copy of role data used for deduplication and notification history is a "private electronic retrieval system" in ordinary reading, and the catch-all sentence prohibits any use not in accordance with the terms. This is the clearest content restriction of the six.
- **Open questions:** whether a single-user local copy counts as a "private electronic retrieval system"; whether Lloyds would grant permission; whether the sitemap route is sanctioned.

### 4.4 Monzo — `greenhouse` adapter ⚠️

- **Intended requests:** `GET https://boards-api.greenhouse.io/v1/boards/monzo/jobs?content=true` — one request per scan.
- **`robots.txt`** (`https://boards-api.greenhouse.io/robots.txt`): `User-agent: *` / `Disallow: /embed/`. The board API path is not disallowed.
- **Monzo's website terms: could not be found.** `https://monzo.com/legal` redirects to the current-account product terms ("These terms and using your Monzo Current Account"), which contain no website-use or automated-access section. Probing `/legal/website-terms`, `/legal/website-terms-of-use`, `/legal/website-terms-and-conditions`, `/legal/terms`, `/legal/terms-of-use` returned 404. The full `/legal/…` index reachable from the privacy notice contains no website-terms document.
- **No automated-access clause** was found in the document that does exist.
- **Greenhouse's own agreement** (`https://www.greenhouse.com/master-subscription-agreement`, §4(d)(ii) "Fair Use of API"):
  > "Customer agrees not to use or access, nor to enable a third party to use or access on its behalf, Greenhouse's API in a manner that is excessive, abusive, or negatively impacts, degrades, or impairs the Greenhouse API's or platform's availability, reliability, or performance."
  - This binds Greenhouse **customers** (employers) under an order form, not members of the public reading a public board. No public job-board API terms page was located; the developer documentation was outside this review's policy-only boundary, so any attribution requirement stated there is unverified.
- **Assessment:** the weakest documentary basis of the six, in the sense that there is almost nothing to cite — absence of a prohibition is not permission. Technically and by `robots.txt` this is the cleanest source (one request per scan, no page crawling).
- **Open questions:** is there an unlinked Monzo website-terms page? Does Greenhouse impose attribution or usage terms on public board consumers in its developer docs?

### 4.5 Wise — `sitemap_html` adapter (profile `wise`) ⛔

- **Intended requests:** `GET https://wise.jobs/sitemap.xml`, `GET https://wise.jobs/vacanciessitemap.xml`, then one `GET` per new or changed page under `https://wise.jobs/jobs/…`.
- **`robots.txt`** (`https://wise.jobs/robots.txt`): `User-agent: *` / `Allow: /` / `Disallow: /jobs?*` plus `/admin*`, `/workflow*`, `/register*`, `/recruitersearch*`, `/newprofile*`, `/login*`, `/editprofile*`, `/candidateupdatecredentials*`, `/candidateresetpassword*`, `/candidatehome*`.
  - The adapter already honours the published wish: sitemap and clean job URLs only, never query-string job pages.
- **Terms** (`https://wise.com/gb/legal/terms-of-use-personal`, "Personal Terms of Use", §8.2 "You must not misuse our systems"):
  > "(c) Using an anonymising proxy; use any robot, spider, other automatic device, or manual process to monitor or copy our websites without our prior written permission; or use any device, software, or routine to bypass our robot exclusion headers."
- The same clause prohibits "taking any action that imposes an unreasonable or disproportionately large load on our websites, software, systems".
- **Acceptable Use Policy** (`https://wise.com/gb/legal/acceptable-use-policy`): no scraping wording.
- **Assessment:** this is an explicit prohibition of exactly the behaviour — an automatic process that monitors Wise's website. `robots.txt` permission for the paths does not mitigate it, and the clause covers `wise.jobs` as part of "our websites". Politeness measures (0.5 s spacing, ~195 pages on a first scan) reduce load but do not address the monitoring prohibition.
- **Open questions:** whether Wise would grant permission for a personal, low-volume monitor; whether a first scan of ~195 pages would still be considered a disproportionate load.

### 4.6 Arm — `sitemap_html` adapter (profile `arm`) ⚠️

- **Intended requests:** `GET https://careers.arm.com/sitemap.xml`, then one `GET` per new or changed page under `https://careers.arm.com/job/…`.
- **`robots.txt`** (`https://careers.arm.com/robots.txt`): `User-agent: *` / `Disallow:/search-jobs/`. Nothing else is restricted; the adapter never requests the search path.
- **Terms** (`https://www.arm.com/company/policies/terms-and-conditions`, "Our Content"):
  > "You agree not to reproduce, duplicate, copy, sell, resell or exploit any portion of an Arm Website without our express written permission."
- **Automated access:** no robot/scraper/crawler/data-mining clause was found in the "Your use of an Arm Website" prohibited-conduct list.
- **Assessment:** nothing prohibits automated reading, but storing copies of role descriptions is arguably "reproduce, duplicate, copy … any portion of an Arm Website". The copyright exposure is about content retention, not access.
- **Open questions:** whether Arm accepts a personal monitor that stores role metadata; whether storing only title/location/URL and linking out is acceptable.

## 5. Cross-cutting observations

1. **`robots.txt` is not permission.** Four sources allow the paths the adapters use, and two of those (Palantir, Wise) prohibit the underlying behaviour in their terms. That is precisely the distinction the PRD and D-012 already require.
2. **Every source's terms engage on copying, not merely access.** Palantir, Barclays, Lloyds, Arm, and Wise all condition reproduction or storage of content on prior written permission. Job-Scout currently stores description text and salary in SQLite for deduplication, notification, and audit — behaviour that the copying clauses reach.
3. **The Workday adapters have the least textual cover.** Their endpoint family is outside the allow rules their hosts publish, they use `POST`, and the provider's own terms prohibit automated extraction and "applications that interact with our Sites" — with unproven scope over tenant hosts.
4. **Politeness is not the problem.** At a 30-minute cadence with one-request-at-a-time access and 0.5 s spacing, Job-Scout is far below what any of these documents describe as excessive load. The constraint is permission, not volume.
5. **The ATS provider and the employer are different parties.** Permission from Lever, Workday, or Greenhouse would not necessarily cover the employer's content, and vice versa. Any request for permission has to name both.

## 6. Options for the Product Owner

These are engineering options with their consequences, not a legal opinion.

| Option | Effect | Cost / risk |
|---|---|---|
| **A. Request written permission** for each source, in order Palantir → Monzo → Wise → Arm → Barclays → Lloyds | The only route that resolves the question | Time and silence; preparing a precise, low-volume, non-commercial description of the access |
| **B. Store metadata only** (title, employer, location, URL, dates; no description or salary) | Reduces copying exposure for Barclays, Lloyds, Arm and Monzo; leaves the Palantir and Wise monitoring prohibitions untouched | Loses description-based matching signals (currently unused by the matcher) and salary in alerts |
| **C. Ship with all six sources disabled** pending explicit configuration by the user | Keeps the repository honest and shifts the informed decision to each deployment | Every source becomes opt-in; the "first usable version" needs a setup step |
| **D. Drop Palantir and Wise from defaults** until permission exists | Removes the two sources whose terms prohibit the behaviour, leaving four engaged-but-silent sources | The current first usable version (Palantir) disappears; the product's demo path changes |
| **E. Proceed anyway on robots-compliance** | Maximum convenience today | Contradicts D-012 and the PRD's compliance requirement, and the product's own stated rule that reachability is not permission |

**Recommendation:** A plus B as the default posture — ask for permission, and meanwhile minimise stored content — with C as the shipping stance until answers arrive. Option D is the fallback if no permission is obtainable for Palantir or Wise, and it is a product decision, not an engineering one.

## 7. Sources consulted (retrieved 2026-10-23)

| Document | URL | Retrieved |
|---|---|---|
| Palantir terms and conditions of website use | https://www.palantir.com/terms-and-conditions/ | Yes |
| Lever public postings host robots | https://api.lever.co/robots.txt | Yes |
| Lever terms of service | https://www.lever.co/legal/terms-of-service | Yes (no scraping wording) |
| Barclays group terms of use | https://home.barclays/terms-of-use/ | Yes |
| Barclays UK site terms | https://www.barclays.co.uk/important-information/site-terms-conditions/ | Yes |
| Barclays Workday tenant robots | https://barclays.wd3.myworkdayjobs.com/robots.txt | Yes |
| Workday online terms of service | https://www.workday.com/en-us/legal/site-terms.html | Yes |
| Lloyds Banking Group site terms | https://www.lloydsbankinggroup.com/site-terms-and-conditions.html | Yes |
| Lloyds Workday tenant robots | https://lbg.wd3.myworkdayjobs.com/robots.txt | Yes |
| Monzo legal hub (redirects to product terms) | https://monzo.com/legal | Yes; no website terms found |
| Greenhouse public board API robots | https://boards-api.greenhouse.io/robots.txt | Yes |
| Greenhouse master subscription agreement | https://www.greenhouse.com/master-subscription-agreement | Yes |
| Wise personal terms of use | https://wise.com/gb/legal/terms-of-use-personal | Yes |
| Wise acceptable use policy | https://wise.com/gb/legal/acceptable-use-policy | Yes (no scraping wording) |
| Wise careers robots | https://wise.jobs/robots.txt | Yes |
| Arm terms and conditions | https://www.arm.com/company/policies/terms-and-conditions | Yes |
| Arm careers robots | https://careers.arm.com/robots.txt | Yes |
