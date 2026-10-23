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
