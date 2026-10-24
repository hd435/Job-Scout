"""The rehearsal proves the whole alert journey without touching the live database (D-089).

Every other check covers one link. These tests defend the properties that make the rehearsal
trustworthy: the baseline is silent, a new role is detected, the notification is really
recorded, the scratch database is throwaway, and a broken journey keeps its evidence instead of
hiding it.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from jobscout.config import AppConfig
from jobscout.matching import DEFAULT_LOCATION_PREFERENCES, DEFAULT_RULES
from jobscout.models import DeliveryOutcome, DeliveryResult
from jobscout.notifier import Notifier
from jobscout.rehearsal import (
    REHEARSAL_EMPLOYER,
    RehearsalResult,
    baseline_payload,
    follow_up_payload,
    run_rehearsal,
)


class Transport:
    def __init__(self, result: DeliveryResult | None = None) -> None:
        self.result = result or DeliveryResult(DeliveryOutcome.SENT, message_id="99")
        self.sent: list[tuple[str, str]] = []

    def send(self, chat_id: str, text: str) -> DeliveryResult:
        self.sent.append((chat_id, text))
        return self.result


def app() -> AppConfig:
    return AppConfig(database_path=Path("data/jobscout.db"), sources=())


def rehearsed(tmp_path: Path, **kwargs) -> RehearsalResult:
    return run_rehearsal(app(), scratch_dir=tmp_path / "rehearsal", **kwargs)


def test_the_payloads_add_exactly_one_matching_role() -> None:
    baseline = {record["id"] for record in baseline_payload()}
    follow_up = {record["id"] for record in follow_up_payload()}

    assert len(follow_up - baseline) == 1, "the second scan must add one new role, no more"
    added = (follow_up - baseline).pop()
    titles = {record["id"]: record["text"] for record in follow_up_payload()}
    assert titles[added] == "Graduate Data Analyst"


def test_a_dry_run_runs_every_step_but_sends_nothing(tmp_path: Path) -> None:
    result = rehearsed(tmp_path)

    assert result.baseline_was_silent, "the first complete scan must not alert"
    assert result.baseline.records_seen == 2
    assert result.detected_new_role
    assert result.delivery is None, "a dry run makes no delivery attempt"
    assert result.notification_counts == {"pending": 1}
    assert result.complete
    assert len(result.preview) == 1
    assert REHEARSAL_EMPLOYER in result.preview[0]
    assert not (tmp_path / "rehearsal").exists(), "a complete rehearsal cleans up after itself"


def test_a_real_run_delivers_and_records_the_notification(tmp_path: Path) -> None:
    transport = Transport()

    result = rehearsed(
        tmp_path,
        chat_id="555000111",
        notifier=Notifier(transport),
    )

    assert result.delivered, "the transport was called and reported success"
    assert result.recorded, "the notification row is marked sent"
    assert result.sent_message_id == "99"
    assert result.complete
    assert len(transport.sent) == 1
    chat_id, text = transport.sent[0]
    assert chat_id == "555000111", "a real rehearsal delivers to the operator's own chat"
    assert "New role: Graduate Data Analyst" in text
    assert REHEARSAL_EMPLOYER in text
    assert "staff software engineer" not in text.casefold(), "the senior role must not alert"


def test_a_failed_delivery_keeps_the_evidence_and_reports_why(tmp_path: Path) -> None:
    transport = Transport(DeliveryResult(DeliveryOutcome.PERMANENT, error="chat not found"))

    result = rehearsed(tmp_path, chat_id="555", notifier=Notifier(transport))

    assert not result.complete
    assert result.notification_counts.get("failed") == 1
    assert result.delivery_error == "chat not found"
    assert result.scratch_kept, "a failed rehearsal keeps its database for diagnosis"
    assert (tmp_path / "rehearsal" / "rehearsal.db").exists()
    assert (tmp_path / "rehearsal" / "payloads" / "follow-up.json").exists()


def test_keep_preserves_the_scratch_database_on_success(tmp_path: Path) -> None:
    result = rehearsed(tmp_path, keep=True)

    assert result.scratch_kept
    assert (tmp_path / "rehearsal" / "rehearsal.db").exists()


def test_a_scope_that_excludes_the_synthetic_roles_says_so(tmp_path: Path) -> None:
    """An Edinburgh-only scope excludes a London role: not a defect, and not a silent pass."""
    rules = replace(
        DEFAULT_RULES,
        locations=replace(
            DEFAULT_LOCATION_PREFERENCES, mode="pinned", cities=("Edinburgh",), remote_uk=False
        ),
        version="rehearsal-narrow-scope",
    )

    result = rehearsed(tmp_path, rules=rules)

    assert result.baseline_was_silent
    assert not result.detected_new_role, "nothing qualified, so nothing was queued"
    assert not result.complete
    assert result.scratch_kept
    decisions = {title: decision for title, decision, _reasons in result.decisions}
    assert decisions["Graduate Data Analyst"] == "no_match"
    reasons = next(r for title, _d, r in result.decisions if title == "Graduate Data Analyst")
    assert "exclude:location" in reasons


def test_each_synthetic_role_gets_a_decision(tmp_path: Path) -> None:
    result = rehearsed(tmp_path)

    decisions = {title: decision for title, decision, _reasons in result.decisions}
    assert decisions == {
        "Graduate Software Engineer": "match",
        "Graduate Data Analyst": "match",
        "Staff Software Engineer": "no_match",
    }


def test_the_rehearsal_never_touches_the_database_it_is_given(tmp_path: Path) -> None:
    """The AppConfig's database path is configuration, not a target."""
    from jobscout.config import SourceConfig
    from jobscout.storage import Storage

    live = tmp_path / "live.db"
    with Storage.open(live) as storage:
        storage.ensure_source(
            SourceConfig(
                id="keep",
                employer="Keep",
                adapter="lever",
                options={"site": "keep"},
                access_note="test fixture",
            )
        )
    before = live.read_bytes()

    run_rehearsal(
        AppConfig(database_path=live, sources=()),
        scratch_dir=tmp_path / "rehearsal",
    )

    assert live.read_bytes() == before, "the live database must be byte-for-byte unchanged"
