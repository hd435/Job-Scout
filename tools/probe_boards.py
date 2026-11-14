"""Probe candidate employer boards and report what each would contribute.

Operator tooling for catalogue expansion (runbook section 7b), not part of the package: it
talks to real public job boards and prints a summary so a candidate can be judged before it is
added to `src/jobscout/catalogue/`.

Input is a text file of `adapter,slug,employer` lines (`#` starts a comment), for example:

    greenhouse,monzo,Monzo
    ashby,linear,Linear
    lever,palantir,Palantir Technologies

For each candidate one request is made (these adapters return the whole board in one response).
The output is one line per candidate plus a JSON file with the detail, so the decision to add a
source can be based on measured evidence rather than a hunch.

Output per candidate: roles seen, roles whose location qualifies under the product's own rules,
roles that would alert under the default rules, roles that would need review, and example titles.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobscout.config import SourceConfig  # noqa: E402
from jobscout.http import UrllibHttpClient  # noqa: E402
from jobscout.matching import (  # noqa: E402
    DEFAULT_RULES,
    ROLE_FAMILIES,
    UNKNOWN_EMPLOYMENT_TYPE,
    classify_employment_type,
    match_job,
    normalize_text,
)
from jobscout.models import MatchOutcome, ScanStatus  # noqa: E402
from jobscout.sources import get_adapter  # noqa: E402

#: Adapters a probe can read directly. Greenhouse, Ashby, Lever and Workable answer in one
#: request; Workday pages, so it is probed with a bounded number of pages and no detail lookups
#: (enough to judge the board, not to build a complete scan).
PROBEABLE = (
    "greenhouse",
    "ashby",
    "lever",
    "workable",
    "recruitee",
    "smartrecruiters",
    "workday",
)

#: Workday probe limits: ten listing pages is 200 postings, which is more than enough to judge
#: whether a tenant is worth enrolling and keeps one candidate to a bounded number of requests.
WORKDAY_PROBE_PAGES = 10


def parse_candidates(path: Path) -> list[tuple[str, str, str, str]]:
    """Parse `adapter,slug,employer[,search_text]` lines.

    A Workday slug is one path-like field: `host/tenant/site`, e.g.
    `lseg.wd3.myworkdayjobs.com/LSEG/Careers`. The optional fourth field is the site's own
    search text, which is how a large group board is narrowed before it is judged.
    """
    candidates: list[tuple[str, str, str, str]] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) not in (3, 4):
            raise SystemExit(
                f"bad candidate line (want adapter,slug,employer[,search_text]): {raw!r}"
            )
        adapter, slug, employer = parts[:3]
        search_text = parts[3] if len(parts) == 4 else ""
        if adapter not in PROBEABLE:
            raise SystemExit(
                f"adapter {adapter!r} cannot be probed (supported: {', '.join(PROBEABLE)})"
            )
        if adapter == "workday" and slug.count("/") != 2:
            raise SystemExit(
                f"a workday slug must be host/tenant/site, e.g."
                f" lseg.wd3.myworkdayjobs.com/LSEG/Careers — got {slug!r}"
            )
        candidates.append((adapter, slug, employer, search_text))
    return candidates


def source_for(adapter: str, slug: str, employer: str, search_text: str = "") -> SourceConfig:
    if adapter in ("greenhouse", "ashby"):
        options: dict[str, Any] = {"board": slug}
        source_id = slug
    elif adapter in ("workable", "recruitee"):
        options = {"account": slug}
        source_id = slug
    elif adapter == "workday":
        host, tenant, site = slug.split("/")
        options = {"host": host, "tenant": tenant, "site": site, "locale": "en-US"}
        if search_text:
            options["search_text"] = search_text
        source_id = tenant
    else:
        options = {"site": slug}
        source_id = slug
    return SourceConfig(
        id=source_id,
        employer=employer,
        adapter=adapter,
        options=options,
        access_note="probe",
    )


def adapter_for(adapter: str) -> Any:
    if adapter == "workday":
        from jobscout.sources.workday import WorkdayAdapter

        return WorkdayAdapter(max_pages=WORKDAY_PROBE_PAGES, max_detail_lookups=0)
    return get_adapter(adapter)


def probe(adapter: str, slug: str, employer: str, search_text: str = "") -> dict[str, Any]:
    source = source_for(adapter, slug, employer, search_text)
    result = adapter_for(adapter).fetch_scan(source, UrllibHttpClient())
    record: dict[str, Any] = {
        "adapter": adapter,
        "slug": slug,
        "employer": employer,
        "search_text": search_text,
        "status": str(result.status),
        "records_seen": result.records_seen,
        "records_parsed": result.records_parsed,
        "error": result.error_summary,
        "uk_roles": 0,
        "matches": 0,
        "review": 0,
        "family_roles": 0,
        "early_roles": 0,
        "early_family_roles": 0,
        "reasons": {},
        "uk_titles": [],
        "early_titles": [],
        "early_examples": [],
    }
    if result.status is not ScanStatus.FAILED:
        _classify(result, record)
    record["uk_share"] = (
        round(record["uk_roles"] / record["records_parsed"], 3) if record["records_parsed"] else 0.0
    )
    record["verdict"], record["verdict_reason"] = verdict(record)
    return record


#: A tenant is worth enrolling when a meaningful share of what it publishes is in the UK, it
#: publishes early-career roles in an enabled family, and it is not a global board dressed up
#: with a `graduate` search. Workday wave 1 learned the last one the expensive way: HP returned
#: 333 postings and 4 UK roles, MUFG 36 and 2, P&G 218 and 9.
MIN_UK_SHARE = 0.10


def verdict(record: dict[str, Any]) -> tuple[str, str]:
    """`enrol`, `defer` or `reject`, with the reason recorded so the decision is reproducible."""
    if record["status"] in ("failed", "error"):
        return "reject", f"scan failed: {(record.get('error') or '')[:80]}"
    if not record["records_parsed"]:
        return "reject", "board returned no roles"
    if not record["uk_roles"]:
        return "reject", "no UK roles at all"
    if record["uk_share"] < MIN_UK_SHARE:
        return (
            "defer",
            f"only {record['uk_roles']} UK of {record['records_parsed']}"
            f" ({record['uk_share']:.0%}), below the {MIN_UK_SHARE:.0%} bar",
        )
    if not record["early_family_roles"]:
        return "defer", "no UK early-career role in an enabled family"
    return (
        "enrol",
        f"{record['early_family_roles']} UK early-career family role(s),"
        f" {record['uk_share']:.0%} of the board is UK",
    )


def _classify(result: Any, record: dict[str, Any]) -> None:
    uk = [job for job in result.jobs if job.location_normalized.qualifies]
    record["uk_roles"] = len(uk)
    reasons: Counter[str] = Counter()
    for job in uk:
        decision = match_job(job, DEFAULT_RULES)
        if decision.reasons:
            reasons[decision.reasons[0]] += 1
        if decision.decision is MatchOutcome.MATCH:
            record["matches"] += 1
            if len(record["early_titles"]) < 6:
                record["early_titles"].append(job.title)
        elif decision.decision is MatchOutcome.NEEDS_REVIEW:
            record["review"] += 1
        if len(record["uk_titles"]) < 25:
            record["uk_titles"].append(job.title)

        # Board quality, independent of today's seniority mix: does this board publish roles
        # in our families, and does it publish early-career roles at all?
        title = normalize_text(job.title)
        family = any(term in title for stem in ROLE_FAMILIES.values() for term in stem)
        employment = classify_employment_type(job.title, job.description or "")
        early = employment not in (UNKNOWN_EMPLOYMENT_TYPE, "experienced_hire")
        if family:
            record["family_roles"] += 1
        if early:
            record["early_roles"] += 1
            if len(record["early_examples"]) < 6:
                record["early_examples"].append(f"{job.title} [{employment}]")
        if family and early:
            record["early_family_roles"] += 1
    record["reasons"] = dict(reasons.most_common(6))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="candidate list file")
    parser.add_argument("--out", type=Path, help="JSON detail output (default: next to input)")
    parser.add_argument("--sleep", type=float, default=0.2, help="seconds between candidates")
    args = parser.parse_args()

    candidates = parse_candidates(args.input)
    out = args.out or args.input.with_suffix(".json")
    print(f"probing {len(candidates)} candidate board(s)")

    records: list[dict[str, Any]] = []
    counters: Counter[str] = Counter()
    for index, (adapter, slug, employer, search_text) in enumerate(candidates, start=1):
        try:
            record = probe(adapter, slug, employer, search_text)
        except Exception as exc:  # a broken candidate must not stop the batch
            record = {
                "adapter": adapter,
                "slug": slug,
                "employer": employer,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        counters[record["status"]] += 1
        records.append(record)
        if record["status"] in ("failed", "error"):
            print(f"{index:>3} {adapter:<10} {slug:<24} FAILED  {(record.get('error') or '')[:70]}")
        else:
            print(
                f"{index:>3} {adapter:<10} {slug[:30]:<30} {record['status']:<13}"
                f" roles={record['records_parsed']:<4} uk={record['uk_roles']:<4}"
                f" both={record['early_family_roles']:<3} match={record['matches']:<2}"
                f" {record.get('verdict', ''):<7} {(record.get('verdict_reason') or '')[:60]}"
            )
        time.sleep(args.sleep)

    payload = {
        "probed_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "counters": dict(counters),
        "records": records,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\n{out}")

    enrolling = [record for record in records if record.get("verdict") == "enrol"]
    enrolling.sort(
        key=lambda r: (r.get("early_family_roles", 0), r.get("matches", 0)), reverse=True
    )
    print(f"\n{len(enrolling)} board(s) worth enrolling, best first:")
    for record in enrolling:
        print(
            f"  {record['adapter']:<10} {record['slug']:<30} uk={record['uk_roles']:<4}"
            f" both={record['early_family_roles']:<3} match={record['matches']:<2}"
            f" {record['employer']}"
        )
    deferred = [record for record in records if record.get("verdict") == "defer"]
    if deferred:
        print(f"\n{len(deferred)} deferred (recorded so the research is not repeated):")
        for record in deferred:
            print(f"  {record['slug']:<30} {record['verdict_reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
