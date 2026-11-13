"""Turn boardless registry candidates into platform slug guesses for `tools/probe_boards.py`.

The inventory route (read a published job page, take the board it names) finds a board for most
employers, but a minority publish on pages that do not reveal one. For those the only cheap,
respectful next step is to guess: most ATS tenants are the employer's name with the spaces
removed or hyphenated. This writes those guesses in the probe tool's input format.

A guess that 404s is not recorded as a verdict on the employer — `employer_registry.apply_probe`
drops 404s — so a sweep costs requests and nothing else.

    python tools/guess_slugs.py                      # writes data/research/slug-guesses.txt
    python tools/probe_boards.py --input data/research/slug-guesses.txt
"""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

REGISTRY = Path(__file__).resolve().parent.parent / "research" / "employers.csv"
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "data" / "research" / "slug-guesses.txt"

#: The platforms that answer in one request, so a wrong guess costs one GET.
PLATFORMS = ("greenhouse", "workable", "lever", "ashby", "recruitee")


def slug_forms(name: str) -> list[str]:
    """The two shapes tenants actually use: no spaces at all, or hyphenated."""
    cleaned = re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()
    if not cleaned:
        return []
    flat = cleaned.replace(" ", "")
    hyphen = cleaned.replace(" ", "-")
    return [flat] if flat == hyphen else [flat, hyphen]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=REGISTRY)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    with args.registry.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    boardless = [row for row in rows if row["status"] == "candidate" and not row["tenant"]]
    entries: list[str] = []
    seen: set[tuple[str, str]] = set()
    for row in boardless:
        name = row["name"] or row["employer"]
        for slug in slug_forms(name):
            for platform in PLATFORMS:
                if (platform, slug) in seen:
                    continue
                seen.add((platform, slug))
                entries.append(f"{platform},{slug},{name}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(entries) + "\n", encoding="utf-8")
    print(f"{len(boardless)} boardless candidate(s) -> {len(entries)} guess(es) in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
