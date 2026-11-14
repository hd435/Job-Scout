"""Turn a probe's enrol verdicts into catalogue files.

Adding an employer by hand is ten lines of TOML, which is fine for one and is the reason expansion
stalled at single figures per sweep. Everything in those ten lines is already known by the time the
probe has judged a board: the adapter, the tenant, the employer name, and the measurement that
justified it. This writes the file, refuses to overwrite one, and prints what it did.

    python tools/probe_boards.py --input candidates.txt
    python tools/add_sources.py --probe data/research/probe-london.json --dry-run
    python tools/add_sources.py --probe data/research/probe-london.json

It never validates or scans — `validate --confirm-live` and `scan --dry-run` stay deliberate steps,
because a source that has not been read once by hand is a source nobody has checked.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOGUE = ROOT / "src" / "jobscout" / "catalogue"

sys.path.insert(0, str(ROOT / "src"))

from jobscout.config import DEFAULT_SOURCES  # noqa: E402

#: The public endpoint each adapter reads, in the form the loader requires (D-087). Copied from the
#: shipped catalogue rather than invented, so a generated file reads like a hand-written one.
ACCESS_NOTES = {
    "greenhouse": (
        "public Greenhouse job-board API (boards-api.greenhouse.io), one GET per scan, no auth,"
        " no pagination"
    ),
    "ashby": (
        "public Ashby job-board API (api.ashbyhq.com/posting-api/job-board), one GET per scan,"
        " no auth, no pagination"
    ),
    "lever": (
        "public Lever postings API (api.lever.co/v0/postings), one GET per scan, no auth,"
        " no pagination"
    ),
    "workable": (
        "public Workable widget API (apply.workable.com/api/v1/widget/accounts), one GET per scan,"
        " no auth, no pagination"
    ),
    "recruitee": (
        "public Recruitee offers API ({account}.recruitee.com/api/offers/), one GET per scan,"
        " no auth, no pagination"
    ),
    "workday": (
        "public Workday job-search endpoint (myworkdayjobs.com), paged GET per scan, no auth"
    ),
}

#: The single option key each adapter needs, chosen from the probe's slug.
OPTION_KEY = {
    "greenhouse": "board",
    "ashby": "board",
    "lever": "site",
    "workable": "account",
    "recruitee": "company",
}


def slugify(name: str) -> str:
    """A short stable id: "Fuse Energy" -> fuse_energy, and never a different employer's id."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", name.strip().casefold()).strip("_")
    return re.sub(r"^the_", "", cleaned) or "employer"


def existing_ids() -> set[str]:
    return {source.id for source in DEFAULT_SOURCES}


def existing_tenants() -> set[str]:
    """Every tenant already monitored, so a second board for a covered employer is not silent."""
    tenants = set()
    for source in DEFAULT_SOURCES:
        for key in ("board", "account", "site", "company", "tenant"):
            value = source.options.get(key)
            if value:
                tenants.add(str(value).casefold())
    return tenants


def render(record: dict[str, object], source_id: str, today: str) -> str:
    """One catalogue file, in the shape the shipped ones use."""
    adapter = str(record["adapter"])
    slug = str(record["slug"])
    employer = str(record["employer"]).strip()
    lines = [
        f"# {adapter.capitalize()} board `{slug}` behind {employer}.",
        f"# Discovery {today} (Workable London search, board resolved from the",
        f"# employer's own careers page): {record['records_parsed']} roles,",
        f"# {record['uk_roles']} UK, {record['early_family_roles']} in an enabled family with an",
        f"# early-career signal, {record['matches']} matching under the default rules.",
    ]
    if not int(record.get("early_family_roles") or 0):
        lines += [
            "#",
            f"# Monitored seasonally (D-117): {record.get('family_roles')} of its UK roles are in",
            "# an enabled family but none is early-career today, so it contributes nothing until",
            "# its next intake opens. Recorded here so the decision is visible, not surprising.",
        ]
    example = (record.get("early_titles") or [""])[0]
    if example:
        lines.append(f"# Example: {example}")
    lines += [
        f'id = "{source_id}"',
        f'employer = "{employer}"',
        f'adapter = "{adapter}"',
        f'access_note = "{ACCESS_NOTES.get(adapter, "public job board, read politely")}"',
        "cadence_minutes = 30",
        "",
        "[options]",
    ]
    if adapter == "workday":
        host, tenant, site = slug.split("/")
        lines += [f'host = "{host}"', f'tenant = "{tenant}"', f'site = "{site}"']
    else:
        lines.append(f'{OPTION_KEY[adapter]} = "{slug}"')
    # `uk_employer` relaxes town-name ambiguity on boards that only hire in the UK.
    parsed = int(record.get("records_parsed") or 0)
    uk = int(record.get("uk_roles") or 0)
    if parsed and uk / parsed >= 0.85:
        lines.append("uk_employer = true")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", type=Path, required=True, help="probe JSON to read")
    parser.add_argument("--catalogue", type=Path, default=CATALOGUE)
    parser.add_argument("--date", default="2026-11-14", help="discovery date for the file comment")
    parser.add_argument("--dry-run", action="store_true", help="print what would be written")
    parser.add_argument(
        "--min-family-roles",
        type=int,
        default=0,
        help=(
            "also enrol a board with no early-career role today, when it publishes at least this"
            " many UK roles in an enabled family (it is monitored for its next intake)"
        ),
    )
    parser.add_argument(
        "--min-uk-share",
        type=float,
        default=0.5,
        help="with --min-family-roles, the share of the board that must be UK",
    )
    parser.add_argument(
        "--accept-unmeasured",
        action="store_true",
        help="write a board with zero matches but with early-career family roles",
    )
    args = parser.parse_args()

    payload = json.loads(args.probe.read_text(encoding="utf-8"))
    ids = existing_ids()
    tenants = existing_tenants()
    written = 0
    skipped: list[str] = []
    for record in payload.get("records", []):
        verdict = record.get("verdict")
        seasonal = False
        if verdict != "enrol":
            # A deferred board is normally not worth a source. The exception is the one that made
            # the catalogue a snapshot of hiring season: an employer that publishes several UK
            # roles in an enabled family but has no early-career role open *today* will post one
            # when its intake opens, and monitoring it is the whole point of the product.
            if not (args.min_family_roles and verdict == "defer"):
                continue
            parsed = int(record.get("records_parsed") or 0)
            uk = int(record.get("uk_roles") or 0)
            family = int(record.get("family_roles") or 0)
            share = uk / parsed if parsed else 0.0
            if family < args.min_family_roles or share < args.min_uk_share:
                continue
            if int(record.get("early_family_roles") or 0):
                continue
            seasonal = True
        employer = str(record.get("employer") or "").strip()
        slug = str(record.get("slug") or "").strip()
        if not employer or not slug:
            skipped.append(f"{employer or slug}: missing name or slug")
            continue
        if str(record.get("adapter")) not in ACCESS_NOTES:
            skipped.append(f"{employer}: no adapter for {record.get('adapter')}")
            continue
        if slug.casefold() in tenants:
            skipped.append(f"{employer}: board {slug} is already monitored")
            continue
        source_id = slugify(employer)
        if source_id in ids:
            source_id = f"{source_id}_{str(record['adapter'])[:4]}"
        if source_id in ids:
            skipped.append(f"{employer}: id {source_id} already exists")
            continue
        if (
            not int(record.get("matches") or 0)
            and not seasonal
            and not (args.accept_unmeasured and int(record.get("early_family_roles") or 0))
        ):
            # A board with roles but no matches is storage without value; the batch review rule in
            # the runbook says keep it only when you expect that to change.
            skipped.append(f"{employer}: {record.get('records_parsed')} roles but 0 matches")
            continue
        path = args.catalogue / f"{source_id}.toml"
        if path.exists():
            skipped.append(f"{employer}: {path.name} already exists")
            continue
        text = render(record, source_id, args.date)
        if args.dry_run:
            print(f"--- {path.name}\n{text}")
        else:
            path.write_text(text, encoding="utf-8")
            print(
                f"wrote {path.name}  ({record['uk_roles']} UK roles, {record['matches']} matches)"
            )
        written += 1
        ids.add(source_id)

    print(
        f"\n{written} file(s) {'would be ' if args.dry_run else ''}written, {len(skipped)} skipped"
    )
    for reason in skipped:
        print("  -", reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
