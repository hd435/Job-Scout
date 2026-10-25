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

#: Adapters whose whole board arrives in one response, so one probe request is honest.
PROBEABLE = ("greenhouse", "ashby", "lever", "workable")


def parse_candidates(path: Path) -> list[tuple[str, str, str]]:
    candidates: list[tuple[str, str, str]] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 3:
            raise SystemExit(f"bad candidate line (want adapter,slug,employer): {raw!r}")
        adapter, slug, employer = parts
        if adapter not in PROBEABLE:
            raise SystemExit(f"adapter {adapter!r} is not probeable in one request ({PROBEABLE})")
        candidates.append((adapter, slug, employer))
    return candidates


def source_for(adapter: str, slug: str, employer: str) -> SourceConfig:
    if adapter in ("greenhouse", "ashby"):
        options = {"board": slug}
    elif adapter == "workable":
        options = {"account": slug}
    else:
        options = {"site": slug}
    return SourceConfig(
        id=slug,
        employer=employer,
        adapter=adapter,
        options=options,
        access_note="probe",
    )


def probe(adapter: str, slug: str, employer: str) -> dict[str, Any]:
    source = source_for(adapter, slug, employer)
    result = get_adapter(adapter).fetch_scan(source, UrllibHttpClient())
    record: dict[str, Any] = {
        "adapter": adapter,
        "slug": slug,
        "employer": employer,
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
    return record


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
    for index, (adapter, slug, employer) in enumerate(candidates, start=1):
        try:
            record = probe(adapter, slug, employer)
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
                f"{index:>3} {adapter:<10} {slug:<24} {record['status']:<13}"
                f" roles={record['records_parsed']:<4} uk={record['uk_roles']:<4}"
                f" fam={record['family_roles']:<4} early={record['early_roles']:<3}"
                f" both={record['early_family_roles']:<3} match={record['matches']:<2}"
                f" {', '.join(record['early_titles'][:2])[:60]}"
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

    keep = sorted(
        (r for r in records if r.get("uk_roles", 0) >= 1),
        key=lambda r: (
            r.get("early_family_roles", 0),
            r.get("family_roles", 0),
            r.get("uk_roles", 0),
        ),
        reverse=True,
    )
    print(f"\n{len(keep)} board(s) with at least one UK role, best first:")
    for record in keep:
        print(
            f"  {record['adapter']:<10} {record['slug']:<24} uk={record['uk_roles']:<4}"
            f" fam={record['family_roles']:<4} early={record['early_roles']:<3}"
            f" both={record['early_family_roles']:<3} {record['employer']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
