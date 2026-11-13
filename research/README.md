# Employer research

This directory holds the durable record of who we investigated and what we found. It exists
because the batch-4–6 platform inventory — the most valuable artefact of that work — was written to
a temporary directory and survived by luck: `data/` is gitignored, so research kept there is
research that gets repeated.

## `employers.csv`

One row per employer, keyed by the id we would use in the catalogue.

| Column | Meaning |
|---|---|
| `employer` | stable id: the catalogue id for a shipped source, otherwise a slug |
| `name` | what appears in an alert |
| `platform` | `greenhouse`, `ashby`, `lever`, `workable`, `workday`, `recruitee`, `rippling`, … |
| `tenant` / `site` | the board, account or Workday tenant/site the employer publishes on |
| `status` | `added` (shipped), `candidate` (queued), `rejected` (with a reason), `deferred` |
| `reason` | required for `rejected`/`deferred`; why research must not be repeated |
| `roles`, `uk_roles`, `family_roles`, `matches` | measured evidence when a probe recorded it |
| `evidence_date` | when that evidence was gathered |

`added` rows are written from the catalogue itself, so the registry cannot claim an employer the
product does not ship; `tests/test_employer_registry.py` enforces that.

## Workflow

```bash
# 1. Discover: one request per employer to read the board it publishes on, then probe the boards.
python tools/probe_boards.py --input candidates.txt

# 2. Record what was found, including the candidates that were not added.
python tools/employer_registry.py seed --inventory employer-boards.json --date 2026-11-13

# 3. Only then write catalogue files, and confirm the two agree.
python tools/employer_registry.py check
python tools/employer_registry.py report
```

`seed` is idempotent and conservative: it never overwrites a verdict a human recorded, and for a
shipped source it keeps the connection details from that source's catalogue file.

## What the queue looks like today

`report` prints outstanding candidates by platform. At the last seed it was 74 Workday, 53 with no
public board found, 37 Workable, 20 Greenhouse, 6 Recruitee and 1 Rippling — which is the argument
for the Workday work in [v0.9](../docs/iterations/v0.9-scaling-to-1000.md) rather than for more
slug guessing.
