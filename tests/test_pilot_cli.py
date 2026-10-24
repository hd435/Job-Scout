"""The CLI side of the pilot: `rehearsal`, and what `summary` says when it is quiet (D-090)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import jobscout.cli as cli
import jobscout.diagnostics as diagnostics
from jobscout.config import SourceConfig
from jobscout.http import FixtureHttpClient
from jobscout.matching import DEFAULT_RULES
from jobscout.models import DeliveryOutcome, DeliveryResult
from jobscout.scanner import run_scan
from jobscout.sources import get_adapter
from jobscout.storage import Storage

SOURCE = SourceConfig(
    id="acme",
    employer="Acme Ltd",
    adapter="lever",
    options={"site": "acme"},
    access_note="test fixture",
)

CONFIG = """
[jobscout]
database = "{database}"

[[sources]]
id = "acme"
employer = "Acme Ltd"
adapter = "lever"
access_note = "test fixture"

[sources.options]
site = "acme"
"""


class FakeTelegram:
    calls: list[tuple[str, str]] = []

    def __init__(self, config, *, poster=None) -> None:
        pass

    def send(self, chat_id: str, text: str) -> DeliveryResult:
        FakeTelegram.calls.append((chat_id, text))
        return DeliveryResult(DeliveryOutcome.SENT, message_id="7")


def telegram_config():
    return type("H", (), {"token": "123456789:" + "A" * 30, "chat_id": "555000111"})()


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    FakeTelegram.calls = []
    monkeypatch.setattr(cli, "REHEARSAL_ROOT", tmp_path / "rehearsal")
    monkeypatch.setattr(cli, "TelegramTransport", FakeTelegram)
    monkeypatch.setattr(cli, "load_telegram_config", lambda *a, **k: telegram_config())
    return tmp_path


def lever_record(role_id: str, title: str, *, location: str = "London, United Kingdom") -> dict:
    return {
        "id": role_id,
        "text": title,
        "categories": {"location": location, "allLocations": [location]},
        "workplaceType": "onsite",
        "descriptionPlain": "Test fixture record.",
        "createdAt": 1759276800000,
        "hostedUrl": f"https://example.test/{role_id}",
    }


def seed(tmp_path: Path, records: list[dict]) -> Path:
    """Populate a real database through the real scanner, then return a config for it."""
    database = tmp_path / "ops.db"
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(records), encoding="utf-8")
    with Storage.open(database) as storage:
        run_scan(
            storage,
            get_adapter("lever"),
            SOURCE,
            FixtureHttpClient(payload),
            rules=DEFAULT_RULES,
            clock=lambda: "2026-10-24T10:00:00Z",
        )
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(database=database.as_posix()), encoding="utf-8")
    return config


# rehearsal ------------------------------------------------------------------


def test_rehearsal_dry_run_explains_every_step(isolated: Path, capsys) -> None:
    exit_code = cli.main(["rehearsal", "--dry-run"])
    out = capsys.readouterr().out

    assert exit_code == cli.EXIT_OK
    assert "silent baseline confirmed" in out
    assert "new role detected and one alert queued" in out
    assert "dry run: nothing was sent" in out
    assert "New role: Graduate Data Analyst" in out
    assert "pending=1" in out
    assert "does not prove that a live employer will post a matching role" in out
    assert FakeTelegram.calls == []


def test_rehearsal_sends_one_labelled_synthetic_alert(isolated: Path, capsys) -> None:
    exit_code = cli.main(["rehearsal"])
    out = capsys.readouterr().out

    assert exit_code == cli.EXIT_OK
    assert "sent=1 failed=0" in out
    assert "the alert was delivered" in out
    assert "chat 555000111" in out
    assert len(FakeTelegram.calls) == 1
    chat_id, text = FakeTelegram.calls[0]
    assert chat_id == "555000111"
    assert "Job-Scout Rehearsal (synthetic)" in text


def test_rehearsal_without_credentials_explains_the_choice(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(cli, "_optional_telegram", lambda: None)

    exit_code = cli.main(["rehearsal"])
    err = capsys.readouterr().err

    assert exit_code == cli.EXIT_FAILED
    assert "Telegram is not configured" in err
    assert "--dry-run" in err
    assert FakeTelegram.calls == []


# summary diagnosis ----------------------------------------------------------


def test_summary_explains_why_nothing_matched(isolated: Path, capsys) -> None:
    config = seed(
        isolated,
        [
            lever_record("1", "Account Executive"),
            lever_record("2", "Staff Software Engineer"),
            lever_record("3", "Graduate Software Engineer", location="Paris, France"),
        ],
    )

    exit_code = cli.main(["summary", "--current", "--config", str(config)])
    out = capsys.readouterr().out

    assert exit_code == cli.EXIT_OK
    assert "Nothing currently matches your preferences" in out
    assert "3 excluded role(s)" in out
    assert "the title reads as senior or experienced hire" in out
    assert "the location is outside your scope" in out
    assert "jobscout config show" in out


def test_summary_explains_a_silent_baseline(isolated: Path, capsys) -> None:
    config = seed(
        isolated,
        [
            lever_record("1", "Graduate Software Engineer"),
            lever_record("2", "Account Executive"),
        ],
    )

    exit_code = cli.main(["summary", "--current", "--config", str(config)])
    out = capsys.readouterr().out

    assert exit_code == cli.EXIT_OK
    assert "1 role(s) match, and no alert has ever been sent" in out
    assert "silent baseline working" in out
    assert "jobscout digest --dry-run" in out


def test_summary_says_nothing_extra_once_an_alert_has_been_sent(isolated: Path, capsys) -> None:
    config = seed(isolated, [lever_record("1", "Graduate Software Engineer")])
    with Storage.open(isolated / "ops.db") as storage:
        storage.connection.execute(
            "INSERT INTO notifications (chat_id, job_id, status, created_at)"
            " SELECT '555', id, 'sent', '2026-10-24T10:00:00Z' FROM jobs"
        )

    cli.main(["summary", "--current", "--config", str(config)])
    out = capsys.readouterr().out

    assert "Nothing currently matches" not in out
    assert "no alert has ever been sent" not in out


def test_exclusions_are_counted_by_leading_reason() -> None:
    summaries = diagnostics.summarise_exclusions(
        (
            '["exclude:family:sales", "exclude:seniority:senior"]',
            '["exclude:family:sales"]',
            '["exclude:location:not_pinned"]',
            "not json",
            '["family:software"]',
        )
    )

    assert [(item.label, item.count) for item in summaries] == [
        ("the role family is outside the ones you chose", 2),
        ("the location is outside your scope", 1),
    ]
