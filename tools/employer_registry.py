"""Employer registry: the durable record of who we looked at and what happened.

Why this exists
---------------
Batch 4–6 identified the platform behind 214 UK early-career employers, then kept the inventory in
a temporary directory because `data/` is gitignored. Research that is not committed is research
that gets repeated. This tool turns that knowledge into one diffable table,
`research/employers.csv`, and cross-checks it against the shipped catalogue.

Usage
-----
    python tools/employer_registry.py seed --inventory inventory.json
    python tools/employer_registry.py check
    python tools/employer_registry.py report

`seed` is idempotent: it never overwrites a status a human set (`rejected`, `deferred`) and never
invents measurements it does not have. `check` exits non-zero when the registry and the catalogue
disagree, which is the drift the registry exists to prevent.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobscout.config import DEFAULT_SOURCES  # noqa: E402

REGISTRY = Path(__file__).resolve().parent.parent / "research" / "employers.csv"

FIELDS = (
    "employer",
    "name",
    "platform",
    "tenant",
    "site",
    "status",
    "reason",
    "roles",
    "uk_roles",
    "family_roles",
    "matches",
    "evidence_date",
)

#: Employers investigated and deliberately not enrolled, with the reason (runbook §7c).
REJECTED = {
    "rolls_royce": (
        "intern_graduate site is global: 94 postings, 7 UK, 0 matches, "
        "94 detail requests per scan mostly for non-UK roles"
    ),
    "andurilindustries": "2 511 postings for one UK early-career match; disproportionate scan cost",
    "canonical": "314 postings, 2 in a target family, none with an early-career signal",
    "hr_wallingford": "4 postings, 1 in a target family, none with an early-career signal",
    "thirdbridge": "35 UK early-career roles but only 1 in a target family",
    "ctccampusboard": "campus/aggregator board, not an employer",
    "mwinternshipprogram": "campus/aggregator board, not an employer",
    "checkout": "board token 404; uses a different ATS",
    "revolut": "board token 404; careers site is CAPTCHA-gated",
    # Workday wave 1: enrolled, measured, then withdrawn. A global tenant narrowed by a
    # `graduate` search still returns mostly non-UK roles, so the cost is per-scan noise.
    "hp": (
        "Workday group board: 333 postings in the graduate search, 4 UK, 0 matches. "
        "Deferred rather than scanned; revisit if a UK or campus site is published"
    ),
    "mufg": (
        "Workday group board returns world-wide roles: 36 postings, 2 UK, 0 matches. "
        "Revisit if a UK graduate site is published"
    ),
    "pg": (
        "Workday group board: 218 postings in the graduate search, 9 UK, 0 matches. P&G names "
        "graduate-entry roles 'Manager', which the seniority exclusions reject; revisit if that "
        "naming changes or a UK careers site appears"
    ),
}

#: Platforms we can read, and the option each uses.
PLATFORM_OPTION = {
    "greenhouse": "board",
    "ashby": "board",
    "lever": "site",
    "workable": "account",
}


def read_registry(path: Path = REGISTRY) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_registry(rows: list[dict[str, str]], path: Path = REGISTRY) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows, key=lambda row: (row["status"], row["employer"]))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in ordered:
            writer.writerow({key: row.get(key, "") for key in FIELDS})


def catalogue_rows(evidence_date: str) -> list[dict[str, str]]:
    """One row per catalogued employer, with the platform it actually uses."""
    rows: list[dict[str, str]] = []
    for source in DEFAULT_SOURCES:
        platform = source.adapter
        tenant = ""
        site = ""
        if platform in PLATFORM_OPTION:
            tenant = str(source.options.get(PLATFORM_OPTION[platform]) or source.id)
        elif platform == "workday":
            tenant = str(source.options.get("tenant") or "")
            site = str(source.options.get("site") or "")
        elif platform == "sitemap_html":
            tenant = str(source.options.get("sitemap_url") or "")
        rows.append(
            {
                "employer": source.id,
                "name": source.employer,
                "platform": platform,
                "tenant": tenant,
                "site": site,
                "status": "added",
                "reason": "",
                "evidence_date": evidence_date,
            }
        )
    return rows


def inventory_rows(path: Path, evidence_date: str) -> list[dict[str, str]]:
    """Rows from a batch-4 style inventory: employer name plus the board URL it publishes on."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, str]] = []
    for record in payload:
        board = record.get("board") or ""
        platform = record.get("host") or ""
        if "myworkdayjobs" in board:
            platform = "workday"
        elif "greenhouse" in board:
            platform = "greenhouse"
        elif "workable" in board:
            platform = "workable"
        elif "recruitee" in board:
            platform = "recruitee"
        elif "rippling" in board:
            platform = "rippling"
        tenant = ""
        site = ""
        if platform == "workday" and board:
            parsed = urlparse(board if "://" in board else f"https://{board}")
            tenant = parsed.hostname.split(".")[0] if parsed.hostname else ""
            site = parsed.path.strip("/").split("/")[0] if parsed.path.strip("/") else ""
        elif board:
            tenant = board.rstrip("/").split("/")[-1]
        rows.append(
            {
                "employer": slugify(record.get("company", "")),
                "name": record.get("company", "").strip(),
                "platform": platform,
                "tenant": tenant,
                "site": site,
                "status": "candidate",
                "reason": "" if board else "no public board found on the published job page",
                "evidence_date": evidence_date,
            }
        )
    return rows


def slugify(name: str) -> str:
    """A stable id that merges with catalogue ids: "P&G" and "P&G" must not become two rows."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", name.strip().casefold()).strip("_")
    return cleaned or "unknown"


def merge(existing: list[dict[str, str]], incoming: list[dict[str, str]]) -> list[dict[str, str]]:
    """Add or refresh rows without discarding a verdict a human recorded."""
    by_id = {row["employer"]: row for row in existing}
    for row in incoming:
        current = by_id.get(row["employer"])
        if current is None:
            by_id[row["employer"]] = row
            continue
        # Keep a human verdict and its reason; refresh everything measured.
        if current.get("status") in ("rejected", "deferred"):
            continue
        merged = dict(current)
        for key in ("name", "platform", "evidence_date"):
            if row.get(key):
                merged[key] = row[key]
        if current.get("status") != "added":
            # A shipped source's connection details come from its catalogue file, not from a
            # discovery pass that may have found a different board for the same employer.
            for key in ("tenant", "site"):
                if row.get(key):
                    merged[key] = row[key]
            merged["status"] = row["status"]
            merged["reason"] = row.get("reason", "")
        by_id[row["employer"]] = merged
    return list(by_id.values())


#: Employers probed and deliberately not enrolled yet, with the measurement that decided it.
#: Deferred rather than rejected: the platform may publish early-career roles seasonally.
DEFERRED = {
    # Workday wave 2 (2026-11-13). Mostly UK-heavy tenants whose boards publish operational or
    # professional roles rather than the enabled families.
    "reed": "4 postings in the graduate search, 3 UK, none in an enabled family",
    "jll": "123 postings, 4 UK (3 %); UK share below the 10 % bar",
    "knightfrank": "3 postings, 1 UK, none in an enabled family",
    "unitedliving": "200 postings, 65 UK, none in an enabled family (site, quantity surveying)",
    "brompton": "10 postings, 7 UK, none in an enabled family",
    "slrconsulting": "18 postings, 3 UK, none in an enabled family",
    "sulzer": "200 postings, 15 UK (8 %); UK share below the 10 % bar",
    "jcdecaux": "14 postings, 11 UK, none in an enabled family",
    "arriva": "170 postings, 41 UK, none in an enabled family (operations and depot roles)",
    "bishopfleming": "13 postings, 2 UK, none in an enabled family",
    "clydeco": "10 postings, 4 UK, none in an enabled family (trainee solicitor is not a family)",
    "linklaters": "graduate search returned 6 postings, none in the UK",
    "lseg": "probe failed with HTTP 502; retry before judging (transient, not a verdict)",
}


def apply_statuses(
    rows: list[dict[str, str]], evidence_date: str
) -> list[dict[str, str]]:
    """Apply recorded rejections and deferrals, keeping any connection detail already found."""
    by_slug = {slugify(row["employer"]): row for row in rows}
    by_id = {row["employer"]: row for row in rows}
    dropped: set[str] = set()
    entries = [(employer, reason, "rejected") for employer, reason in REJECTED.items()]
    entries += [(employer, reason, "deferred") for employer, reason in DEFERRED.items()]
    for employer, reason, status in entries:
        target = by_id.get(employer) or by_slug.get(slugify(employer))
        if target is None:
            target = {
                "employer": employer,
                "name": employer.replace("_", " ").title(),
                "platform": "",
                "tenant": "",
                "site": "",
            }
            by_id[employer] = target
        elif target["employer"] != employer:
            # The discovery pass spelled the name differently ("P&G" -> p_g); the verdict
            # belongs to the catalogue id, so fold the alias away rather than keep both.
            dropped.add(target["employer"])
        target["status"] = status
        target["reason"] = reason
        target["evidence_date"] = evidence_date
    return [row for row in by_id.values() if row["employer"] not in dropped]


def apply_measurements(rows: list[dict[str, str]], db: Path) -> list[dict[str, str]]:
    """Fill each shipped row with what it actually contributed, from the scan database.

    Contribution is the measure that matters (D-106): an employer that stores hundreds of roles
    and matches none is recorded as such rather than counted as coverage.
    """
    import sqlite3

    if not db.exists():
        print(f"warning: {db} not found, contributions left blank", file=sys.stderr)
        return rows
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    measured: dict[str, tuple[int, int, int, int]] = {}
    try:
        for source_id, roles, uk, family, matches in connection.execute(
            """
            SELECT source_id,
                   count(*),
                   sum(CASE WHEN location_normalized_json LIKE '%"qualifies": true%'
                            THEN 1 ELSE 0 END),
                   sum(CASE WHEN match_reason LIKE '%"family:%' THEN 1 ELSE 0 END),
                   sum(CASE WHEN match_decision = 'match' THEN 1 ELSE 0 END)
            FROM jobs WHERE status = 'open' AND first_complete_scan_id IS NOT NULL
            GROUP BY source_id
            """
        ):
            measured[source_id] = (roles or 0, uk or 0, family or 0, matches or 0)
    finally:
        connection.close()

    for row in rows:
        if row["status"] != "added":
            continue
        values = measured.get(row["employer"])
        if values is None:
            continue
        row["roles"], row["uk_roles"], row["family_roles"], row["matches"] = (
            str(value) for value in values
        )
    return rows


def cmd_seed(args: argparse.Namespace) -> int:
    rows = read_registry()
    rows = merge(rows, catalogue_rows(args.date))
    if args.inventory:
        rows = merge(rows, inventory_rows(args.inventory, args.date))
    rows = apply_statuses(rows, args.date)
    if args.db:
        rows = apply_measurements(rows, args.db)
    write_registry(rows)
    counts = Counter(row["status"] for row in rows)
    print(f"registry: {REGISTRY} ({len(rows)} employers)")
    for status, count in sorted(counts.items()):
        print(f"  {status:<12}{count:>5}")
    contributing = sum(
        1
        for row in rows
        if row["status"] == "added" and row["matches"].isdigit() and int(row["matches"]) > 0
    )
    added = sum(1 for row in rows if row["status"] == "added")
    print(f"  contributing (>=1 match) {contributing} of {added} shipped employers")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    rows = read_registry(args.registry)
    if not rows:
        print(f"error: no registry at {args.registry}", file=sys.stderr)
        return 1
    catalogued = {source.id for source in DEFAULT_SOURCES}
    registered = {row["employer"] for row in rows}
    added = {row["employer"] for row in rows if row["status"] == "added"}

    missing = sorted(catalogued - registered)
    unshipped = sorted(added - catalogued)
    stale = sorted(
        row["employer"]
        for row in rows
        if row["status"] == "added" and row["employer"] not in catalogued
    )

    failures = 0
    if missing:
        print(f"error: {len(missing)} catalogued employer(s) missing from the registry:")
        for employer in missing:
            print("   ", employer)
        failures += 1
    if unshipped or stale:
        drifted = sorted(set(unshipped) | set(stale))
        print(f"error: {len(drifted)} registry 'added' row(s) not in the catalogue:")
        for employer in drifted:
            print("   ", employer)
        failures += 1
    if not failures:
        print(f"registry and catalogue agree: {len(catalogued)} employers, all recorded")
    return 1 if failures else 0


def cmd_report(args: argparse.Namespace) -> int:
    rows = read_registry(args.registry)
    by_status = Counter(row["status"] for row in rows)
    by_platform = Counter(row["platform"] or "unknown" for row in rows if row["status"] == "added")
    print(f"employers known: {len(rows)}")
    for status, count in sorted(by_status.items()):
        print(f"  {status:<12}{count:>5}")
    print("catalogued by platform:")
    for platform, count in by_platform.most_common():
        print(f"  {platform:<14}{count:>5}")
    candidates = Counter(
        row["platform"] or "unknown" for row in rows if row["status"] == "candidate"
    )
    print("outstanding candidates by platform (the expansion queue):")
    for platform, count in candidates.most_common():
        print(f"  {platform:<14}{count:>5}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=REGISTRY)
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="write or refresh the registry from the catalogue")
    seed.add_argument("--inventory", type=Path, help="employer-boards.json from a discovery pass")
    seed.add_argument("--db", type=Path, help="scan database to record each source's contribution")
    seed.add_argument("--date", default="", help="evidence date to record, e.g. 2026-11-13")
    seed.set_defaults(func=cmd_seed)

    check = sub.add_parser("check", help="fail when the registry and catalogue disagree")
    check.set_defaults(func=cmd_check)

    report = sub.add_parser("report", help="summarise the registry")
    report.set_defaults(func=cmd_report)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
