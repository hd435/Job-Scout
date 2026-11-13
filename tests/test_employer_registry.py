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
