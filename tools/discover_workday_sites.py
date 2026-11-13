"""Discover which Workday site names a tenant exposes, so a campus site can be found by machine.

A Workday tenant is addressed by `host/tenant/site`, and the site name is not published anywhere
machine-readable — it is whatever the employer called it: `intern_graduate`,
`blackstone_campus_careers`, `externalcareersite`, `1000`. Guessing from a careers-page URL works
for a group board but not for
the early-career sites that actually match our families, so this tries the names those sites
conventionally use and reports which ones answer.

One small POST per (tenant, site) — the listing query with `limit: 20`, no detail lookups — spaced
per host by the same pacing client the scanner uses.

    python tools/discover_workday_sites.py --tenants tenants.txt
    python tools/discover_workday_sites.py --tenants tenants.txt --search-text graduate

Input is `host,tenant` lines (`#` starts a comment). Output is one line per tenant plus a JSON file
beside the input, so the result can be fed to `tools/probe_boards.py` or straight into the registry.
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

from jobscout.http import HttpError, PacedHttpClient, UrllibHttpClient  # noqa: E402

#: Site names employers use for early-career hiring, most specific first. That pattern is worth
#: trying because `blackstone_campus_careers` and `intern_graduate` were both found this way.
CANDIDATE_SITES = (
    "intern_graduate",
    "interns_graduates",
    "graduates",
    "graduate",
    "graduate_careers",
    "early_careers",
    "earlycareers",
    "early_talent",
    "campus",
    "campus_careers",
    "students",
    "student_careers",
    "university",
    "university_careers",
    "emerging_talent",
)


def listing_url(host: str, tenant: str, site: str) -> str:
    return f"https://{host}/wday/cxs/{tenant}/{site}/jobs"


def read_tenants(path: Path) -> list[tuple[str, str]]:
    tenants: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 2:
            raise SystemExit(f"bad tenant line (want host,tenant): {raw!r}")
        tenants.append((parts[0], parts[1]))
    return tenants


def site_totals(
    client: PacedHttpClient, host: str, tenant: str, site: str, search_text: str
) -> int | None:
    """How many postings this site reports, or None when it does not exist for this tenant."""
    payload = {
        "appliedFacets": {},
        "searchText": search_text,
        "limit": 20,
        "offset": 0,
    }
    try:
        response = client.post_json(listing_url(host, tenant, site), payload, retry=False)
    except HttpError:
        return None
    if not isinstance(response, dict):
        return None
    postings = response.get("jobPostings")
    if not isinstance(postings, list):
        return None
    total = response.get("total")
    return int(total) if isinstance(total, int) else len(postings)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenants", type=Path, required=True, help="host,tenant lines")
    parser.add_argument("--search-text", default="", help="narrow every query, e.g. graduate")
    parser.add_argument("--out", type=Path, help="JSON output (default: beside the input)")
    parser.add_argument("--sleep", type=float, default=0.0, help="extra pause between tenants")
    args = parser.parse_args()

    tenants = read_tenants(args.tenants)
    out = args.out or args.tenants.with_suffix(".json")
    client = PacedHttpClient(UrllibHttpClient(max_attempts=1))
    print(f"trying {len(CANDIDATE_SITES)} site name(s) per tenant for {len(tenants)} tenant(s)")

    records: list[dict[str, Any]] = []
    for index, (host, tenant) in enumerate(tenants, start=1):
        found: list[dict[str, Any]] = []
        for site in CANDIDATE_SITES:
            total = site_totals(client, host, tenant, site, args.search_text)
            if total is None:
                continue
            found.append({"site": site, "total": total})
        records.append({"host": host, "tenant": tenant, "sites": found})
        if found:
            summary = ", ".join(f"{item['site']}={item['total']}" for item in found)
            print(f"{index:>3} {tenant:<22} {len(found)} site(s): {summary[:110]}")
        else:
            print(f"{index:>3} {tenant:<22} none of the conventional early-career site names")
        if args.sleep:
            time.sleep(args.sleep)

    payload = {
        "checked_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "search_text": args.search_text,
        "candidate_sites": list(CANDIDATE_SITES),
        "requests": client.requests,
        "records": records,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\n{out} ({client.requests} request(s))")

    hits = sum(len(record["sites"]) for record in records)
    counts = Counter(len(record["sites"]) for record in records)
    print(f"{hits} site name(s) answered across {len(records)} tenant(s); {dict(counts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
