"""Digest tests: formatting, chunking, read-only behaviour, and delivery outcomes."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

import jobscout.cli as cli
import jobscout.digest as digest
from jobscout.config import SourceConfig
from jobscout.models import (
    DeliveryOutcome,
    DeliveryResult,
    MatchDecision,
    MatchOutcome,
    ScanResult,
    ScanStatus,
)
from jobscout.sources.base import build_job
from jobscout.storage import Storage

NOW = "2026-10-23T19:00:00Z"
LLOYDS = SourceConfig(id="lloyds_graduate", employer="Lloyds Banking Group", adapter="workday")
MONZO = SourceConfig(id="monzo", employer="Monzo", adapter="greenhouse")


def seed_source(storage: Storage, source: SourceConfig, roles: list[tuple[str, str, str]]) -> None:
    result = ScanResult(
        source_id=source.id,
        started_at=NOW,
        finished_at=NOW,
        status=ScanStatus.SUCCESS,
        records_seen=len(roles),
        records_parsed=len(roles),
    )
    scan_id = storage.record_scan(result, is_baseline=True)
    storage.update_source_after_scan(source.id, result, now=NOW, is_baseline=True)
    for job_id, title, location in roles:
        job = build_job(
            source_id=source.id,
            source_job_id=job_id,
            employer=source.employer,
            title=title,
            location_raw=location,
            url=f"https://example.test/{job_id}",
            posted_at=date(2026, 10, 1),
            uk_employer=True,
        )
        storage.insert_job(
            job,
            scan_id=scan_id,
            decision=MatchDecision(MatchOutcome.MATCH, ("family:software",), "m1.1"),
            first_complete_scan_id=scan_id,
            now=NOW,
        )


@pytest.fixture
def populated(store: Storage) -> Storage:
    store.ensure_source(LLOYDS)
    store.ensure_source(MONZO)
    seed_source(
        store,
        LLOYDS,
        [
            ("JR1", "Software Engineer Graduate Scheme (London)", "London"),
            ("JR2", "Data Science & AI Graduate Scheme (Leeds)", "Leeds"),
        ],
    )
    seed_source(store, MONZO, [("4102001", "Associate Software Engineer - Intern", "London")])
    return store


def db_path(storage: Storage) -> Path:
    row = list(storage.connection.execute("PRAGMA database_list"))[0]
    return Path(row["file"])


def counts(storage: Storage) -> dict[str, int]:
    return {
        table: list(storage.connection.execute(f"SELECT COUNT(*) FROM {table}"))[0][0]
        for table in ("jobs", "notifications", "scans", "sources")
    }


def test_digest_lists_each_matching_role_with_its_official_link(populated: Storage) -> None:
    entries = [
        (
            source.id,
            source.employer,
            populated.list_jobs_by_decision(source.id, MatchOutcome.MATCH),
        )
        for source in (LLOYDS, MONZO)
    ]

    messages = digest.build_digest(entries, generated_at=NOW)

    assert [message.source_id for message in messages] == ["lloyds_graduate", "monzo"]
    lloyds = messages[0].text
    assert "2 matching role(s) at Lloyds Banking Group" in lloyds
    assert "these are current, not new" in lloyds
    assert "1. " in lloyds and "2. " in lloyds
    assert (
        "Software Engineer Graduate Scheme (London)\n   London\n   https://example.test/JR1"
        in lloyds
    )
    assert "https://example.test/JR2" in lloyds
    assert messages[0].role_count == 2
    assert "1 matching role(s) at Monzo" in messages[1].text


def test_a_source_without_matches_produces_no_message(populated: Storage) -> None:
    messages = digest.build_digest(
        [
            ("arm", "Arm", []),
            ("monzo", "Monzo", populated.list_jobs_by_decision("monzo", MatchOutcome.MATCH)),
        ],
        generated_at=NOW,
    )

    assert [message.source_id for message in messages] == ["monzo"]


def test_long_lists_are_split_under_the_telegram_limit(store: Storage) -> None:
    source = SourceConfig(id="monzo", employer="Monzo", adapter="greenhouse")
    store.ensure_source(source)
    roles = [(f"R{index}", f"Graduate Engineer {index}", "London") for index in range(120)]
    seed_source(store, source, roles)
    jobs = store.list_jobs_by_decision("monzo", MatchOutcome.MATCH)

    messages = digest.build_digest([("monzo", "Monzo", jobs)], generated_at=NOW)

    assert len(messages) > 1
    assert all(len(message.text) <= digest.MESSAGE_CHAR_LIMIT for message in messages)
    assert "continued" in messages[1].text
    # Every role appears exactly once across the parts.
    combined = "\n".join(message.text for message in messages)
    for index in range(120):
        assert f"Graduate Engineer {index}\n" in combined


def test_the_digest_never_writes_or_queues(populated: Storage, capsys) -> None:
    before = counts(populated)

    exit_code = cli.main(["digest", "--dry-run", "--db", str(db_path(populated))])
    out = capsys.readouterr().out

    assert exit_code == cli.EXIT_OK
    assert "Digest: 2 message(s) covering 3 role(s)" in out
    assert "Dry run: nothing was sent and nothing was written." in out
    assert counts(populated) == before, "the digest must not touch jobs, notifications or scans"


def test_delivery_sends_one_message_per_source(populated: Storage, monkeypatch, capsys) -> None:
    sent: list[tuple[str, str]] = []

    class RecordingTransport:
        def __init__(self, config, *, poster=None) -> None:
            pass

        def send(self, chat_id: str, text: str) -> DeliveryResult:
            sent.append((chat_id, text))
            return DeliveryResult(DeliveryOutcome.SENT, message_id=str(len(sent)))

    monkeypatch.setattr(cli, "TelegramTransport", RecordingTransport)
    monkeypatch.setattr(
        cli,
        "load_telegram_config",
        lambda *a, **k: type("T", (), {"token": "1" * 8 + ":secret", "chat_id": "555"})(),
    )

    exit_code = cli.main(["digest", "--db", str(db_path(populated))])

    assert exit_code == cli.EXIT_OK
    assert len(sent) == 2
    assert "Lloyds Banking Group" in sent[0][1]
    assert "Monzo" in sent[1][1]
    assert "does not queue alerts" in capsys.readouterr().out


def test_a_failed_send_is_reported_as_degraded(populated: Storage, monkeypatch, capsys) -> None:
    class FailingTransport:
        def __init__(self, config, *, poster=None) -> None:
            pass

        def send(self, chat_id: str, text: str) -> DeliveryResult:
            return DeliveryResult(DeliveryOutcome.PERMANENT, error="chat not found")

    monkeypatch.setattr(cli, "TelegramTransport", FailingTransport)
    monkeypatch.setattr(
        cli,
        "load_telegram_config",
        lambda *a, **k: type("T", (), {"token": "1" * 8 + ":secret", "chat_id": "555"})(),
    )

    exit_code = cli.main(["digest", "--db", str(db_path(populated))])

    assert exit_code == cli.EXIT_DEGRADED
    assert "chat not found" in capsys.readouterr().err


def test_digest_refuses_a_missing_database(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing.db"

    exit_code = cli.main(["digest", "--db", str(missing)])

    assert exit_code == cli.EXIT_FAILED
    assert "database not found" in capsys.readouterr().err
    assert not missing.exists()


def test_digest_can_be_limited_to_one_source(populated: Storage, capsys) -> None:
    exit_code = cli.main(
        ["digest", "--dry-run", "--source", "monzo", "--db", str(db_path(populated))]
    )
    out = capsys.readouterr().out

    assert exit_code == cli.EXIT_OK
    assert "Digest: 1 message(s) covering 1 role(s)" in out
    assert "Lloyds" not in out
