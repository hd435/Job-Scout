"""Deployment-facing behaviour: environment overrides, backups and the health check.

These are the pieces an unattended deployment depends on, so they are tested without a network:
an operator should be able to trust `service check` and `service backup` before pointing a
supervisor at them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jobscout.cli import main
from jobscout.config import load_config
from jobscout.service.backup import backup_database, default_backup_path
from jobscout.storage import Storage


def _write_config(tmp_path: Path, database: str = "data/jobscout.db") -> Path:
    path = tmp_path / "config.toml"
    # TOML basic strings treat backslashes as escapes, so a Windows path is written with
    # forward slashes; the loader accepts either.
    path.write_text(
        f'[jobscout]\ndatabase = "{database.replace(chr(92), "/")}"\n', encoding="utf-8"
    )
    return path


def test_database_env_overrides_the_config_file(tmp_path: Path, monkeypatch) -> None:
    path = _write_config(tmp_path)
    monkeypatch.setenv("JOBSCOUT_DATABASE", "/data/service.db")

    assert load_config(path).database_path == Path("/data/service.db")


def test_database_env_overrides_the_builtin_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # no config.toml here, so the defaults apply
    monkeypatch.setenv("JOBSCOUT_DATABASE", "/data/service.db")

    assert load_config().database_path == Path("/data/service.db")


def test_without_the_env_var_the_config_file_wins(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("JOBSCOUT_DATABASE", raising=False)
    path = _write_config(tmp_path, "data/from-file.db")

    assert load_config(path).database_path == Path("data/from-file.db")


def test_config_env_names_the_file_when_no_path_is_given(tmp_path: Path, monkeypatch) -> None:
    path = _write_config(tmp_path, "data/service.db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JOBSCOUT_DATABASE", raising=False)
    monkeypatch.setenv("JOBSCOUT_CONFIG", str(path))

    assert load_config().database_path == Path("data/service.db")


def test_backup_copies_and_verifies_a_live_database(tmp_path: Path) -> None:
    source = tmp_path / "service.db"
    storage = Storage.open(source)
    try:
        storage.record_scan_placeholder() if hasattr(storage, "record_scan_placeholder") else None
        storage.write_meta("bot_offset", "42")
    finally:
        storage.close()
    target = tmp_path / "backups" / "copy.db"

    result = backup_database(source, target)

    assert result.ok is True
    assert result.bytes > 0
    assert result.counts["jobs"] == 0
    assert target.exists()


def test_backup_default_path_is_dated_beside_the_database(tmp_path: Path) -> None:
    now = datetime(2026, 10, 25, 9, 30, tzinfo=UTC)

    path = default_backup_path(tmp_path / "service.db", now=now)

    assert path == tmp_path / "backups" / "service-20261025T093000Z.db"


def test_backup_reports_a_missing_database_as_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        backup_database(tmp_path / "absent.db", tmp_path / "copy.db")


def _service_db_with_scan(tmp_path: Path, *, finished_minutes_ago: int) -> Path:
    database = tmp_path / "service.db"
    storage = Storage.open(database)
    try:
        finished = (datetime.now(UTC) - timedelta(minutes=finished_minutes_ago)).replace(
            microsecond=0
        )
        stamp = finished.isoformat().replace("+00:00", "Z")
        with storage.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sources (id, employer, adapter, config_json, enabled,"
                " cadence_minutes) VALUES ('probe', 'Probe', 'greenhouse', '{}', 1, 30)"
            )
            conn.execute(
                """
                INSERT INTO scans (source_id, started_at, finished_at, status, complete,
                                   records_seen, records_parsed, source_reported_total,
                                   skipped_count, error_summary, is_baseline)
                VALUES ('probe', ?, ?, 'success', 1, 1, 1, 1, 0, NULL, 1)
                """,
                (stamp, stamp),
            )
    finally:
        storage.close()
    return database


def test_check_passes_when_a_complete_scan_is_recent(tmp_path: Path, capsys) -> None:
    database = _service_db_with_scan(tmp_path, finished_minutes_ago=5)
    config = _write_config(tmp_path, str(database))

    code = main(["service", "check", "--config", str(config)])

    out = capsys.readouterr().out
    assert code == 0
    assert "State: ok" in out


def test_check_fails_when_nothing_has_scanned_for_too_long(tmp_path: Path, capsys) -> None:
    database = _service_db_with_scan(tmp_path, finished_minutes_ago=300)
    config = _write_config(tmp_path, str(database))

    code = main(["service", "check", "--config", str(config)])

    out = capsys.readouterr().out
    assert code != 0
    assert "STALE" in out


def test_check_treats_a_brand_new_database_as_starting(tmp_path: Path, capsys) -> None:
    database = tmp_path / "fresh.db"
    Storage.open(database).close()
    config = _write_config(tmp_path, str(database))

    code = main(["service", "check", "--config", str(config)])

    assert code == 0
    assert "starting" in capsys.readouterr().out


def test_backup_command_reports_what_it_copied(tmp_path: Path, capsys) -> None:
    database = _service_db_with_scan(tmp_path, finished_minutes_ago=5)
    config = _write_config(tmp_path, str(database))
    target = tmp_path / "copy.db"

    code = main(["service", "backup", "--config", str(config), "--to", str(target)])

    out = capsys.readouterr().out
    assert code == 0
    assert "Integrity: ok" in out
    assert target.exists()


def test_service_status_mentions_polling_and_sources(tmp_path: Path, capsys) -> None:
    database = _service_db_with_scan(tmp_path, finished_minutes_ago=5)
    config = _write_config(tmp_path, str(database))

    code = main(["service", "status", "--config", str(config)])

    out = capsys.readouterr().out
    assert code == 0
    assert "Bot polling:" in out
    assert "Sources:" in out
    json.dumps(out)  # status output must stay plain text
