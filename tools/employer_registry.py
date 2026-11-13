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
                "employer": record.get("company", "").strip().casefold().replace(" ", "_"),
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


def apply_rejections(rows: list[dict[str, str]], evidence_date: str) -> list[dict[str, str]]:
    by_id = {row["employer"]: row for row in rows}
    for employer, reason in REJECTED.items():
        if employer in by_id:
            by_id[employer]["status"] = "rejected"
            by_id[employer]["reason"] = reason
            by_id[employer]["evidence_date"] = evidence_date
        else:
            by_id[employer] = {
                "employer": employer,
                "name": employer.replace("_", " ").title(),
                "platform": "",
                "tenant": "",
                "site": "",
                "status": "rejected",
                "reason": reason,
                "evidence_date": evidence_date,
            }
    return list(by_id.values())


def cmd_seed(args: argparse.Namespace) -> int:
    rows = read_registry()
    rows = merge(rows, catalogue_rows(args.date))
    if args.inventory:
        rows = merge(rows, inventory_rows(args.inventory, args.date))
    rows = apply_rejections(rows, args.date)
    write_registry(rows)
    counts = Counter(row["status"] for row in rows)
    print(f"registry: {REGISTRY} ({len(rows)} employers)")
    for status, count in sorted(counts.items()):
        print(f"  {status:<12}{count:>5}")
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
