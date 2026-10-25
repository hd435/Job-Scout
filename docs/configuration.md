# Configuration reference

**Authority:** this file is the operator's reference for every configuration key. Product
intent lives in [prd.md](./prd.md), design in [architecture.md](./architecture.md), and the
reasons behind each option in [decisions.md](./decisions.md). Operational procedures are in
[runbook.md](./runbook.md).

Introduced by v0.5 increment 0.2 (D-073). Everything here is data: no Python edit is needed to
change sources, role families, seniority, employment types, locations or exclusions.

---

## 1. Where configuration lives

| Item | Default | Notes |
|---|---|---|
| Config file | `config.toml` in the working directory | Any path via `--config path/to/file.toml`. Absent means "built-in defaults" |
| Database | `data/jobscout.db` | Override with `[jobscout].database` or `--db` |
| Telegram credentials | `.env` or environment | `JOBSCOUT_TELEGRAM_TOKEN`, `JOBSCOUT_TELEGRAM_CHAT_ID`. Never in the config file |

Configuration is validated when it loads, so a typo fails immediately instead of silently
changing behaviour:

```powershell
python -m jobscout config show       # effective settings, sources, preferences, fingerprint
python -m jobscout config validate   # load and report problems, exit 2 if it will not load
```

`config validate` exits `0` when the file loads, and lists warnings for values that are valid
but can never take effect (an unknown adapter, the same employer configured twice, a pinned town
the city list does not know). An unloadable file exits `2`.

At scale, three views answer "what is happening?":

| Command | Answers |
|---|---|
| `jobscout sources list` | every source with adapter, cadence, last scan, matches, and whether it has been validated (and when) |
| `jobscout status --brief` | one line per source: state, consecutive failures, next run, match/review counts, and the error only if it is failing *now* |
| `jobscout status --source <id>` | the full block for one source |

`validate` writes `data/validation/<id>-latest.json` beside its timestamped artifacts, which is
what the `validated` column reads. A source never validated shows `never`; one whose last check
failed shows `FAILED`; a successful check older than 30 days is counted as stale.

## 2. `[jobscout]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `database` | string | `data/jobscout.db` | SQLite file. Relative paths resolve from the working directory |
| `catalogue` | string | none | Directory of employer files to use **instead of** the set shipped with the package. Relative paths resolve from the config file's directory |
| `disable` | array of ids | `[]` | Turn catalogue sources off without editing the catalogue files |

## 3. The employer catalogue (D-081)

Employers are files, not code. The package ships one per employer in `src/jobscout/catalogue/`
— the six researched employers plus expansion batch 1; `jobscout sources list` prints the current
set. Adding the next one is one more file.

A catalogue file describes exactly one employer:

```toml
# Optional comment explaining the feed, and anything a reviewer should know.
id = "example"
employer = "Example Ltd"
adapter = "greenhouse"        # lever | greenhouse | workday | sitemap_html
access_note = "public Greenhouse job-board API, one GET per scan, no auth"   # required
cadence_minutes = 30          # optional, default 30
enabled = true                # optional, default true

[options]
board = "example"
uk_employer = true
```

`access_note` is the recorded access basis for that employer (D-087, satisfying AC-19): one line
naming the public endpoint the source reads and the request hygiene applied to it — one request
per scan, page caps, request spacing, a search narrowing. It is the *recording* of the posture the
Product Owner accepted for this personal-use deployment (D-066), not a per-source permission
decision, and nothing in it claims a site's terms permit automated access.

Rules, so the list stays predictable at a hundred entries:

- One employer per file, and the file must not contain a `[[sources]]` list.
- Files are read in filename order, so the source list is alphabetical and stable.
- Every file is validated when the configuration loads: a bad option fails with the file name,
  the source id and the key.
- A missing `access_note` is an error naming the file, and an unknown top-level key is an error
  listing the accepted ones — a typo like `cadence_minute` must not silently become a default.
- Two files claiming the same `id` are an error naming both files.

**Which list applies.** The bundled catalogue is the default. `[jobscout] catalogue = "sources"`
uses your own directory *instead* — it is your list, not an addition to the shipped one. A
`[[sources]]` block in the config file still replaces whatever list applies, which is what a config
written before v0.6 means and keeps meaning. `disable = ["wise", "arm"]` narrows any of those lists
without touching the files. `config.example.toml` in the repository root is a commented, working
starting point for all of section 4:

```toml
[jobscout]
database = "data/jobscout.db"
catalogue = "sources"
disable = ["wise"]
```

`jobscout sources list` prints every source that results, with its adapter, state and — from
`validate` — whether it has ever been checked.

## 3a. `[[sources]]` in the config file

One table per employer, for an override or a one-off experiment. Listing any `[[sources]]` block
**replaces** the catalogue, so keep every source you want to run.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `id` | string | required | Stable identifier used by `--source`, the database and the report |
| `employer` | string | required | Display name used in alerts and reports |
| `adapter` | string | required | `lever`, `greenhouse`, `workday`, or `sitemap_html` |
| `enabled` | bool | `true` | Disabled sources are kept in config but never scanned or counted against targets |
| `cadence_minutes` | int | `30` | Minimum gap between scans of this source |
| `access_note` | string | `""` | Recorded access basis (D-087). Optional here; `config validate` names any that omit it |
| `[sources.options]` | table | `{}` | Adapter-specific keys below |

### 3.1 Adapter options

Every key below is validated when the configuration loads: an option the adapter does not read,
or a required option that is missing, is an error naming the source and the key
(`src/jobscout/source_options.py` is the single source of truth).

| Adapter | Required options | Optional |
|---|---|---|
| `lever` | — | `site` (defaults to the source id), `uk_employer` |
| `greenhouse` | — | `board` (defaults to the source id), `uk_employer` |
| `workday` | `host`, `tenant`, `site` | `locale`, `page_size`, `max_pages`, `search_text`, `uk_employer` |
| `sitemap_html` | `job_url_pattern` and `sitemap_urls` (or `sitemap`) | `sitemap_child_pattern`, `location_from`, `max_sitemap_documents`, `max_detail_fetches`, `request_delay_seconds`, `uk_employer` |

A `[[sources]]` block may omit `access_note` — it is the documented way to try one source, and a
missing note is a documentation gap rather than a reason not to run — but `jobscout config
validate` lists every enabled source that has not recorded one.

`uk_employer = true` marks a source whose board only advertises UK roles. It relaxes the
town-name collision check (Manchester, Halifax, and other names that exist abroad) so a UK-only
board does not leave those roles in review for ever. It never makes a non-UK location qualify
(D-065).

### 3.2 The sitemap/JSON-LD adapter (D-076)

One adapter covers any careers site that publishes a sitemap of job URLs whose pages carry a
schema.org `JobPosting` JSON-LD block. A new employer of that shape needs no Python change.

| Key | Meaning |
|---|---|
| `sitemap_urls` | Array of sitemap URLs. A sitemap **index** is followed one level, so list the index |
| `sitemap` | Single sitemap URL; shorthand for a one-entry `sitemap_urls` |
| `sitemap_child_pattern` | Regex; when an index lists several child sitemaps, only matching children are read. Omit to read all of them |
| `job_url_pattern` | **Required.** Regex matched against each sitemap URL. It must contain a named group `(?P<id>...)`, which becomes the stable source job id. Only matching URLs are ever requested |
| `location_from` | `"url"` takes the town from a named group `(?P<location>...)` in `job_url_pattern`, for pages that publish no JSON-LD address. Hyphens become spaces |
| `max_sitemap_documents` | Cap on sitemap documents per scan (default 5) |
| `max_detail_fetches` | Cap on job pages fetched per scan (default 400) |
| `request_delay_seconds` | Pause between job-page requests (default 0.5). Raise it for a board of several hundred pages |

### 3.3 Workday search (`search_text`)

`search_text` is passed to the site's own search, exactly as the search box would, and the
listing's reported total reflects it — so "complete" means complete for that search. Use it when a
board is much larger than the early-career subset you want: Barclays' external site holds thousands
of postings, and `search_text = "graduate"` brings a scan down to about 90. The trade is recall: a
role whose title never contains the search term is never seen.

Behaviour worth knowing before adding a site:

- A role's `lastmod` is its revision. An unchanged revision is not re-fetched; the role is
  recorded as observed only, which keeps a 400-role board cheap after the first scan.
- A page with no JSON-LD still yields a title from its heading. Its location stays unknown (or
  comes from the URL with `location_from = "url"`), which keeps the role in review rather than
  guessing.
- A scan that matches no URLs, or hits a cap, is `partial`, not `success`. Partial scans never
  establish a baseline, never alert, and never close roles.
- A detail request that fails transiently is retried once. A URL that returns 404 is treated as a
  removed role and the scan can still complete, because a stale sitemap entry or one dropped
  connection must not stop a source from ever baselining (D-079). If every URL 404s and nothing is
  confirmed unchanged, the scan fails: an empty "complete" scan would close roles.
- The first scan of a large board is the expensive one. With `max_detail_fetches = 400` a 434-role
  board needs two passes: the first is `partial` (capped), the second fetches the remainder and
  completes. After that only changed roles are re-fetched.
- TOML interprets backslashes inside double-quoted strings. Write a regex in single quotes
  (`'^https://example.test/jobs/(?P<id>\\d+)$'`) or double every backslash.

A source whose *kind* of access is none of those four shapes still needs an adapter.

## 4. `[preferences]`

The user's job-search scope. Omitting the whole table gives the shipped defaults, which are the
pre-0.5 behaviour exactly; naming the defaults explicitly is detected and the rules identifier
stays `m1.1`.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `role_families` | array of strings | all six approved families | Families that may alert. Unknown or empty is rejected |
| `employment_types` | array of strings | the five early-career classes | Classes that may alert. `unknown` cannot be chosen; empty is rejected |
| `seniority_exclusions` | array of strings | the approved list | Replaces the approved list. `[]` means "allow senior titles" |
| `title_exclusions` | array of strings | `[]` | Title phrases that exclude a role outright |
| `[preferences.locations]` | table | `mode = "uk"` | Location scope, section 4.2 |

Approved role families: `data`, `software`, `quant`, `finance`, `consulting`,
`business_analysis`. Unrelated families (sales, marketing, HR, legal, accounting, design) are
excluded by code and cannot be enabled by configuration.

### 4.1 Employment-type classes (D-068)

| Class | Recognised by | Alerts by default |
|---|---|---|
| `graduate_scheme` | "graduate" in the title, scheme/programme wording in title or description | yes |
| `internship` | intern, internship, summer analyst | yes |
| `placement` | industrial placement, placement year, year in industry | yes |
| `apprenticeship` | apprentice, apprenticeship | yes |
| `entry_level_permanent` | junior, trainee, campus, entry-level, early career, or a description signal | yes |
| `experienced_hire` | an explicit multi-year experience requirement | **no, always reviewed** |
| `unknown` | nothing recognisable | **no, always reviewed** |

The classifier uses only what a source exposes — title and description — so it is a documented
heuristic. A title-based class always wins; an explicit "3+ years of experience" then outranks a
soft description phrase. `unknown` is not a legal preference value and is never alerted.

A class you remove from `employment_types` is excluded with `exclude:employment_type:<class>`.
`experienced_hire` and `unknown` cannot be excluded: they are reported for review, because that
list is the operator's false-positive and missed-match log.

### 4.2 Location scope (D-069)

| `mode` | Qualifies |
|---|---|
| `uk` (default) | Any London or other UK location, UK remote, UK hybrid |
| `pinned` | Only the listed `cities`, plus UK remote and UK hybrid when those toggles are on |
| `uk_plus_pinned` | Anything `uk` qualifies, plus the listed cities outside the UK |

| Key | Type | Default | Meaning |
|---|---|---|---|
| `cities` | array of strings | `[]` | Required and non-empty when `mode = "pinned"` |
| `remote_uk` | bool | `true` | Include roles explicitly remote within the UK |
| `hybrid_uk` | bool | `true` | Include UK hybrid roles that name no city |

City names are resolved through the built-in city list, so a pinned `Manchester` still needs a
country or a `uk_employer` source before it can qualify — pinning does not override ambiguity.
A name the list does not know (for example `Bournemouth`) is matched against the raw location
text instead, so a finite city list cannot make a configured preference silently ineffective;
`config show` marks those as "text match only".

Excluded locations are reported, not hidden: `exclude:location:not_pinned`,
`exclude:location:remote_not_selected`, `exclude:location:hybrid_not_selected`.

### 4.3 Operator scope for the personal beta

Kept in the gitignored `data/ops/config.toml`, not in repository defaults, so a clone is never
silently narrowed:

```toml
[preferences.locations]
mode = "pinned"
cities = ["London"]
remote_uk = true
hybrid_uk = true
```

This is why the beta alerts on London and UK-remote roles only, and why a Manchester-only or
Edinburgh-only role is listed as excluded rather than alerted.

## 4a. `[pacing]` (D-082)

One pacer per process, shared by every source, so coverage can grow without bursts.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `default_seconds` | number | `0.5` | Minimum gap between two requests to the same host, whichever source makes them |
| `max_requests` | integer | `5000` | Hard ceiling on requests per process; a scan that hits it fails loudly instead of crawling on |
| `[pacing.hosts]` | table | `{}` | Per-host overrides, by hostname only (no scheme, no port) |

```toml
[pacing]
default_seconds = 0.5
max_requests = 5000

[pacing.hosts]
"careers.arm.com" = 1.0
```

Two different mechanisms pace traffic, and they are not duplicates:

- `sources.options.request_delay_seconds` paces **one source's own** page fetches, inside the
  adapter. Raise it for a board with hundreds of pages.
- `[pacing]` guarantees a floor **between any two requests to the same host**, whatever source
  makes them. It is what stops two tenants on one ATS host, or a board and a sitemap on one
  domain, from being scanned back to back.

A budget stop is reported as a failed scan with the budget in the message, not as a partial
scan, because it is a configuration fault rather than one bad page.

## 5. `[matching]`

Additive terms inside an approved family, and the legacy title-exclusion key.

| Key | Type | Meaning |
|---|---|---|
| `extra_family_terms` | table of family = array | Extra phrases for an approved family |
| `extra_exclude_terms` | array | Extra title exclusions; equivalent to `[preferences].title_exclusions` |

Terms must be at least two characters and contain a letter or digit. Unknown families and keys
are rejected. The distinction from `[preferences]` matters: `[matching]` adds phrases to a
category that has already been reviewed, while `[preferences]` chooses among categories.

## 6. The rules identifier

`build_rules()` fingerprints the active scope — additive terms, allowed families, seniority,
employment types and the location scope — into a short digest. The default scope keeps the plain
`m1.1` identifier; anything else becomes `m1.1+<digest>`.

That identifier is stored with every match decision (`jobs.rules_version`), so a stored decision
can always be explained by the exact scope that produced it. `jobscout config show` prints both
the identifier and the preference-only fingerprint.

When you change a preference, the next scan re-decides every role it sees. Baselines and
existing alert history are untouched: no role is re-alerted because the scope changed.

## 7. Validation errors you may meet

| Message | Cause |
|---|---|
| `unknown [preferences] key(s): …` | A misspelled key. The message lists the allowed set |
| `[preferences].role_families must be an array of strings` | A bare string instead of an array |
| `unknown role family 'legals' (approved: …)` | A family outside the approved six |
| `employment_types cannot include 'unknown'` | `unknown` is never alert-eligible |
| `[preferences.locations].mode = 'london' is not one of pinned, uk, uk_plus_pinned` | A mode typo |
| `[preferences.locations].mode = 'pinned' needs at least one entry in …cities` | Pinned with no cities |
| `[preferences.locations].remote_uk must be true or false` | Quoted boolean |
| `configuration file not found: …` | `--config` points at a missing file |

## 8. What is deliberately not configurable

- The approved keyword lists themselves (D-026 stays open; additions are data, new categories are not).
- The unrelated families that are excluded by code.
- Which notification states exist, deduplication rules, baseline rules, or closure rules.
- Retention horizons (ratified as implemented, D-067).
- Anything requiring code for a new *kind* of source access (increment 0.3).
