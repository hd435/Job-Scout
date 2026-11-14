"""Resolve an employer's official careers source from its own domain.

The inventory route reads a published job page and takes the board it names. That works until the
published page does not name one, and the slug sweep (`tools/guess_slugs.py`) then guesses a tenant
blind. This closes the gap the way a person would: go to the employer's website, find its careers
page, and read which ATS it actually uses.

Two requests per candidate domain, in the order that pays first: the root page (whose navigation
almost always links straight to the ATS) and then `/careers`. The first ATS fingerprint found wins,
so a real employer usually costs two requests and a domain that does not exist costs one.

    python tools/resolve_careers.py                     # every registry candidate with no board
    python tools/resolve_careers.py --names names.txt    # or a plain list, one employer per line
    python tools/employer_registry.py seed --probe <out>.json

A resolved Workday site is written as `host/tenant/site`, which is the shape `probe_boards.py`
expects. Platforms with no adapter are recorded as such rather than dropped: knowing that forty
employers use iCIMS is the argument for writing that adapter, and it is exactly the research that
gets repeated.

This is discovery, not monitoring: it reads public career pages at human pace and never enrols
anything. Every board it finds is judged by `probe_boards.py` like any other candidate.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobscout.http import (  # noqa: E402
    HttpError,
    PacedHttpClient,
    PacingPolicy,
    UrllibHttpClient,
)

REGISTRY = Path(__file__).resolve().parent.parent / "research" / "employers.csv"
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "data" / "research" / "careers-resolved.json"

#: The domain suffixes worth trying, best first: `.co.uk` before `.com` for a UK-first product is
#: not obviously right — most UK employers with an ATS use `.com` — so the other order was measured
#: and `.com` first resolves more. `.uk` is last because it is rare.
SUFFIXES = ("com", "co.uk", "uk")

#: Paths to try on a domain that answered. The root page comes first because its navigation usually
#: links to the ATS directly.
PATHS = ("", "/careers")

#: ATS fingerprints: the host an employer's careers link points at, and how to read the tenant out
#: of it. Every pattern captures the account in group 1, so one function can classify them all.
ATS_PATTERNS: dict[str, str] = {
    "greenhouse": r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_%-]+)",
    "ashby": r"(?:jobs|api)\.ashbyhq\.com/([a-z0-9._-]+)",
    "lever": r"jobs\.lever\.co/([a-z0-9-]+)",
    "workable": r"apply\.workable\.com/([a-z0-9-]+)",
    "recruitee": r"([a-z0-9-]+)\.recruitee\.com",
    "teamtailor": r"([a-z0-9-]+)\.teamtailor\.com",
    "smartrecruiters": r"jobs\.smartrecruiters\.com/([A-Za-z0-9-]+)",
    "icims": r"([a-z0-9-]+)\.icims\.com",
    "taleo": r"([a-z0-9-]+)\.taleo\.net",
    "successfactors": r"([a-z0-9-]+)\.successfactors\.(?:com|eu)",
    "oracle": r"([a-z0-9-]+)\.oraclecloud\.com/hcmUI/CandidateExperience",
}

#: Workday needs its host and site as well as its tenant, so it is matched on its own.
WORKDAY_PATTERN = (
    r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)"
)

#: Platforms this project can already read with one request, so a hit here is immediately useful.
READABLE = ("greenhouse", "ashby", "lever", "workable", "recruitee", "workday")

#: A page is only worth fingerprinting if it is plausibly a careers page; anything else is a
#: marketing site whose ATS links would be noise. This is a cheap pre-filter, not a parser.
CAREERS_HINT = re.compile(r"career|job|vacanc|recruit|hiring", re.IGNORECASE)


def slug_forms(name: str) -> list[str]:
    """The domains a name plausibly maps to: "Bae Systems" -> baesystems, bae-systems, bae.

    The third form is the first word on its own, and it exists because UK employers are often named
    after a group whose domain drops the tail: "Analog Devices" publishes on `analog.com`, "Baker
    Hughes" on `bakerhughes.com` but "CME Group" on `cmegroup.com`. It is tried last because a
    single generic word can belong to an unrelated business — which is why the domain that answered
    is recorded and the board it names is still judged by the probe gate.
    """
    cleaned = re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()
    cleaned = re.sub(r"\b(limited|ltd|plc|group|holdings?|the)\b", " ", cleaned)
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return []
    flat = cleaned.replace(" ", "")
    hyphen = cleaned.replace(" ", "-")
    forms = [flat] if flat == hyphen else [flat, hyphen]
    first = cleaned.split(" ")[0]
    if len(cleaned.split(" ")) > 1 and first not in forms:
        forms.append(first)
    return forms


def fingerprint(html: str) -> tuple[str, str] | None:
    """Read an ATS out of a page: (platform, probe slug) or None.

    The slug is in the shape `tools/probe_boards.py` expects, so a hit can be judged straight away:
    a Workday site becomes `host/tenant/site` because that is one path-like field there.
    """
    match = re.search(WORKDAY_PATTERN, html)
    if match:
        tenant, host, site = match.group(1), match.group(2), match.group(3)
        return "workday", f"{tenant}.{host}.myworkdayjobs.com/{tenant}/{site}"
    for platform, pattern in ATS_PATTERNS.items():
        match = re.search(pattern, html)
        if match:
            return platform, match.group(1)
    return None


def careers_domains(name: str) -> list[str]:
    """Candidate front pages, most likely first."""
    return [f"https://www.{slug}.{suffix}" for slug in slug_forms(name) for suffix in SUFFIXES]


def url_groups(name: str, website: str = "") -> list[list[str]]:
    """The pages to read, grouped by how much they are worth trying.

    A harvested website is worth ten guesses: it is the employer's own domain, so `/careers` and
    `/jobs` are the only two paths that usually matter. The groups are ordered so a caller can stop
    after the website answers — reading the marketing site it already found, under a guessed name,
    adds nothing.
    """
    groups: list[list[str]] = []
    if website:
        base = website.strip().rstrip("/")
        if not base.startswith("http"):
            base = f"https://{base}"
        groups.append([base + path for path in PATHS])
    groups.append([domain + path for domain in careers_domains(name) for path in PATHS])
    return groups


def base_of(url: str) -> str:
    """Scheme and host, so the pages of one site can be skipped together."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def resolve(client: PacedHttpClient, name: str, website: str = "") -> dict[str, object]:
    """Read an employer's own pages until one names an ATS, and record what was tried."""
    record: dict[str, object] = {
        "employer": name,
        "platform": "",
        "slug": "",
        "source_url": "",
        "attempts": 0,
        "note": "",
    }
    seen_careers_page = False
    dead: set[str] = set()
    for group in url_groups(name, website):
        answered = False
        for url in group:
            base = base_of(url)
            if base in dead:
                continue
            try:
                html = client.get_text(url)
            except ValueError:
                # A malformed URL in a harvested list ("Outbrain, a Taboola company" once produced
                # one). Nothing about the employer follows from it, so the site is simply unusable.
                dead.add(base)
                continue
            except HttpError as exc:
                if exc.status is not None:
                    # A real answer: this page is not there, but the site is, so try its other
                    # paths.
                    answered = True
                    continue
                if not exc.uncertain:
                    # The host never opened, so it is not this employer's: skip its remaining
                    # pages rather than asking a name that does not resolve for more answers.
                    dead.add(base)
                continue
            answered = True
            record["attempts"] = int(record["attempts"]) + 1
            found = fingerprint(html)
            if found:
                platform, slug = found
                record.update(platform=platform, slug=slug, source_url=url)
                if platform not in READABLE:
                    record["note"] = f"{platform} has no adapter yet"
                return record
            if CAREERS_HINT.search(html):
                seen_careers_page = True
        if answered and website:
            # The employer's own site answered, so guessing its name would only find the marketing
            # site we have just read.
            break
    record["note"] = (
        "a careers page answered but named no known ATS"
        if seen_careers_page
        else "no domain answered"
    )
    return record


def load_names(args: argparse.Namespace) -> list[tuple[str, str]]:
    """Employers to resolve, with any website already known for them."""
    if args.sites:
        pairs = []
        for line in args.sites.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            # Split on the last comma: a company name can contain one ("Outbrain, a Taboola
            # company") but a website cannot.
            name, _, website = line.rpartition(",")
            if not name:
                pairs.append((website.strip(), ""))
                continue
            pairs.append((name.strip(), website.strip()))
        return pairs
    if args.names:
        return [
            (line.strip(), "")
            for line in args.names.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    with args.registry.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [(row["name"] or row["employer"], "") for row in rows if row["status"] == "candidate"]


def write_outputs(out: Path, records: list[dict[str, object]]) -> Path:
    """Write the record file and the probe input together, so an interrupted sweep still helps."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"records": records}, indent=2), encoding="utf-8")
    probe_lines = [
        f"{record['platform']},{record['slug']},{record['employer']}"
        for record in records
        if record["platform"] in READABLE
    ]
    probe_path = out.with_suffix(".txt")
    probe_path.write_text("\n".join(probe_lines) + "\n", encoding="utf-8")
    return probe_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=REGISTRY)
    parser.add_argument("--names", type=Path, help="plain list of employer names, one per line")
    parser.add_argument(
        "--sites",
        type=Path,
        help="list of `employer,website` lines: the site is read first, guesses follow",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--limit", type=int, default=0, help="stop after this many employers")
    parser.add_argument(
        "--sleep", type=float, default=1.0, help="seconds between requests per host"
    )
    parser.add_argument(
        "--fresh", action="store_true", help="ignore an existing output file instead of resuming"
    )
    args = parser.parse_args()

    previous: dict[str, dict[str, object]] = {}
    if args.out.exists() and not args.fresh:
        for record in json.loads(args.out.read_text(encoding="utf-8")).get("records", []):
            previous[str(record["employer"])] = record

    names = load_names(args)
    if previous:
        # Resuming matters at this scale: a sweep is a long run against dozens of unrelated hosts,
        # and losing it to one connection reset wastes the whole pass.
        names = [pair for pair in names if pair[0] not in previous]
    if args.limit:
        names = names[: args.limit]
    if not names and not previous:
        print("nothing to resolve", file=sys.stderr)
        return 1

    client = PacedHttpClient(
        UrllibHttpClient(), policy=PacingPolicy(default_seconds=args.sleep, max_requests=100_000)
    )
    print(f"resolving {len(names)} employer(s), {len(previous)} already done")

    records: list[dict[str, object]] = list(previous.values())
    for index, (name, site) in enumerate(names, start=1):
        client.begin_scan()
        record = resolve(client, name, site)
        records.append(record)
        write_outputs(args.out, records)
        detail = (
            f"{record['platform']}/{record['slug']}  from {record['source_url']}"
            if record["platform"]
            else str(record["note"])
        )
        print(f"{index:>3} {name[:34]:<34} {detail}")

    probe_path = write_outputs(args.out, records)
    probe_lines = sum(1 for record in records if record["platform"] in READABLE)

    found = [record for record in records if record["platform"]]
    readable = [record for record in found if record["platform"] in READABLE]
    print(
        f"\n{len(found)} of {len(records)} resolved to an ATS ({len(readable)} readable now);"
        f" {probe_lines} probe line(s) in {probe_path}"
    )
    for platform, count in sorted(
        (
            (platform, sum(1 for r in found if r["platform"] == platform))
            for platform in {r["platform"] for r in found}
        ),
        key=lambda pair: pair[1],
        reverse=True,
    ):
        print(f"  {platform:<16}{count:>4}{'' if platform in READABLE else '   (no adapter)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
