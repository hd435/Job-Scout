"""M5.3 retention tests: boundaries, protection, idempotency, and failure cases."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import jobscout.cli as cli
from jobscout.config import SourceConfig
from jobscout.matching import DEFAULT_RULES
from jobscout.models import (
    JobStatus,
    MatchDecision,
    MatchOutcome,
    ScanResult,
    ScanStatus,
)
from jobscout.retention import RetentionPolicy, cutoff
from jobscout.scanner import run_scan
from jobscout.sources.base import build_job
from jobscout.storage import Storage

NOW = "2026-10-23T12:00:00Z"
SOURCE = SourceConfig(id="palantir", employer="Palantir Technologies", adapter="scripted")


def source_config() -> SourceConfig:
    return SOURCE


def iso_days_ago(days: int) -> str:
    return cutoff(NOW, days)


def seed_scan(storage: Storage, started_at: str, *, complete: bool = True) -> int:
    result = ScanResult(
        source_id=SOURCE.id,
        started_at=started_at,
        finished_at=started_at,
        status=ScanStatus.SUCCESS if complete else ScanStatus.PARTIAL,
        records_seen=1,
        records_parsed=1,
    )
    return storage.record_scan(result, is_baseline=False)


def seed_job(
    storage: Storage,
    *,
    job_id: str = "R1",
    scan_id: int,
    created_at: str,
    closed_at: str | None = None,
    description: str | None = "A role description.",
    salary: str | None = "£50,000 per year",
) -> int:
    job = build_job(
        source_id=SOURCE.id,
        source_job_id=job_id,
        employer=SOURCE.employer,
        title="Graduate Software Engineer",
        location_raw="London, United Kingdom",
        url=f"https://example.test/{job_id}",
        description=description,
        salary_text=salary,
        posted_at=date(2026, 1, 1),
    )
    db_id = storage.insert_job(
        job,
        scan_id=scan_id,
        decision=MatchDecision(MatchOutcome.MATCH, ("family:software",), "m1.1"),
        first_complete_scan_id=scan_id,
        now=created_at,
    )
    if closed_at is not None:
        storage.close_job(db_id, now=closed_at)
    return db_id


def seed_notification(
    storage: Storage,
    job_id: int,
    *,
    created_at: str,
    status: str = "sent",
    error: str | None = None,
) -> int:
    notification_id = storage.create_pending_notification("555000111", job_id, now=created_at)
    assert notification_id is not None
    if status == "sent":
        storage.record_notification_sent(notification_id, message_id="1", now=created_at)
    elif status == "failed":
        storage.record_notification_failed(
            notification_id, error=error or "permanent failure", now=created_at
        )
    return notification_id


@pytest.fixture
def seeded(store: Storage) -> Storage:
    store.ensure_source(SOURCE)
    return store


def rows(storage: Storage, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return list(storage.connection.execute(sql, params))


# boundaries ---------------------------------------------------------------


def test_open_jobs_are_never_pruned_or_stripped(seeded: Storage) -> None:
    old = iso_days_ago(800)
    scan_id = seed_scan(seeded, old)
    seed_job(seeded, scan_id=scan_id, created_at=old)

    report = seeded.prune(RetentionPolicy(), now=NOW)

    row = seeded.get_job(SOURCE.id, "R1")
    assert row is not None
    assert row.status is JobStatus.OPEN
    assert row.job.description == "A role description."
    assert row.job.salary_text == "£50,000 per year"
    assert report.deleted_jobs == 0
    assert report.cleared_job_content == 0


def test_closed_content_is_cleared_only_after_the_content_horizon(seeded: Storage) -> None:
    old_scan = seed_scan(seeded, iso_days_ago(400))
    fresh_scan = seed_scan(seeded, iso_days_ago(5))
    seed_job(
        seeded,
        job_id="OLD",
        scan_id=old_scan,
        created_at=iso_days_ago(400),
        closed_at=iso_days_ago(40),
    )
    seed_job(
        seeded,
        job_id="NEW",
        scan_id=fresh_scan,
        created_at=iso_days_ago(5),
        closed_at=iso_days_ago(10),
    )

    report = seeded.prune(RetentionPolicy(), now=NOW)

    old = seeded.get_job(SOURCE.id, "OLD")
    fresh = seeded.get_job(SOURCE.id, "NEW")
    assert old is not None and old.job.description is None and old.job.salary_text is None
    assert old.job.title == "Graduate Software Engineer"  # identity and metadata kept
    assert old.job.url.startswith("https://example.test/")
    assert fresh is not None and fresh.job.description == "A role description."
    assert report.cleared_job_content == 1


def test_closed_identity_is_deleted_after_a_year_taking_its_notifications(seeded: Storage) -> None:
    old_scan = seed_scan(seeded, iso_days_ago(500))
    old_job = seed_job(
        seeded,
        job_id="OLD",
        scan_id=old_scan,
        created_at=iso_days_ago(500),
        closed_at=iso_days_ago(400),
    )
    seed_notification(seeded, old_job, created_at=iso_days_ago(480))

    kept_scan = seed_scan(seeded, iso_days_ago(330))
    kept_job = seed_job(
        seeded,
        job_id="KEPT",
        scan_id=kept_scan,
        created_at=iso_days_ago(330),
        closed_at=iso_days_ago(300),
    )
    seed_notification(seeded, kept_job, created_at=iso_days_ago(320))

    report = seeded.prune(RetentionPolicy(), now=NOW)

    assert seeded.get_job(SOURCE.id, "OLD") is None
    assert rows(seeded, "SELECT * FROM notifications WHERE job_id = ?", (old_job,)) == []
    assert seeded.get_job(SOURCE.id, "KEPT") is not None
    assert len(rows(seeded, "SELECT * FROM notifications WHERE job_id = ?", (kept_job,))) == 1
    assert report.deleted_jobs == 1
    assert report.deleted_notifications == 1


def test_scan_history_is_pruned_only_when_nothing_references_it(seeded: Storage) -> None:
    referenced = seed_scan(seeded, iso_days_ago(200))
    unreferenced = seed_scan(seeded, iso_days_ago(200))
    recent = seed_scan(seeded, iso_days_ago(10))
    seed_job(seeded, scan_id=referenced, created_at=iso_days_ago(200))
    seeded.connection.execute(
        "UPDATE sources SET baseline_scan_id = ? WHERE id = ?", (unreferenced, SOURCE.id)
    )

    report = seeded.prune(RetentionPolicy(), now=NOW)

    surviving = {row["id"] for row in rows(seeded, "SELECT id FROM scans")}
    assert referenced in surviving  # first_seen for an open job
    assert unreferenced in surviving  # the source's baseline scan
    assert recent in surviving
    assert report.deleted_scans == 0


def test_an_unreferenced_old_scan_is_deleted(seeded: Storage) -> None:
    old = seed_scan(seeded, iso_days_ago(200))
    recent = seed_scan(seeded, iso_days_ago(10))

    report = seeded.prune(RetentionPolicy(), now=NOW)

    surviving = {row["id"] for row in rows(seeded, "SELECT id FROM scans")}
    assert old not in surviving
    assert recent in surviving
    assert report.deleted_scans == 1


def test_delivery_error_text_is_cleared_but_the_record_stays(seeded: Storage) -> None:
    scan_id = seed_scan(seeded, iso_days_ago(300))
    job_id = seed_job(seeded, scan_id=scan_id, created_at=iso_days_ago(300))
    old_notification = seed_notification(
        seeded,
        job_id,
        created_at=iso_days_ago(100),
        status="failed",
        error="http 500 at example.test/x",
    )

    report = seeded.prune(RetentionPolicy(), now=NOW)

    row = rows(seeded, "SELECT * FROM notifications WHERE id = ?", (old_notification,))[0]
    assert row["last_error"] is None
    assert row["status"] == "failed"  # outcome retained for diagnosis
    assert report.cleared_notification_errors == 1


# safety and idempotency ---------------------------------------------------


def test_prune_is_idempotent(seeded: Storage) -> None:
    old_scan = seed_scan(seeded, iso_days_ago(500))
    job_id = seed_job(
        seeded, scan_id=old_scan, created_at=iso_days_ago(500), closed_at=iso_days_ago(400)
    )
    seed_notification(seeded, job_id, created_at=iso_days_ago(480))

    first = seeded.prune(RetentionPolicy(), now=NOW)
    second = seeded.prune(RetentionPolicy(), now=NOW)

    assert first.total_changes > 0
    assert second.total_changes == 0
    assert second.truncated is False


def test_prune_respects_the_batch_limit_and_reports_remaining_work(seeded: Storage) -> None:
    for index in range(3):
        scan_id = seed_scan(seeded, iso_days_ago(500))
        seed_job(
            seeded,
            job_id=f"R{index}",
            scan_id=scan_id,
            created_at=iso_days_ago(500),
            closed_at=iso_days_ago(400),
        )
    policy = RetentionPolicy(batch_size=1, max_batches=1)

    first = seeded.prune(policy, now=NOW)
    second = seeded.prune(policy, now=NOW)

    assert first.deleted_jobs == 1
    assert first.truncated is True
    assert second.deleted_jobs == 1

    remaining = seeded.prune(RetentionPolicy(), now=NOW)
    assert remaining.deleted_jobs == 1
    assert rows(seeded, "SELECT * FROM jobs WHERE status = 'closed'") == []


def test_dry_run_reports_without_changing_anything(seeded: Storage) -> None:
    old_scan = seed_scan(seeded, iso_days_ago(500))
    seed_job(seeded, scan_id=old_scan, created_at=iso_days_ago(500), closed_at=iso_days_ago(400))
    before = rows(seeded, "SELECT COUNT(*) AS total FROM jobs")[0]["total"]

    report = seeded.prune(RetentionPolicy(), now=NOW, dry_run=True)

    after = rows(seeded, "SELECT COUNT(*) AS total FROM jobs")[0]["total"]
    assert report.deleted_jobs == 1
    assert report.dry_run is True
    assert before == after
    assert seeded.get_job(SOURCE.id, "R1") is not None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"closed_content_days": -1},
        {"closed_job_days": 0},
        {"scan_days": -5},
        {"batch_size": 0},
        {"max_batches": 0},
        {"closed_content_days": 60, "closed_job_days": 30},
    ],
)
def test_invalid_policies_are_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        RetentionPolicy(**kwargs)


# lifecycle safety ---------------------------------------------------------


class FixedAdapter:
    adapter_id = "scripted"

    def __init__(self, jobs: tuple) -> None:
        self._jobs = jobs

    def fetch_scan(self, config, http_client=None, *, known=None) -> ScanResult:
        return ScanResult(
            source_id=config.id,
            started_at=NOW,
            finished_at=NOW,
            status=ScanStatus.SUCCESS,
            records_seen=len(self._jobs),
            records_parsed=len(self._jobs),
            jobs=self._jobs,
        )


def job_for(job_id: str) -> Any:
    return build_job(
        source_id=SOURCE.id,
        source_job_id=job_id,
        employer=SOURCE.employer,
        title="Graduate Software Engineer",
        location_raw="London, United Kingdom",
        url=f"https://example.test/{job_id}",
        description="A role description.",
    )


def test_a_retained_closed_role_does_not_re_alert_after_pruning(seeded: Storage) -> None:
    scan_id = seed_scan(seeded, iso_days_ago(300))
    seed_job(
        seeded,
        job_id="R1",
        scan_id=scan_id,
        created_at=iso_days_ago(300),
        closed_at=iso_days_ago(300),
    )
    seeded.prune(RetentionPolicy(), now=NOW)
    assert seeded.get_job(SOURCE.id, "R1") is not None

    outcome = run_scan(
        seeded,
        FixedAdapter((job_for("R1"),)),
        SOURCE,
        http_client=None,  # type: ignore[arg-type]
        notify_chat_id="555000111",
        rules=DEFAULT_RULES,
    )

    assert outcome.candidates == ()
    assert outcome.notifications_queued == 0


def test_an_open_role_keeps_its_identity_so_it_is_not_re_alerted(seeded: Storage) -> None:
    scan_id = seed_scan(seeded, iso_days_ago(400))
    seed_job(seeded, job_id="R1", scan_id=scan_id, created_at=iso_days_ago(400))

    first = run_scan(
        seeded,
        FixedAdapter((job_for("R1"),)),
        SOURCE,
        http_client=None,  # type: ignore[arg-type]
        notify_chat_id="555000111",
        rules=DEFAULT_RULES,
    )
    seeded.prune(RetentionPolicy(), now=NOW)
    second = run_scan(
        seeded,
        FixedAdapter((job_for("R1"),)),
        SOURCE,
        http_client=None,  # type: ignore[arg-type]
        notify_chat_id="555000111",
        rules=DEFAULT_RULES,
    )

    assert first.new_jobs == 0
    assert second.new_jobs == 0
    assert second.candidates == ()
    assert second.notifications_queued == 0
    assert seeded.get_job(SOURCE.id, "R1").status is JobStatus.OPEN


def test_prune_never_runs_as_part_of_a_scan(seeded: Storage) -> None:
    """Retention is explicit: a scan must not delete anything, however old the data."""
    old_scan = seed_scan(seeded, iso_days_ago(500))
    seed_job(seeded, scan_id=old_scan, created_at=iso_days_ago(500), closed_at=iso_days_ago(400))

    run_scan(
        seeded,
        FixedAdapter(()),
        SOURCE,
        http_client=None,  # type: ignore[arg-type]
        rules=DEFAULT_RULES,
    )

    stale = seeded.get_job(SOURCE.id, "R1")
    assert stale is not None
    assert stale.status is JobStatus.CLOSED
    assert stale.job.description == "A role description."


def assert_fk_clean(storage: Storage) -> None:
    violations = list(storage.connection.execute("PRAGMA foreign_key_check"))
    assert violations == [], f"foreign key violations: {violations}"


def seed_expiring_job(
    storage: Storage,
    job_id: str,
    *,
    closed_days_ago: int,
    notification_days_ago: int | None = None,
) -> int:
    scan_id = seed_scan(storage, iso_days_ago(closed_days_ago + 10))
    job_id_value = seed_job(
        storage,
        job_id=job_id,
        scan_id=scan_id,
        created_at=iso_days_ago(closed_days_ago + 10),
        closed_at=iso_days_ago(closed_days_ago),
    )
    if notification_days_ago is not None:
        seed_notification(storage, job_id_value, created_at=iso_days_ago(notification_days_ago))
    return job_id_value


# batching across notifications and jobs -----------------------------------


def test_batches_advance_past_jobs_that_never_had_notifications(seeded: Storage) -> None:
    """The old code re-selected the same first batch and stopped when it had no notifications."""
    seed_expiring_job(seeded, "A", closed_days_ago=420)
    seed_expiring_job(seeded, "B", closed_days_ago=410)
    seed_expiring_job(seeded, "C", closed_days_ago=400, notification_days_ago=390)
    seed_expiring_job(seeded, "D", closed_days_ago=390, notification_days_ago=380)
    policy = RetentionPolicy(batch_size=2, max_batches=2)

    report = seeded.prune(policy, now=NOW)

    assert report.deleted_jobs == 4
    assert report.deleted_notifications == 2
    assert report.truncated is False
    assert rows(seeded, "SELECT * FROM jobs WHERE status = 'closed'") == []
    assert rows(seeded, "SELECT * FROM notifications") == []
    assert_fk_clean(seeded)


def test_a_batch_limit_stops_between_batches_rather_than_mid_batch(seeded: Storage) -> None:
    seed_expiring_job(seeded, "A", closed_days_ago=420)
    seed_expiring_job(seeded, "B", closed_days_ago=410, notification_days_ago=400)
    seed_expiring_job(seeded, "C", closed_days_ago=400, notification_days_ago=390)
    policy = RetentionPolicy(batch_size=2, max_batches=1)

    first = seeded.prune(policy, now=NOW)

    assert first.deleted_jobs == 2
    assert first.deleted_notifications == 1
    assert first.truncated is True
    assert [row["source_job_id"] for row in rows(seeded, "SELECT source_job_id FROM jobs")] == ["C"]
    assert len(rows(seeded, "SELECT * FROM notifications")) == 1
    assert_fk_clean(seeded)

    second = seeded.prune(policy, now=NOW)

    assert second.deleted_jobs == 1
    assert second.deleted_notifications == 1
    assert second.truncated is False
    assert rows(seeded, "SELECT * FROM jobs") == []
    assert_fk_clean(seeded)


def test_many_expiring_jobs_need_several_runs_and_converge(seeded: Storage) -> None:
    for index in range(7):
        seed_expiring_job(
            seeded,
            f"R{index}",
            closed_days_ago=400 + index,
            notification_days_ago=390 + index if index % 2 else None,
        )
    policy = RetentionPolicy(batch_size=2, max_batches=1)

    runs = []
    for _ in range(10):
        report = seeded.prune(policy, now=NOW)
        runs.append(report)
        assert_fk_clean(seeded)
        if not report.truncated:
            break

    assert [report.deleted_jobs for report in runs] == [2, 2, 2, 1]
    assert sum(report.deleted_notifications for report in runs) == 3
    assert rows(seeded, "SELECT * FROM jobs") == []
    assert rows(seeded, "SELECT * FROM notifications") == []
    assert seeded.prune(policy, now=NOW).total_changes == 0


# notification error age ---------------------------------------------------


def test_recent_error_on_an_old_notification_is_kept(seeded: Storage) -> None:
    scan_id = seed_scan(seeded, iso_days_ago(300))
    job_id = seed_job(seeded, scan_id=scan_id, created_at=iso_days_ago(300))
    notification_id = seed_notification(seeded, job_id, created_at=iso_days_ago(200))
    seeded.record_notification_failed(notification_id, error="recent failure", now=iso_days_ago(5))

    report = seeded.prune(RetentionPolicy(), now=NOW)

    row = rows(seeded, "SELECT * FROM notifications WHERE id = ?", (notification_id,))[0]
    assert row["last_error"] == "recent failure"
    assert report.cleared_notification_errors == 0


def test_old_error_on_an_old_notification_is_cleared(seeded: Storage) -> None:
    scan_id = seed_scan(seeded, iso_days_ago(400))
    job_id = seed_job(seeded, scan_id=scan_id, created_at=iso_days_ago(400))
    notification_id = seed_notification(seeded, job_id, created_at=iso_days_ago(300))
    seeded.record_notification_failed(notification_id, error="stale failure", now=iso_days_ago(200))

    report = seeded.prune(RetentionPolicy(), now=NOW)

    row = rows(seeded, "SELECT * FROM notifications WHERE id = ?", (notification_id,))[0]
    assert row["last_error"] is None
    assert row["status"] == "failed"
    assert report.cleared_notification_errors == 1


def test_error_clearing_falls_back_to_creation_time_when_never_attempted(seeded: Storage) -> None:
    scan_id = seed_scan(seeded, iso_days_ago(400))
    job_id = seed_job(seeded, scan_id=scan_id, created_at=iso_days_ago(400))
    notification_id = seed_notification(seeded, job_id, created_at=iso_days_ago(200))
    seeded.connection.execute(
        "UPDATE notifications SET last_error = ?, last_attempt_at = NULL WHERE id = ?",
        ("unattempted failure", notification_id),
    )

    report = seeded.prune(RetentionPolicy(), now=NOW)

    row = rows(seeded, "SELECT * FROM notifications WHERE id = ?", (notification_id,))[0]
    assert row["last_error"] is None
    assert report.cleared_notification_errors == 1


# dry run versus real run --------------------------------------------------


def test_dry_run_predicts_a_single_real_run_under_the_same_limits(seeded: Storage) -> None:
    for index in range(5):
        seed_expiring_job(
            seeded,
            f"R{index}",
            closed_days_ago=400 + index,
            notification_days_ago=390 + index if index % 2 == 0 else None,
        )
    seed_scan(seeded, iso_days_ago(400))
    for index in range(3):
        notification_scan = seed_scan(seeded, iso_days_ago(300))
        job_id = seed_job(
            seeded, job_id=f"E{index}", scan_id=notification_scan, created_at=iso_days_ago(300)
        )
        notification_id = seed_notification(seeded, job_id, created_at=iso_days_ago(200))
        seeded.record_notification_failed(
            notification_id, error=f"error {index}", now=iso_days_ago(150)
        )
    policy = RetentionPolicy(batch_size=2, max_batches=1)

    before = {
        table: rows(seeded, f"SELECT COUNT(*) AS total FROM {table}")[0]["total"]
        for table in ("jobs", "notifications", "scans")
    }
    dry = seeded.prune(policy, now=NOW, dry_run=True)
    after_dry = {
        table: rows(seeded, f"SELECT COUNT(*) AS total FROM {table}")[0]["total"]
        for table in ("jobs", "notifications", "scans")
    }
    real = seeded.prune(policy, now=NOW)

    assert dry.dry_run is True
    assert before == after_dry  # nothing written
    assert (dry.cleared_job_content, dry.deleted_jobs, dry.deleted_notifications) == (
        real.cleared_job_content,
        real.deleted_jobs,
        real.deleted_notifications,
    )
    assert dry.cleared_notification_errors == real.cleared_notification_errors
    assert dry.deleted_scans == real.deleted_scans
    assert dry.truncated == real.truncated is True
    assert_fk_clean(seeded)


def test_dry_run_reports_truncation_when_the_backlog_exceeds_the_cap(seeded: Storage) -> None:
    for index in range(4):
        seed_expiring_job(seeded, f"R{index}", closed_days_ago=400 + index)
    policy = RetentionPolicy(batch_size=1, max_batches=2)

    report = seeded.prune(policy, now=NOW, dry_run=True)

    assert report.deleted_jobs == 2
    assert report.truncated is True
    assert len(rows(seeded, "SELECT * FROM jobs")) == 4


# truncation in every phase ------------------------------------------------


def test_content_clearing_reports_truncation(seeded: Storage) -> None:
    for index in range(3):
        seed_expiring_job(seeded, f"R{index}", closed_days_ago=40 + index)
    policy = RetentionPolicy(batch_size=1, max_batches=1)

    first = seeded.prune(policy, now=NOW)
    second = seeded.prune(policy, now=NOW)

    assert first.cleared_job_content == 1
    assert first.truncated is True
    assert second.cleared_job_content == 1
    assert second.truncated is True
    third = seeded.prune(policy, now=NOW)
    assert third.cleared_job_content == 1
    assert third.truncated is False


def test_error_clearing_reports_truncation(seeded: Storage) -> None:
    scan_id = seed_scan(seeded, iso_days_ago(400))
    for index in range(3):
        other_job = seed_job(
            seeded,
            job_id=f"E{index}",
            scan_id=scan_id,
            created_at=iso_days_ago(400),
        )
        notification_id = seed_notification(seeded, other_job, created_at=iso_days_ago(300))
        seeded.record_notification_failed(
            notification_id, error=f"error {index}", now=iso_days_ago(200)
        )
    policy = RetentionPolicy(batch_size=1, max_batches=1)

    first = seeded.prune(policy, now=NOW)

    assert first.cleared_notification_errors == 1
    assert first.truncated is True

    second = seeded.prune(policy, now=NOW)
    assert second.cleared_notification_errors == 1
    assert second.truncated is True

    third = seeded.prune(policy, now=NOW)
    assert third.cleared_notification_errors == 1
    assert third.truncated is False


def test_scan_deletion_reports_truncation(seeded: Storage) -> None:
    for _ in range(3):
        seed_scan(seeded, iso_days_ago(200))
    policy = RetentionPolicy(batch_size=1, max_batches=1)

    first = seeded.prune(policy, now=NOW)
    second = seeded.prune(policy, now=NOW)
    third = seeded.prune(policy, now=NOW)

    assert (first.deleted_scans, first.truncated) == (1, True)
    assert (second.deleted_scans, second.truncated) == (1, True)
    assert (third.deleted_scans, third.truncated) == (1, False)


# CLI ----------------------------------------------------------------------


def test_cli_dry_run_reports_and_changes_nothing(seeded: Storage, tmp_path: Path, capsys) -> None:
    old_scan = seed_scan(seeded, iso_days_ago(500))
    seed_job(seeded, scan_id=old_scan, created_at=iso_days_ago(500), closed_at=iso_days_ago(400))
    db_path = tmp_path / "cli.db"
    seeded.close()
    with Storage.open(db_path) as target:
        target.ensure_source(SOURCE)
        scan_id = seed_scan(target, iso_days_ago(500))
        seed_job(target, scan_id=scan_id, created_at=iso_days_ago(500), closed_at=iso_days_ago(400))

    exit_code = cli.main(["prune", "--dry-run", "--db", str(db_path)])
    out = capsys.readouterr().out

    assert exit_code == cli.EXIT_OK
    assert "Dry run: nothing was changed." in out
    with Storage.open(db_path) as verify:
        assert verify.get_job(SOURCE.id, "R1") is not None


def test_cli_applies_the_policy(seeded: Storage, tmp_path: Path, capsys, monkeypatch) -> None:
    db_path = tmp_path / "cli.db"
    seeded.close()
    with Storage.open(db_path) as target:
        target.ensure_source(SOURCE)
        scan_id = seed_scan(target, iso_days_ago(500))
        seed_job(target, scan_id=scan_id, created_at=iso_days_ago(500), closed_at=iso_days_ago(400))

    fixed_now = "2026-10-23T12:00:00Z"
    monkeypatch.setattr(cli, "utc_now_iso", lambda: fixed_now)

    exit_code = cli.main(["prune", "--db", str(db_path)])
    out = capsys.readouterr().out

    assert exit_code == cli.EXIT_OK
    assert "Pruned delete 1 closed role(s)" in out
    with Storage.open(db_path) as verify:
        assert verify.get_job(SOURCE.id, "R1") is None


def test_cli_rejects_an_invalid_policy(tmp_path: Path, capsys) -> None:
    exit_code = cli.main(["prune", "--closed-content-days", "400", "--closed-job-days", "30"])

    assert exit_code == cli.EXIT_FAILED
    assert "closed_job_days" in capsys.readouterr().err
