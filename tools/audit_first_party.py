"""Audit monitored boards for the first-party principle (D-114, and the sourcing rule behind it).

A monitored source must be the employer's own channel: the roles it carries must be roles *that
employer* fills. An ATS account belonging to a recruitment agency passes every other check — the
account is the agency's, the roles are real, the links are direct — while being exactly what the
principle excludes, because the employer named in an alert would be the agency.

Workable's widget response carries the account's own self-description, so the test is cheap: read it
and look for the language businesses use when they recruit for other people. A flag is a prompt to
read, never a verdict, and the false positives are instructive — Javelin, Lanes Group and Zaizi all
describe serving clients, and all three are genuine employers.

    python tools/audit_first_party.py            # every Workable source, one request each
    python tools/audit_first_party.py --ids a b  # just these

Run it after any batch that adds boards; the batch-14 audit found one violation in 70 (`la_fosse`).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobscout.config import DEFAULT_SOURCES  # noqa: E402
from jobscout.http import HttpError, PacedHttpClient, PacingPolicy, UrllibHttpClient  # noqa: E402

#: Language that means "we are not the employer of these roles". Deliberately blunt.
AGENCY_SIGNS = (
    "recruitment agency",
    "recruitment consultancy",
    "recruitment partner",
    "recruitment services",
    "executive search",
    "talent solutions",
    "talent partner",
    "staffing",
    "we recruit for",
    "on behalf of our clients",
    "on behalf of clients",
    "our clients'",
    "job board",
    "headhunt",
    "apprenticeship provider",
    "training provider",
    "we deliver training",
)


def plain(text: str) -> str:
    """The description without markup, collapsed to one line."""
    stripped = re.sub(r"<[^>]+>", " ", text or "")
    return " ".join(stripped.replace("&nbsp;", " ").split())


def signs_in(name: str, description: str) -> list[str]:
    """The phrases that suggest this account is recruiting for other companies."""
    text = f"{name} {plain(description)}".casefold()
    return [sign for sign in AGENCY_SIGNS if sign in text]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", nargs="*", help="only these source ids")
    parser.add_argument("--sleep", type=float, default=0.4, help="seconds between requests")
    args = parser.parse_args()

    wanted = set(args.ids or ())
    accounts = [
        (source.id, str(source.options.get("account")))
        for source in DEFAULT_SOURCES
        if source.adapter == "workable"
        and source.options.get("account")
        and (not wanted or source.id in wanted)
    ]
    client = PacedHttpClient(
        UrllibHttpClient(), policy=PacingPolicy(default_seconds=args.sleep, max_requests=100_000)
    )
    flagged = []
    failed = 0
    for source_id, account in accounts:
        client.begin_scan()
        url = f"https://apply.workable.com/api/v1/widget/accounts/{account}?details=true"
        try:
            payload = client.get_json(url)
        except HttpError:
            failed += 1
            continue
        name = str(payload.get("name") or "")
        description = plain(str(payload.get("description") or ""))
        hits = signs_in(name, description)
        if hits:
            flagged.append((source_id, name, hits, description))

    print(f"{len(accounts)} board(s) read, {failed} unreadable, {len(flagged)} flagged")
    for source_id, name, hits, description in flagged:
        print(f"\n{source_id}  ({name})")
        print(f"  signs: {', '.join(hits)}")
        print(f"  says: {description[:280]}")
    if flagged:
        print("\nA flag is a prompt to read the account, not a verdict: most employers describe")
        print("their own clients, and only recruiter language means out of scope.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
