"""Harvest UK early-career vacancies from SmartRecruiters' public job search, for discovery only.

Same opportunity-first shape as `tools/harvest_workable.py`, against a much larger index: the search
answers a keyword and a location, and every posting names the company identifier whose own tenant
the role lives on. That identifier is what a source would monitor.

    python tools/harvest_smartrecruiters.py --keyword graduate --out data/research/sr-graduate.json
    python tools/probe_boards.py --input data/research/sr-graduate.txt

Nothing here is monitored: the output is a list of companies for the probe to judge, because a
company that advertises one graduate role today is not the same as one worth a source.
"""

from __future__ import annotations

import argparse
import csv
import json
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
from jobscout.matching import (  # noqa: E402
    EARLY_CAREER_STEMS,
    EARLY_CAREER_WORDS,
    _has_stem,
    _has_word,
    normalize_text,
)

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "research" / "employers.csv"
SEARCH = "https://jobs.smartrecruiters.com/sr-jobs/search"

#: The index answers 100 postings per request regardless of the limit asked for.
PAGE_SIZE = 100

#: Keywords worth asking for: the index searches text, so one word per early-career idea.
KEYWORDS = ("graduate", "intern", "placement", "apprentice", "trainee", "junior", "entry level")


def is_early_career(title: str) -> bool:
    """The matcher's early-career test, so discovery and matching agree on the vocabulary."""
    text = normalize_text(title)
    if any(_has_stem(text, stem) for stem in EARLY_CAREER_STEMS):
        return True
    return bool(any(_has_word(text, word) for word in EARLY_CAREER_WORDS))


def load_known(registry: Path) -> set[str]:
    """Employer names already known, so a sweep does not re-research them."""
    if not registry.exists():
        return set()
    with registry.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    known = {" ".join(row["name"].casefold().split()) for row in rows if row.get("name")}
    known |= {" ".join(row["employer"].casefold().split()) for row in rows if row.get("employer")}
    return known


def search(
    client: PacedHttpClient, keyword: str, location: str, pages: int
) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    offset = 0
    for _ in range(pages):
        url = (
            f"{SEARCH}?keyword={quote(keyword)}&location={quote(location)}"
            f"&offset={offset}&limit={PAGE_SIZE}"
        )
        try:
            payload = client.get_json(url)
        except HttpError as exc:
            print(f"  {keyword}: {exc.summary()}", file=sys.stderr)
            break
        content = payload.get("content") or []
        found.extend(content)
        offset += len(content)
        total = payload.get("totalFound")
        if not content or (isinstance(total, int) and offset >= total):
            break
    return found


def summarise(postings: list[dict[str, object]], known: set[str]) -> list[dict[str, object]]:
    """One row per company, with the UK postings that suggest it is worth a source."""
    by_company: dict[str, dict[str, object]] = {}
    for posting in postings:
        company = posting.get("company") or {}
        if not isinstance(company, dict):
            continue
        identifier = str(company.get("identifier") or "")
        name = str(company.get("name") or identifier)
        if not identifier or " ".join(name.casefold().split()) in known:
            continue
        location = posting.get("location") or {}
        country = str(location.get("country") or "") if isinstance(location, dict) else ""
        if country.casefold() != "gb":
            continue
        title = str(posting.get("name") or "")
        if not is_early_career(title):
            continue
        row = by_company.setdefault(
            identifier,
            {"identifier": identifier, "employer": name, "titles": [], "cities": set()},
        )
        if title not in row["titles"]:
            row["titles"].append(title)  # type: ignore[union-attr]
        if isinstance(location, dict):
            city = str(location.get("city") or "")
            if city:
                row["cities"].add(city)  # type: ignore[union-attr]
    rows = [
        {
            "identifier": row["identifier"],
            "employer": row["employer"],
            "postings": len(row["titles"]),  # type: ignore[arg-type]
            "example": (row["titles"] or [""])[0],  # type: ignore[index]
            "cities": sorted(row["cities"])[:6],  # type: ignore[arg-type]
        }
        for row in by_company.values()
    ]
    rows.sort(key=lambda row: int(row["postings"]), reverse=True)  # type: ignore[arg-type]
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=REGISTRY)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--location", default="London, United Kingdom")
    parser.add_argument("--pages", type=int, default=10, help="pages per keyword (100 each)")
    parser.add_argument("--sleep", type=float, default=0.6, help="seconds between requests")
    args = parser.parse_args()

    known = load_known(args.registry)
    client = PacedHttpClient(
        UrllibHttpClient(), policy=PacingPolicy(default_seconds=args.sleep, max_requests=100_000)
    )
    client.begin_scan()

    seen: set[str] = set()
    postings: list[dict[str, object]] = []
    for keyword in KEYWORDS:
        found = search(client, keyword, args.location, args.pages)
        fresh = [posting for posting in found if str(posting.get("id")) not in seen]
        seen.update(str(posting.get("id")) for posting in fresh)
        postings.extend(fresh)
        print(f"{keyword:<14}{len(found):>5} postings, {len(fresh):>5} new")

    rows = summarise(postings, known)
    print(
        f"\n{len(postings)} posting(s) read, {len(rows)} new UK employer(s) with early-career roles"
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"employers": rows}, indent=2), encoding="utf-8")
    probe_path = args.out.with_suffix(".txt")
    probe_path.write_text(
        "\n".join(f"smartrecruiters,{row['identifier']},{row['employer']}" for row in rows) + "\n",
        encoding="utf-8",
    )
    for row in rows[:25]:
        print(f"  {row['identifier'][:28]:<29}{row['postings']:>3}  {row['example'][:52]}")
    if len(rows) > 25:
        print(f"  ... {len(rows) - 25} more in {probe_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
