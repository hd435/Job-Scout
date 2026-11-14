"""Harvest UK early-career vacancies from Workable's public job search, for discovery only.

The chosen engine is opportunity-first: start from real vacancies, take the employer, and then
monitor that employer's **own** board. Workable publishes a search API over every published role on
its platform, so a UK early-career query returns hundreds of vacancies each naming the account that
carries it — and that account is the employer's official careers system, not a third-party feed.

Nothing here is monitored. The output is a list of accounts for `tools/probe_boards.py` to judge and
`tools/employer_registry.py` to record, because an account that publishes one graduate role is not
the same as an employer worth adding.

    python tools/harvest_workable.py                 # all early-career terms, UK
    python tools/probe_boards.py --input data/research/workable-accounts.txt

The page size is capped at 20 by the API, so the queries are narrowed to early-career terms rather
than paging the whole UK catalogue (10 000+ roles) — about 50 requests instead of 500.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobscout.http import (  # noqa: E402
    HttpError,
    PacedHttpClient,
    PacingPolicy,
    UrllibHttpClient,
)

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "research" / "employers.csv"
DEFAULT_OUT = ROOT / "data" / "research" / "workable-accounts.json"
SEARCH = "https://jobs.workable.com/api/v1/jobs"

#: Everyday early-career vocabulary, matching what the matcher looks for so discovery and matching
#: agree about what "early career" means.
TERMS = (
    "graduate",
    "intern",
    "placement",
    "apprentice",
    "trainee",
    "junior",
    "entry level",
    "year in industry",
)

#: The API answers at most this many records per request.
PAGE_SIZE = 20

#: `https://apply.workable.com/{account}/j/{shortcode}/` is the public shape of a posting.
JOB_URL = re.compile(r"apply\.workable\.com/([a-z0-9-]+)/j/([A-Za-z0-9]+)")


def account_of(url: str) -> str:
    """The Workable account a posting belongs to, or "" when the URL is not the public shape."""
    match = JOB_URL.search(url)
    return match.group(1) if match else ""


def load_known_accounts(registry: Path) -> set[str]:
    """Employers already shipped or already judged, so the sweep does not re-research them."""
    if not registry.exists():
        return set()
    with registry.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    known = {slugify(row["name"]) for row in rows if row.get("name")}
    known |= {row["employer"] for row in rows if row.get("employer")}
    return known


def slugify(name: str) -> str:
    """The same form the registry uses for its ids, so an employer is recognised however spelled."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", name.strip().casefold()).strip("_")
    return re.sub(r"^the_", "", cleaned)


def search(client: PacedHttpClient, term: str, pages: int) -> list[dict[str, object]]:
    """One term, up to `pages` pages of results."""
    found: list[dict[str, object]] = []
    token = ""
    for _ in range(pages):
        url = f"{SEARCH}?location={quote('United Kingdom')}&query={quote(term)}&limit={PAGE_SIZE}"
        if token:
            url += f"&pageToken={quote(token)}"
        try:
            payload = client.get_json(url)
        except HttpError as exc:
            print(f"  {term}: {exc.summary()}", file=sys.stderr)
            break
        jobs = payload.get("jobs") or []
        found.extend(jobs)
        token = payload.get("nextPageToken") or ""
        if not token or len(jobs) < PAGE_SIZE:
            break
    return found


def summarise(jobs: list[dict[str, object]], known: set[str]) -> list[dict[str, object]]:
    """One row per company, with what it published and the website it names.

    The search does not reveal the Workable account that carries a posting, and the public posting
    page is a JavaScript shell, so the account is not the handle to work from. The company record
    does carry its `website`, which is the better handle anyway: resolving the board from an
    employer's own domain finds any ATS, not just Workable.
    """
    by_company: dict[str, dict[str, object]] = {}
    for job in jobs:
        company = job.get("company") or {}
        if not isinstance(company, dict):
            continue
        key = str(company.get("id") or "")
        if not key:
            continue
        name = str(company.get("title") or key)
        slug = slugify(name)
        if slug in known:
            continue
        row = by_company.setdefault(
            key,
            {
                "employer": name,
                "website": str(company.get("website") or ""),
                "titles": [],
                "locations": set(),
            },
        )
        title = str(job.get("title") or "")
        if title and title not in row["titles"]:
            row["titles"].append(title)  # type: ignore[union-attr]
        location = job.get("location") or {}
        if isinstance(location, dict):
            city = str(location.get("city") or "")
            if city:
                row["locations"].add(city)  # type: ignore[union-attr]
    rows = []
    for row in by_company.values():
        rows.append(
            {
                "employer": row["employer"],
                "website": row["website"],
                "postings": len(row["titles"]),  # type: ignore[arg-type]
                "example": (row["titles"] or [""])[0],  # type: ignore[index]
                "cities": sorted(row["locations"])[:6],  # type: ignore[arg-type]
            }
        )
    rows.sort(key=lambda row: int(row["postings"]), reverse=True)  # type: ignore[arg-type]
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=REGISTRY)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--pages", type=int, default=20, help="pages per term (20 records each)")
    parser.add_argument("--sleep", type=float, default=0.5, help="seconds between requests")
    args = parser.parse_args()

    known = load_known_accounts(args.registry)
    client = PacedHttpClient(
        UrllibHttpClient(), policy=PacingPolicy(default_seconds=args.sleep, max_requests=100_000)
    )
    client.begin_scan()

    seen_ids: set[str] = set()
    jobs: list[dict[str, object]] = []
    for term in TERMS:
        found = search(client, term, args.pages)
        fresh = [job for job in found if str(job.get("id")) not in seen_ids]
        seen_ids.update(str(job.get("id")) for job in fresh)
        jobs.extend(fresh)
        print(f"{term:<18}{len(found):>5} results, {len(fresh):>5} new")

    rows = summarise(jobs, known)
    print(f"\n{len(jobs)} UK early-career posting(s) across {len(rows)} unknown employer(s)")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"employers": rows}, indent=2), encoding="utf-8")
    sites_path = args.out.with_suffix(".txt")
    sites_path.write_text(
        "\n".join(f"{row['employer']},{row['website']}" for row in rows if row["website"]) + "\n",
        encoding="utf-8",
    )

    for row in rows[:25]:
        print(f"  {row['employer'][:32]:<32}{row['postings']:>3}  {row['website'][:40]}")
    if len(rows) > 25:
        print(f"  ... {len(rows) - 25} more in {sites_path}")
    without_site = sum(1 for row in rows if not row["website"])
    if without_site:
        print(f"  {without_site} employer(s) named no website and cannot be resolved this way")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
