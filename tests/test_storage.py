from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from jobscout.config import SourceConfig
from jobscout.models import JobStatus
from jobscout.storage import FAILURE_NOTICE_MAX_ATTEMPTS, Storage


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
        assert version == 8
        assert {
            "sources",
            "scans",
            "jobs",
            "notifications",
            "outage_notices",
            "delivery_checks",
            "service_users",
            "service_preferences",
            "service_subscriptions",
            "service_sessions",
            "service_meta",
        } <= tables

    with Storage.open(path) as storage:
        assert storage.connection.execute("PRAGMA user_version").fetchone()[0] == 8


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


def _failing_source(store: Storage, failures: int = 3) -> None:
    store.ensure_source(
        SourceConfig(id="palantir", employer="Palantir Technologies", adapter="lever", options={})
    )
    store.connection.execute(
        "UPDATE sources SET consecutive_failures = ? WHERE id = 'palantir'", (failures,)
    )


def test_failure_notice_is_claimed_once_per_episode(store: Storage) -> None:
    _failing_source(store)

    assert store.claim_failure_notice("palantir", now="t1", threshold=3)
    assert not store.claim_failure_notice("palantir", now="t2", threshold=3)

    store.record_failure_notice_outcome("palantir", outcome="sent")

    assert not store.claim_failure_notice("palantir", now="t3", threshold=3)
    assert store.get_source_state("palantir").failure_notice_attempts == 1


def test_failed_notice_may_be_retried_but_ambiguous_may_not(store: Storage) -> None:
    _failing_source(store)

    assert store.claim_failure_notice("palantir", now="t1", threshold=3)
    store.record_failure_notice_outcome("palantir", outcome="failed")
    assert store.claim_failure_notice("palantir", now="t2", threshold=3)

    store.record_failure_notice_outcome("palantir", outcome="ambiguous")
    assert not store.claim_failure_notice("palantir", now="t3", threshold=3), (
        "an ambiguous send may have been delivered, so it is never retried (D-024)"
    )


def test_failed_notice_retries_stop_at_the_attempt_cap(store: Storage) -> None:
    _failing_source(store)
    claims = 0
    for index in range(10):
        if not store.claim_failure_notice("palantir", now=f"t{index}", threshold=3):
            break
        claims += 1
        store.record_failure_notice_outcome("palantir", outcome="failed")

    assert claims == FAILURE_NOTICE_MAX_ATTEMPTS
    assert store.get_source_state("palantir").failure_notice_attempts == FAILURE_NOTICE_MAX_ATTEMPTS


def test_notice_below_threshold_is_never_claimed(store: Storage) -> None:
    _failing_source(store, failures=2)

    assert not store.claim_failure_notice("palantir", now="t1", threshold=3)


def test_completed_scan_resets_the_attempt_budget(store: Storage, scan, record) -> None:
    scan([record("a", "Graduate Software Engineer")])
    _failing_source(store)
    assert store.claim_failure_notice("palantir", now="t1", threshold=3)
    store.record_failure_notice_outcome("palantir", outcome="failed")

    scan([record("a", "Graduate Software Engineer")])

    state = store.get_source_state("palantir")
    assert state.consecutive_failures == 0
    assert state.failure_notice_attempts == 0
    assert state.failure_notice_outcome is None


def test_closed_job_row_keeps_identity(store: Storage, scan, record) -> None:
    scan([record("a", "Graduate Software Engineer"), record("b", "Graduate Data Engineer")])
    scan([record("a", "Graduate Software Engineer")])
    scan([record("a", "Graduate Software Engineer")])

    closed = store.get_job("palantir", "b")
    assert closed.status is JobStatus.CLOSED
    assert closed.closed_at is not None
    assert closed.job.title == "Graduate Data Engineer"


# ---------------------------------------------------------------------------
# The digest cache key: a counter that moves only when a result could differ
# ---------------------------------------------------------------------------


def test_the_jobs_change_counter_moves_for_a_new_role(store: Storage, scan, record) -> None:
    before = store.jobs_change_seq()

    scan([record("a", "Graduate Software Engineer")])

    assert store.jobs_change_seq() > before


def test_scan_bookkeeping_alone_does_not_move_the_counter(store: Storage, scan, record) -> None:
    """Re-seeing the same board is not a change: a cached digest must survive it."""
    payload = [record("a", "Graduate Software Engineer")]
    scan(payload)
    before = store.jobs_change_seq()

    scan(payload)
    scan(payload)

    assert store.jobs_change_seq() == before


def test_a_closed_role_moves_the_counter(store: Storage, scan, record) -> None:
    scan([record("a", "Graduate Software Engineer"), record("b", "Graduate Data Engineer")])
    scan([record("a", "Graduate Software Engineer")])
    before = store.jobs_change_seq()

    scan([record("a", "Graduate Software Engineer")])  # 'b' is now closed

    assert store.get_job("palantir", "b").status is JobStatus.CLOSED
    assert store.jobs_change_seq() > before, "a role that closed must not stay in a cached digest"


def test_a_retitled_role_moves_the_counter(store: Storage, scan, record) -> None:
    scan([record("a", "Graduate Software Engineer")])
    before = store.jobs_change_seq()

    scan([record("a", "Graduate Data Engineer")])

    assert store.jobs_change_seq() > before


def test_a_changed_application_link_moves_the_counter(store: Storage, scan, record) -> None:
    scan([record("a", "Graduate Software Engineer")])
    before = store.jobs_change_seq()

    scan([record("a", "Graduate Software Engineer", url="https://example.test/moved")])

    assert store.jobs_change_seq() > before


def test_becoming_eligible_for_a_digest_moves_the_counter(store: Storage, scan, record) -> None:
    scan([record("a", "Graduate Software Engineer")])
    store.connection.execute("UPDATE jobs SET first_complete_scan_id = NULL")
    before = store.jobs_change_seq()

    store.connection.execute("UPDATE jobs SET first_complete_scan_id = 1 WHERE source_job_id = 'a'")

    assert store.jobs_change_seq() > before


def test_stored_match_decisions_do_not_move_the_counter(store: Storage, scan, record) -> None:
    """The digest re-decides with the user's own rules, so the operator's view is not a change."""
    scan([record("a", "Graduate Software Engineer")])
    before = store.jobs_change_seq()

    store.connection.execute(
        "UPDATE jobs SET match_reason = '[\"different\"]' WHERE source_job_id = 'a'"
    )

    assert store.jobs_change_seq() == before
