"""Build a probe list from the registry's one-request candidates, plus wave 3's two Workday hits.

Most of these boards were judged under the previous six families and deferred for having no UK
early-career role in an enabled family. D-108 added engineering, science and supply chain, so the
same boards deserve one more request before they are written off.

    python tools/registry_candidates.py --out data/research/candidates-wave3.txt
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

REGISTRY = Path(__file__).resolve().parent.parent / "research" / "employers.csv"
ONE_REQUEST = ("greenhouse", "workable", "lever", "ashby")

#: Found by tools/discover_workday_sites.py, not guessed: the only two dedicated early-career
#: Workday sites among 20 large tenants (2 hits in 300 name guesses).
WORKDAY_HITS = (
    "workday,lseg.wd3.myworkdayjobs.com/lseg/graduate_careers,LSEG,graduate",
    "workday,wellington.wd5.myworkdayjobs.com/wellington/campus,Wellington Management,graduate",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    with REGISTRY.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    lines = [
        "# Wave 3 (2026-11-13): registry candidates on one-request platforms, re-judged under the",
        "# expanded families (D-108), plus the two Workday early-career sites discovery found.",
    ]
    lines.extend(WORKDAY_HITS)
    counted = 0
    for row in rows:
        if row["status"] != "candidate" or row["platform"] not in ONE_REQUEST:
            continue
        if not row["tenant"]:
            continue
        lines.append(f"{row['platform']},{row['tenant']},{row['name']}")
        counted += 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"wrote {args.out}: {counted} one-request candidate(s)"
        f" + {len(WORKDAY_HITS)} Workday site(s)"
    )


if __name__ == "__main__":
    main()
