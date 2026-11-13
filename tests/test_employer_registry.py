"""The employer registry must not drift from the shipped catalogue.

`research/employers.csv` is the durable record of what was investigated and why. Its value comes
from being trustworthy, so the invariant is asserted here rather than checked by hand: every
catalogued employer is recorded, and nothing is marked `added` that is not shipped.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

REGISTRY = Path(__file__).resolve().parent.parent / "research" / "employers.csv"
TOOLS = Path(__file__).resolve().parent.parent / "tools"


def registry_rows() -> list[dict[str, str]]:
    with REGISTRY.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_the_registry_exists_and_has_the_expected_columns() -> None:
    assert REGISTRY.exists(), "research/employers.csv is the discovery record and must be committed"
    rows = registry_rows()
    assert rows, "the registry should not be empty"
    assert set(rows[0]) >= {
        "employer",
        "name",
        "platform",
        "tenant",
        "status",
        "reason",
        "evidence_date",
    }


def test_every_catalogued_employer_is_recorded() -> None:
    from jobscout.config import DEFAULT_SOURCES

    recorded = {row["employer"] for row in registry_rows()}
    missing = sorted({source.id for source in DEFAULT_SOURCES} - recorded)

    assert missing == [], f"catalogued but not in the registry: {missing}"


def test_nothing_is_marked_added_that_is_not_shipped() -> None:
    from jobscout.config import DEFAULT_SOURCES

    catalogued = {source.id for source in DEFAULT_SOURCES}
    claimed = {row["employer"] for row in registry_rows() if row["status"] == "added"}

    assert sorted(claimed - catalogued) == [], "registry claims employers the catalogue lacks"


def test_every_rejection_records_a_reason() -> None:
    """A rejection without a reason is how research gets repeated."""
    for row in registry_rows():
        if row["status"] in ("rejected", "deferred"):
            assert row["reason"].strip(), f"{row['employer']} has no reason recorded"


def test_the_registry_check_command_passes() -> None:
    """The tool's own drift check is the gate; run it rather than reimplementing it."""
    import argparse

    sys.path.insert(0, str(TOOLS))
    try:
        import employer_registry
    finally:
        sys.path.pop(0)

    exit_code = employer_registry.cmd_check(argparse.Namespace(registry=REGISTRY))

    assert exit_code == 0


def test_a_missing_board_is_not_recorded_as_a_verdict(tmp_path: Path) -> None:
    """A guessed slug that 404s is a failed guess, not a fact about the employer."""
    import json

    sys.path.insert(0, str(TOOLS))
    try:
        import employer_registry
    finally:
        sys.path.pop(0)

    probe_path = tmp_path / "probe.json"
    probe_path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "adapter": "greenhouse",
                        "slug": "zzzznotarealboard12345",
                        "employer": "Nope",
                        "verdict": "reject",
                        "verdict_reason": "scan failed: request rejected status=404 permanent",
                        "records_parsed": 0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    rows = employer_registry.apply_probe([], probe_path, "2026-11-13")

    assert rows == []


def test_no_row_duplicates_a_feed_that_is_already_shipped() -> None:
    """A second row for the same board is how a candidate queue silently doubles.

    A discovery pass keys its rows by the employer name it found while the catalogue keys them by
    source id, so re-seeding an inventory after enrolment used to append `charles_river_associates`
    beside `charles_river` — the same board counted twice, and probed twice next time.
    """
    sys.path.insert(0, str(TOOLS))
    try:
        import employer_registry
    finally:
        sys.path.pop(0)

    _, duplicates = employer_registry.dedupe(registry_rows())

    assert duplicates == []


def test_dedupe_keeps_a_genuinely_different_board_for_a_covered_employer() -> None:
    """The board is the identity, not the employer: DRW's university board is its own feed."""
    sys.path.insert(0, str(TOOLS))
    try:
        import employer_registry
    finally:
        sys.path.pop(0)

    rows = [
        {
            "employer": "drw",
            "name": "DRW",
            "platform": "greenhouse",
            "tenant": "drweng",
            "site": "",
            "status": "added",
            "reason": "",
            "evidence_date": "",
        },
        {
            "employer": "drw_university",
            "name": "DRW - University",
            "platform": "greenhouse",
            "tenant": "drwuniversityjobs",
            "site": "",
            "status": "candidate",
            "reason": "",
            "evidence_date": "",
        },
    ]

    kept, duplicates = employer_registry.dedupe(rows)

    assert duplicates == []
    assert [row["employer"] for row in kept] == ["drw", "drw_university"]


def test_dedupe_drops_the_repeated_board_and_keeps_rows_with_no_board() -> None:
    sys.path.insert(0, str(TOOLS))
    try:
        import employer_registry
    finally:
        sys.path.pop(0)

    rows = [
        {
            "employer": "charles_river",
            "name": "Charles River Associates",
            "platform": "greenhouse",
            "tenant": "charlesriverassociates",
            "site": "",
            "status": "added",
            "reason": "",
            "evidence_date": "",
        },
        {
            "employer": "charles_river_associates",
            "name": "Charles River Associates",
            "platform": "greenhouse",
            "tenant": "charlesriverassociates",
            "site": "",
            "status": "candidate",
            "reason": "",
            "evidence_date": "",
        },
        {
            "employer": "unknown_a",
            "name": "Unknown A",
            "platform": "",
            "tenant": "",
            "site": "",
            "status": "candidate",
            "reason": "no public board found",
            "evidence_date": "",
        },
        {
            "employer": "unknown_b",
            "name": "Unknown B",
            "platform": "",
            "tenant": "",
            "site": "",
            "status": "candidate",
            "reason": "no public board found",
            "evidence_date": "",
        },
    ]

    kept, duplicates = employer_registry.dedupe(rows)

    assert [row["employer"] for row in kept] == ["charles_river", "unknown_a", "unknown_b"]
    assert duplicates == ["charles_river_associates (greenhouse/charlesriverassociates)"]
