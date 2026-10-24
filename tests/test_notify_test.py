"""`jobscout notify-test`: prove the Telegram path without inventing a job (D-088).

The distinction these tests defend is the whole point of the command: a successful test
proves the token, chat id and transport work. It says nothing about the matching-to-alert
path, and it must leave the product's own records untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import jobscout.cli as cli
from jobscout.models import DeliveryOutcome, DeliveryResult
from jobscout.storage import Storage

DB = "data/jobscout.db"


class FakeTelegram:
    """Stands in for the real transport; `TelegramTransport(config, poster=...)`."""

    calls: list[tuple[str, str]] = []
    result: DeliveryResult = DeliveryResult(DeliveryOutcome.SENT, message_id="42")

    def __init__(self, config, *, poster=None) -> None:
        self.config = config

    def send(self, chat_id: str, text: str) -> DeliveryResult:
        FakeTelegram.calls.append((chat_id, text))
        return FakeTelegram.result


class FakeRaisingTelegram:
    def __init__(self, config, *, poster=None) -> None:
        pass

    def send(self, chat_id: str, text: str) -> DeliveryResult:
        raise RuntimeError("socket exploded")


def telegram_config():
    return type("T", (), {"token": "123456789:" + "A" * 30, "chat_id": "555000111"})()


@pytest.fixture(autouse=True)
def reset_fake() -> None:
    FakeTelegram.calls = []
    FakeTelegram.result = DeliveryResult(DeliveryOutcome.SENT, message_id="42")


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(cli, "load_telegram_config", lambda *a, **k: telegram_config())
    monkeypatch.setattr(cli, "TelegramTransport", FakeTelegram)
    return tmp_path / "ops.db"


def test_a_successful_check_records_the_test_and_nothing_else(db: Path, capsys) -> None:
    exit_code = cli.main(["notify-test", "--db", str(db)])
    out = capsys.readouterr().out

    assert exit_code == cli.EXIT_OK
    assert "Delivery check: sent" in out
    assert "Telegram message id: 42" in out
    assert "does not prove the matching-to-alert path" in out
    assert len(FakeTelegram.calls) == 1

    chat_id, text = FakeTelegram.calls[0]
    assert chat_id == "555000111"
    assert text.startswith("Job-Scout delivery check")
    assert "not a job alert" in text

    with Storage.open(db) as storage:
        checks = storage.list_delivery_checks()
        assert [check.outcome for check in checks] == ["sent"]
        # No alert history, no jobs, no source state.
        assert storage.connection.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0
        assert storage.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert storage.connection.execute("SELECT count(*) FROM sources").fetchone()[0] == 0
        assert storage.connection.execute("SELECT count(*) FROM scans").fetchone()[0] == 0


def test_an_undelivered_check_fails_loudly_and_is_recorded(db: Path, capsys) -> None:
    FakeTelegram.result = DeliveryResult(DeliveryOutcome.PERMANENT, error="chat not found")

    exit_code = cli.main(["notify-test", "--db", str(db)])
    captured = capsys.readouterr()

    assert exit_code == cli.EXIT_FAILED
    assert "Delivery check: failed" in captured.out
    assert "chat not found" in captured.out
    assert "was not delivered" in captured.err

    with Storage.open(db) as storage:
        checks = storage.list_delivery_checks()
        assert [(check.outcome, check.last_error) for check in checks] == [
            ("failed", "chat not found")
        ]


def test_an_ambiguous_outcome_is_named_rather_than_guessed(db: Path, capsys) -> None:
    FakeTelegram.result = DeliveryResult(
        DeliveryOutcome.AMBIGUOUS, error="request timed out (delivery state unknown)"
    )

    exit_code = cli.main(["notify-test", "--db", str(db)])
    out = capsys.readouterr().out

    assert exit_code == cli.EXIT_FAILED
    assert "Delivery check: ambiguous" in out
    with Storage.open(db) as storage:
        assert storage.list_delivery_checks()[0].outcome == "ambiguous"


def test_a_raising_transport_is_recorded_as_a_failure(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(cli, "TelegramTransport", FakeRaisingTelegram)

    exit_code = cli.main(["notify-test", "--db", str(db)])
    out = capsys.readouterr().out

    # The transport contract says it must not raise. If it does, the check is recorded as a
    # failure rather than crashing or claiming a message was sent.
    assert exit_code == cli.EXIT_FAILED
    assert "Delivery check: failed" in out
    assert "RuntimeError" in out
    with Storage.open(db) as storage:
        checks = storage.list_delivery_checks()
        assert [(check.outcome, check.telegram_message_id) for check in checks] == [
            ("failed", None),
        ]


def test_a_dry_run_sends_nothing_and_writes_nothing(db: Path, capsys) -> None:
    exit_code = cli.main(["notify-test", "--db", str(db), "--dry-run"])
    out = capsys.readouterr().out

    assert exit_code == cli.EXIT_OK
    assert "Chat id: 555000111" in out
    assert "Dry run: nothing was sent" in out
    assert "Job-Scout delivery check" in out
    assert FakeTelegram.calls == []
    assert not db.exists(), "a dry run must not even create the database"


def test_missing_credentials_are_explained(tmp_path: Path, monkeypatch, capsys) -> None:
    from jobscout.config import ConfigError

    def refuse(*_args, **_kwargs):
        raise ConfigError("set JOBSCOUT_TELEGRAM_TOKEN and JOBSCOUT_TELEGRAM_CHAT_ID")

    monkeypatch.setattr(cli, "load_telegram_config", refuse)
    monkeypatch.setattr(cli, "TelegramTransport", FakeTelegram)

    exit_code = cli.main(["notify-test", "--db", str(tmp_path / "ops.db")])
    err = capsys.readouterr().err

    assert exit_code == cli.EXIT_FAILED
    assert "JOBSCOUT_TELEGRAM_TOKEN" in err
    assert FakeTelegram.calls == []
