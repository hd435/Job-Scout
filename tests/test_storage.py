from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from jobscout.config import SourceConfig
from jobscout.models import JobStatus
from jobscout.storage import Storage


@pytest.fixture
def source_config() -> SourceConfig:
    return SourceConfig(
        id="palantir",
        employer="Palantir Technologies",
        adapter="lever",
        options={"site": "palantir"},
    )


def test_schema_and_migrations_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "jobscout.db"

    with Storage.open(path) as storage:
        version = storage.connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row["name"]
            for row in storage.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert version == 2
        assert {"sources", "scans", "jobs", "notifications"} <= tables

    with Storage.open(path) as storage:
        assert storage.connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_open_creates_missing_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state" / "jobscout.db"

    with Storage.open(path):
        assert path.exists()


def test_job_identity_is_unique_per_source(store: Storage, source_config, scan, record) -> None:
    scan([record("same-id", "Graduate Software Engineer")])

    row = store.get_job("palantir", "same-id")
    assert row is not None
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            """
            INSERT INTO jobs (
                source_id, source_job_id, employer, title, location_raw, url,
                content_hash, first_seen_scan_id, last_seen_scan_id, status,
                created_at, updated_at
            ) VALUES ('palantir', 'same-id', 'x', 'x', 'x', 'x', 'x', 1, 1, 'open', 'x', 'x')
            """
        )


def test_foreign_keys_are_enforced(store: Storage) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            """
            INSERT INTO scans (source_id, started_at, finished_at, status, complete,
                               records_seen, records_parsed)
            VALUES ('missing-source', 'x', 'x', 'failed', 0, 0, 0)
            """
        )


def test_not_null_constraints_are_enforced(store: Storage) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute("INSERT INTO jobs (source_id, title) VALUES ('palantir', 'x')")


def test_unknown_source_state_is_none(store: Storage) -> None:
    assert store.get_source_state("nope") is None


def test_ensure_source_preserves_baseline_after_scan(
    store: Storage, source_config, scan, record
) -> None:
    outcome = scan([record("a", "Graduate Software Engineer")])
    baseline_before = store.get_source_state("palantir").baseline_scan_id

    store.ensure_source(source_config)

    state = store.get_source_state("palantir")
    assert state.baseline_scan_id == baseline_before == outcome.scan_id
    assert state.employer == "Palantir Technologies"


def test_transaction_rolls_back_on_error(store: Storage) -> None:
    with pytest.raises(RuntimeError), store.transaction():
        store.connection.execute(
            """
                INSERT INTO sources (id, employer, adapter, config_json, cadence_minutes)
                VALUES ('temp', 'Temp', 'lever', '{}', 30)
                """
        )
        raise RuntimeError("boom")

    assert store.get_source_state("temp") is None


def test_closed_job_row_keeps_identity(store: Storage, scan, record) -> None:
    scan([record("a", "Graduate Software Engineer"), record("b", "Graduate Data Engineer")])
    scan([record("a", "Graduate Software Engineer")])
    scan([record("a", "Graduate Software Engineer")])

    closed = store.get_job("palantir", "b")
    assert closed.status is JobStatus.CLOSED
    assert closed.closed_at is not None
    assert closed.job.title == "Graduate Data Engineer"
