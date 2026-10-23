from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobscout.cli import main
from jobscout.config import TELEGRAM_CHAT_ID_ENV, TELEGRAM_TOKEN_ENV
from jobscout.models import DeliveryOutcome, DeliveryResult, MatchOutcome
from jobscout.notifier import Notifier
from jobscout.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures"
PALANTIR_FIXTURE = FIXTURES / "palantir_jobs.json"

BOT_TOKEN = "123456789:AAFakeTokenValueForRedactionTests"
CHAT_ID = "555000111"


def run_cli(*args: str) -> int:
    return main(list(args))


def scan_command(db: Path, fixture: Path) -> list[str]:
    return [
        "scan",
        "--source",
        "palantir",
        "--dry-run",
        "--db",
        str(db),
        "--fixture",
        str(fixture),
    ]


def test_scan_dry_run_reports_a_baseline(db, capsys) -> None:
    exit_code = run_cli(*scan_command(db, PALANTIR_FIXTURE))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "dry run, no notifications" in out
    assert "Baseline established" in out
    assert "records: seen=22 parsed=22 skipped=0" in out
    assert "Alert candidates: none" in out


def test_scan_reports_new_candidates_after_baseline(db, capsys, tmp_path) -> None:
    run_cli(*scan_command(db, PALANTIR_FIXTURE))
    capsys.readouterr()

    payload = json.loads(PALANTIR_FIXTURE.read_text(encoding="utf-8"))
    payload.append(
        {
            "id": "8a1f4c10-9999-4000-8000-000000000099",
            "text": "Graduate Machine Learning Engineer",
            "categories": {"location": "London, United Kingdom"},
            "workplaceType": "hybrid",
            "descriptionPlain": "New graduate role.",
            "hostedUrl": "https://jobs.lever.co/palantir/8a1f4c10-9999",
            "salaryRange": {
                "min": 60000,
                "max": 65000,
                "currency": "GBP",
                "interval": "per-year-salary",
            },
        }
    )
    second_fixture = tmp_path / "second.json"
    second_fixture.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = run_cli(*scan_command(db, second_fixture))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "Alert candidates (1)" in out
    assert "Graduate Machine Learning Engineer" in out
    assert "New roles: 1" in out


def test_scan_requires_dry_run_in_m1(db, capsys) -> None:
    exit_code = run_cli(
        "scan", "--source", "palantir", "--db", str(db), "--fixture", str(PALANTIR_FIXTURE)
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "--dry-run" in captured.err


def test_partial_scan_returns_a_degraded_exit_code(db, capsys, tmp_path) -> None:
    payload = json.loads(PALANTIR_FIXTURE.read_text(encoding="utf-8"))
    payload.append({"id": "8a1f4c10-9998-4000-8000-000000000098", "text": "Missing URL"})
    broken_fixture = tmp_path / "broken.json"
    broken_fixture.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = run_cli(*scan_command(db, broken_fixture))
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "Status: partial (incomplete)" in out
    assert "Skipped records (1)" in out
    assert "missing_url" in out


def test_failed_scan_returns_a_failure_exit_code(db, capsys, tmp_path) -> None:
    bad_fixture = tmp_path / "invalid.json"
    bad_fixture.write_text("{not json", encoding="utf-8")

    exit_code = run_cli(*scan_command(db, bad_fixture))
    out = capsys.readouterr().out

    assert exit_code == 2
    assert "Status: failed (incomplete)" in out


def test_unknown_source_is_rejected(db, capsys) -> None:
    exit_code = run_cli(
        "scan",
        "--source",
        "monzo",
        "--dry-run",
        "--db",
        str(db),
        "--fixture",
        str(PALANTIR_FIXTURE),
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "unknown source 'monzo'" in captured.err


def test_summary_lists_current_matches_and_is_read_only(db, capsys) -> None:
    run_cli(*scan_command(db, PALANTIR_FIXTURE))
    capsys.readouterr()

    with Storage.open(db) as storage:
        jobs_before = storage.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        scans_before = storage.count_scans("palantir")
        match_before = storage.count_by_decision("palantir", MatchOutcome.MATCH)
        review_before = storage.count_by_decision("palantir", MatchOutcome.NEEDS_REVIEW)

    exit_code = run_cli("summary", "--current", "--db", str(db))
    out = capsys.readouterr().out

    with Storage.open(db) as storage:
        assert storage.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == jobs_before
        assert storage.count_scans("palantir") == scans_before
        assert storage.count_by_decision("palantir", MatchOutcome.MATCH) == match_before
        assert storage.count_by_decision("palantir", MatchOutcome.NEEDS_REVIEW) == review_before

    assert exit_code == 0
    assert "read-only" in out
    assert f"Matches: {match_before}" in out
    assert "Needs review (not alert-eligible):" in out
    assert "Graduate Software Engineer" in out


def test_status_reports_operational_state_without_secrets(db, capsys) -> None:
    run_cli(*scan_command(db, PALANTIR_FIXTURE))
    capsys.readouterr()

    exit_code = run_cli("status", "--db", str(db))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "Baseline:" in out
    assert "Last scan: success (complete)" in out
    assert "Consecutive failures: 0" in out
    assert "Last error: none" in out
    assert "needs_review=" in out
    assert "token" not in out.casefold()
    assert "chat" not in out.casefold()


def test_summary_without_any_scan_explains_what_to_run(db, capsys) -> None:
    exit_code = run_cli("summary", "--db", str(db))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "No scans recorded yet" in out


def test_fixture_scan_uses_no_network(db, monkeypatch) -> None:
    def fail(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("network access attempted")

    monkeypatch.setattr("urllib.request.urlopen", fail)

    assert run_cli(*scan_command(db, PALANTIR_FIXTURE)) == 0


def test_status_reports_delivery_state_without_the_chat_id(
    db, capsys, fake_transport, make_notifier
) -> None:
    run_cli(*scan_command(db, PALANTIR_FIXTURE))
    capsys.readouterr()

    chat_id = "987654321"
    with Storage.open(db) as storage:
        job = storage.list_jobs_by_decision("palantir", MatchOutcome.MATCH)[0]
        storage.create_pending_notification(chat_id, job.id, now="2026-10-22T12:00:00Z")
        transport = fake_transport(
            [DeliveryResult(DeliveryOutcome.PERMANENT, error="400 Bad Request: peer not found")]
        )
        report = make_notifier(transport).deliver_pending(storage, chat_id)
    assert report.failed == 1

    exit_code = run_cli("status", "--db", str(db))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "Notifications: failed=1" in out
    assert "Last delivery error:" in out
    assert chat_id not in out


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "cli.db"


def use_telegram_env(monkeypatch) -> None:
    monkeypatch.setenv(TELEGRAM_TOKEN_ENV, BOT_TOKEN)
    monkeypatch.setenv(TELEGRAM_CHAT_ID_ENV, CHAT_ID)


def forbid_network(monkeypatch) -> None:
    def fail(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("network access attempted")

    monkeypatch.setattr("urllib.request.urlopen", fail)


def patched_notifier(monkeypatch, transport) -> None:
    monkeypatch.setattr(
        "jobscout.cli._build_notifier",
        lambda _telegram: Notifier(transport, sleep=lambda _seconds: None),
    )


def scan_with_new_role(tmp_path: Path) -> Path:
    payload = json.loads(PALANTIR_FIXTURE.read_text(encoding="utf-8"))
    payload.append(
        {
            "id": "8a1f4c10-9999-4000-8000-000000000099",
            "text": "Graduate Machine Learning Engineer",
            "categories": {"location": "London, United Kingdom"},
            "workplaceType": "hybrid",
            "descriptionPlain": "New graduate role.",
            "hostedUrl": "https://jobs.lever.co/palantir/8a1f4c10-9999",
        }
    )
    second = tmp_path / "second.json"
    second.write_text(json.dumps(payload), encoding="utf-8")
    return second


def test_scan_deliver_requires_telegram_configuration(db, capsys, monkeypatch) -> None:
    monkeypatch.delenv(TELEGRAM_TOKEN_ENV, raising=False)
    monkeypatch.delenv(TELEGRAM_CHAT_ID_ENV, raising=False)
    forbid_network(monkeypatch)

    exit_code = run_cli(
        "scan",
        "--source",
        "palantir",
        "--deliver",
        "--db",
        str(db),
        "--fixture",
        str(PALANTIR_FIXTURE),
    )
    err = capsys.readouterr().err

    assert exit_code == 2
    assert TELEGRAM_TOKEN_ENV in err
    assert TELEGRAM_CHAT_ID_ENV in err


def test_scan_rejects_dry_run_with_deliver(db) -> None:
    with pytest.raises(SystemExit) as exc:
        run_cli("scan", "--source", "palantir", "--dry-run", "--deliver", "--db", str(db))

    assert exc.value.code == 2


def test_scan_deliver_sends_only_new_matching_roles(
    db, capsys, monkeypatch, tmp_path, fake_transport
) -> None:
    run_cli(*scan_command(db, PALANTIR_FIXTURE))
    capsys.readouterr()
    second = scan_with_new_role(tmp_path)
    use_telegram_env(monkeypatch)
    forbid_network(monkeypatch)
    transport = fake_transport()
    patched_notifier(monkeypatch, transport)

    exit_code = run_cli(
        "scan", "--source", "palantir", "--deliver", "--db", str(db), "--fixture", str(second)
    )
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "Notifications queued: 1" in out
    assert "Delivery: sent=1 failed=0 ambiguous=0 retrying=0" in out
    assert len(transport.sent) == 1
    assert transport.sent[0][0] == CHAT_ID
    assert BOT_TOKEN not in out
    assert CHAT_ID not in out


def test_scan_deliver_reports_permanent_failure(db, capsys, monkeypatch, tmp_path, fake_transport):
    run_cli(*scan_command(db, PALANTIR_FIXTURE))
    capsys.readouterr()
    second = scan_with_new_role(tmp_path)
    use_telegram_env(monkeypatch)
    forbid_network(monkeypatch)
    transport = fake_transport(
        [DeliveryResult(DeliveryOutcome.PERMANENT, error="400 Bad Request: chat not found")]
    )
    patched_notifier(monkeypatch, transport)

    exit_code = run_cli(
        "scan", "--source", "palantir", "--deliver", "--db", str(db), "--fixture", str(second)
    )
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "Delivery: sent=0 failed=1 ambiguous=0 retrying=0" in out
    assert "failed permanently" in out


def test_deliver_command_retries_pending_alerts(db, capsys, monkeypatch, fake_transport) -> None:
    run_cli(*scan_command(db, PALANTIR_FIXTURE))
    capsys.readouterr()
    with Storage.open(db) as storage:
        job = storage.list_jobs_by_decision("palantir", MatchOutcome.MATCH)[0]
        storage.create_pending_notification(CHAT_ID, job.id, now="2026-10-23T00:00:00Z")
    use_telegram_env(monkeypatch)
    forbid_network(monkeypatch)
    transport = fake_transport()
    patched_notifier(monkeypatch, transport)

    exit_code = run_cli("deliver", "--db", str(db))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "Delivery: sent=1 failed=0 ambiguous=0 retrying=0" in out
    assert len(transport.sent) == 1
    assert BOT_TOKEN not in out
    assert CHAT_ID not in out


def test_deliver_command_requires_configuration(db, capsys, monkeypatch) -> None:
    monkeypatch.delenv(TELEGRAM_TOKEN_ENV, raising=False)
    monkeypatch.delenv(TELEGRAM_CHAT_ID_ENV, raising=False)

    exit_code = run_cli("deliver", "--db", str(db))
    err = capsys.readouterr().err

    assert exit_code == 2
    assert TELEGRAM_TOKEN_ENV in err


def test_deliver_command_is_idempotent(db, capsys, monkeypatch, fake_transport) -> None:
    run_cli(*scan_command(db, PALANTIR_FIXTURE))
    capsys.readouterr()
    with Storage.open(db) as storage:
        job = storage.list_jobs_by_decision("palantir", MatchOutcome.MATCH)[0]
        storage.create_pending_notification(CHAT_ID, job.id, now="2026-10-23T00:00:00Z")
    use_telegram_env(monkeypatch)
    forbid_network(monkeypatch)
    transport = fake_transport()
    patched_notifier(monkeypatch, transport)

    run_cli("deliver", "--db", str(db))
    capsys.readouterr()
    exit_code = run_cli("deliver", "--db", str(db))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "Delivery: sent=0 failed=0 ambiguous=0 retrying=0" in out
    assert len(transport.sent) == 1
