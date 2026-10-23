"""M5.4.0 trial measurement tests: windows, coverage, gaps, delivery tallies. Offline."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

import jobscout.cli as cli
from jobscout.config import SourceConfig
from jobscout.models import (
    MatchDecision,
    MatchOutcome,
    ScanResult,
    ScanStatus,
)
from jobscout.report import (
    MISSED_GAP_FACTOR,
    DeliveryTally,
    SourceTrial,
    build_trial_report,
    format_trial_report,
)
from jobscout.sources.base import build_job
from jobscout.storage import Storage

NOW = "2026-10-30T09:00:00Z"
SOURCE = SourceConfig(id="palantir", employer="Palantir Technologies", adapter="scripted")


def seed_scan(
    storage: Storage,
    started_at: str,
    *,
    status: ScanStatus = ScanStatus.SUCCESS,
    source_id: str = SOURCE.id,
) -> int:
    result = ScanResult(
        source_id=source_id,
        started_at=started_at,
        finished_at=started_at,
        status=status,
        records_seen=1,
        records_parsed=1,
        error_summary="boom" if status is ScanStatus.FAILED else None,
    )
    scan_id = storage.record_scan(result, is_baseline=False)
    storage.update_source_after_scan(source_id, result, now=started_at, is_baseline=False)
    return scan_id


def seed_notification(storage: Storage, *, created_at: str, status: str = "sent") -> int:
    scan_id = storage.latest_scan_id(SOURCE.id)
    assert scan_id is not None
    job = build_job(
        source_id=SOURCE.id,
        source_job_id=f"R{created_at}",
        employer=SOURCE.employer,
        title="Graduate Software Engineer",
        location_raw="London, United Kingdom",
        url="https://example.test/role",
        posted_at=date(2026, 1, 1),
    )
    job_id = storage.insert_job(
        job,
        scan_id=scan_id,
        decision=MatchDecision(MatchOutcome.MATCH, ("family:software",), "m1.1"),
        first_complete_scan_id=scan_id,
        now=created_at,
    )
    notification_id = storage.create_pending_notification("555000111", job_id, now=created_at)
    assert notification_id is not None
    if status == "sent":
        storage.record_notification_sent(notification_id, message_id="1", now=created_at)
    elif status == "failed":
        storage.record_notification_failed(notification_id, error="nope", now=created_at)
    elif status == "ambiguous":
        storage.record_notification_ambiguous(notification_id, error="unknown", now=created_at)
    return notification_id


@pytest.fixture
def seeded(store: Storage) -> Storage:
    store.ensure_source(SOURCE)
    return store


def rows(storage: Storage, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    return list(storage.connection.execute(sql, params))


# window and coverage ------------------------------------------------------


def test_scans_outside_the_window_are_ignored(seeded: Storage) -> None:
    seed_scan(seeded, "2026-10-01T09:00:00Z")  # 29 days before the window ends
    seed_scan(seeded, "2026-10-24T09:00:00Z")  # inside the 7-day window
    seed_scan(seeded, "2026-10-30T08:30:00Z")

    report = build_trial_report(seeded, now=NOW, days=7)

    assert report.since == "2026-10-23T09:00:00Z"
    assert report.scans == 2


def test_coverage_compares_scans_with_the_cycles_the_cadence_implies(seeded: Storage) -> None:
    for step in range(8):  # four hours of half-hourly scans inside the window
        hour = 5 + step // 2
        minute = 30 * (step % 2)
        seed_scan(seeded, f"2026-10-30T{hour:02d}:{minute:02d}:00Z")

    report = build_trial_report(seeded, now=NOW, days=1)

    source = report.sources[0]
    assert source.cadence_minutes == 30
    assert source.expected_runs == 48
    assert source.scans == 8
    assert 0.16 < source.coverage < 0.17
    assert report.missed_episodes == 0


def test_coverage_never_exceeds_one() -> None:
    source = SourceTrial(
        source_id="palantir",
        employer="Palantir Technologies",
        cadence_minutes=30,
        expected_runs=2,
        scans=5,
        complete=5,
        partial=0,
        failed=0,
        missed_episodes=0,
        largest_gap_minutes=None,
        mean_delay_minutes=None,
        consecutive_failures=0,
        last_success_at=None,
        next_run_at=None,
    )

    assert source.coverage == 1.0


def test_scan_statuses_are_counted_separately(seeded: Storage) -> None:
    seed_scan(seeded, "2026-10-23T10:00:00Z")
    seed_scan(seeded, "2026-10-23T10:30:00Z", status=ScanStatus.PARTIAL)
    seed_scan(seeded, "2026-10-23T11:00:00Z", status=ScanStatus.FAILED)
    seed_scan(seeded, "2026-10-23T11:30:00Z", status=ScanStatus.SUCCESS_EMPTY)

    source = build_trial_report(seeded, now=NOW, days=7).sources[0]

    assert (source.scans, source.complete, source.partial, source.failed) == (4, 2, 1, 1)


# gaps, delays, and missed episodes ---------------------------------------


def test_gaps_measure_downtime_between_scans(seeded: Storage) -> None:
    seed_scan(seeded, "2026-10-23T09:00:00Z")
    seed_scan(seeded, "2026-10-23T09:30:00Z")
    seed_scan(seeded, "2026-10-23T11:30:00Z")  # 120 minutes: three cycles missed

    source = build_trial_report(seeded, now=NOW, days=7).sources[0]

    assert source.largest_gap_minutes == 120.0
    assert source.missed_episodes == 1
    assert MISSED_GAP_FACTOR * source.cadence_minutes < 120.0
    # Mean delay across 30 and 120 minutes against a 30 minute cadence.
    assert source.mean_delay_minutes == 45.0


def test_a_single_scan_has_no_gap_measurement(seeded: Storage) -> None:
    seed_scan(seeded, "2026-10-23T09:00:00Z")

    source = build_trial_report(seeded, now=NOW, days=7).sources[0]

    assert source.largest_gap_minutes is None
    assert source.mean_delay_minutes is None
    assert source.missed_episodes == 0


# sources and delivery -----------------------------------------------------


def test_current_source_state_is_reported(seeded: Storage) -> None:
    seed_scan(seeded, "2026-10-23T09:00:00Z")
    seed_scan(seeded, "2026-10-23T09:30:00Z", status=ScanStatus.FAILED)
    seed_scan(seeded, "2026-10-23T10:00:00Z", status=ScanStatus.FAILED)

    source = build_trial_report(seeded, now=NOW, days=7).sources[0]

    assert source.consecutive_failures == 2
    assert source.last_success_at == "2026-10-23T09:00:00Z"
    assert source.next_run_at is None


def test_deliveries_are_tallied_by_state_inside_the_window(seeded: Storage) -> None:
    seed_scan(seeded, "2026-10-23T09:00:00Z")
    seed_notification(seeded, created_at="2026-10-24T09:00:00Z", status="sent")
    seed_notification(seeded, created_at="2026-10-24T10:00:00Z", status="failed")
    seed_notification(seeded, created_at="2026-10-24T11:00:00Z", status="ambiguous")
    seed_notification(seeded, created_at="2026-10-24T12:00:00Z", status="pending")
    seed_notification(seeded, created_at="2026-09-01T09:00:00Z", status="sent")

    report = build_trial_report(seeded, now=NOW, days=7)

    assert report.deliveries == DeliveryTally(pending=1, sent=1, failed=1, ambiguous=1)
    assert report.deliveries.total == 4


def test_report_limits_itself_to_one_source_when_asked(seeded: Storage) -> None:
    other = SourceConfig(id="acme", employer="Acme", adapter="scripted", cadence_minutes=15)
    seeded.ensure_source(other)
    seed_scan(seeded, "2026-10-23T09:00:00Z")
    seed_scan(seeded, "2026-10-23T09:00:00Z", source_id="acme")

    report = build_trial_report(seeded, now=NOW, days=7, source_id="acme")

    assert [source.source_id for source in report.sources] == ["acme"]
    assert report.scans == 1
    assert report.sources[0].expected_runs == 7 * 24 * 60 // 15


def test_an_empty_database_reports_honestly(seeded: Storage) -> None:
    report = build_trial_report(seeded, now=NOW, days=7)

    assert report.scans == 0
    assert report.sources[0].scans == 0
    assert report.sources[0].coverage == 0.0
    assert "no heartbeat" in format_trial_report(report)


def test_a_disabled_source_is_flagged_and_not_a_trial_target(seeded: Storage) -> None:
    other = SourceConfig(
        id="arm", employer="Arm", adapter="sitemap_html", enabled=False, cadence_minutes=30
    )
    seeded.ensure_source(other)
    seed_scan(seeded, "2026-10-30T08:00:00Z")

    text = format_trial_report(build_trial_report(seeded, now=NOW, days=7))

    assert "Sources: 2 recorded (1 enabled)" in text
    assert "arm (Arm) cadence 30m [disabled: not a trial target]" in text
    assert "palantir (Palantir Technologies) cadence 30m\n" in text


def test_a_non_positive_window_is_rejected(seeded: Storage) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        build_trial_report(seeded, now=NOW, days=0)


def test_the_report_writes_nothing(seeded: Storage) -> None:
    seed_scan(seeded, "2026-10-23T09:00:00Z")
    seed_notification(seeded, created_at="2026-10-24T09:00:00Z")
    before = {
        table: rows(seeded, f"SELECT COUNT(*) AS total FROM {table}")[0]["total"]
        for table in ("jobs", "scans", "notifications", "sources")
    }

    build_trial_report(seeded, now=NOW, days=7)

    after = {
        table: rows(seeded, f"SELECT COUNT(*) AS total FROM {table}")[0]["total"]
        for table in ("jobs", "scans", "notifications", "sources")
    }
    assert before == after


# formatting and CLI -------------------------------------------------------


def test_the_text_report_names_every_measured_value(seeded: Storage) -> None:
    seed_scan(seeded, "2026-10-23T09:00:00Z")
    seed_scan(seeded, "2026-10-23T11:00:00Z", status=ScanStatus.FAILED)
    seed_notification(seeded, created_at="2026-10-24T09:00:00Z")

    text = format_trial_report(build_trial_report(seeded, now=NOW, days=7))

    assert "Trial window: 2026-10-23T09:00:00Z to 2026-10-30T09:00:00Z (7 day(s))" in text
    assert "scans 2/336 cycles (1%)" in text
    assert "complete 1, partial 0, failed 1" in text
    assert "longest 120m, 1 missed episode(s)" in text
    assert "consecutive failures 1" in text
    assert "Deliveries queued in window: 1 (sent 1" in text


def test_cli_report_reads_a_database_without_touching_it(
    seeded: Storage, tmp_path: Path, capsys
) -> None:
    seed_scan(seeded, "2026-10-23T09:00:00Z")
    db_path = _database_path(seeded)

    exit_code = cli.main(["report", "--days", "7", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == cli.EXIT_OK
    assert "Trial window" in output
    assert "palantir" in output
    assert rows(seeded, "SELECT COUNT(*) AS total FROM scans")[0]["total"] == 1


def test_cli_report_rejects_an_invalid_window(tmp_path: Path, capsys) -> None:
    exit_code = cli.main(["report", "--days", "0", "--db", str(tmp_path / "empty.db")])

    assert exit_code == cli.EXIT_FAILED
    assert "at least 1" in capsys.readouterr().err


def test_cli_report_refuses_to_invent_a_database(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing.db"

    exit_code = cli.main(["report", "--db", str(missing)])

    assert exit_code == cli.EXIT_FAILED
    assert "database not found" in capsys.readouterr().err
    assert not missing.exists()


def _database_path(storage: Storage) -> Path:
    row = rows(storage, "PRAGMA database_list")[0]
    return Path(row["file"])
