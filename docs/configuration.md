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

## 2. `[jobscout]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `database` | string | `data/jobscout.db` | SQLite file. Relative paths resolve from the working directory |

## 3. `[[sources]]`

One table per employer. Listing any `[[sources]]` block **replaces** the built-in list, so keep
every source you want to run.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `id` | string | required | Stable identifier used by `--source`, the database and the report |
| `employer` | string | required | Display name used in alerts and reports |
| `adapter` | string | required | `lever`, `greenhouse`, `workday`, or `sitemap_html` |
| `enabled` | bool | `true` | Disabled sources are kept in config but never scanned or counted against targets |
| `cadence_minutes` | int | `30` | Minimum gap between scans of this source |
| `[sources.options]` | table | `{}` | Adapter-specific keys below |

### 3.1 Adapter options

| Adapter | Required options | Optional |
|---|---|---|
| `lever` | `site` (board name) | — |
| `greenhouse` | `board` | — |
| `workday` | `host`, `tenant`, `site` | `locale`, `page_size`, `max_detail_lookups`, `uk_employer` |
| `sitemap_html` | `profile` (in-code profile name: `wise` or `arm`) | `sitemap`, `max_detail_fetches`, `request_delay_seconds`, `uk_employer` |

`uk_employer = true` marks a source whose board only advertises UK roles. It relaxes the
town-name collision check (Manchester, Halifax, …) so a UK-only board does not leave those roles
in review for ever. It never makes a non-UK location qualify (D-065).

Adding an employer whose *kind* of access is not in that table still needs an adapter. That is
increment 0.3, not configuration.

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
