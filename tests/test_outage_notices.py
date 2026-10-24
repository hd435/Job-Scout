"""Shared connectivity outages are one problem, not one problem per source (D-086).

These tests drive `FailureNoticeSender` against a real database, because the rules depend on
persisted episode state: a notice is claimed per source, a completed scan clears the episode,
and a sent outage notice opens a quiet window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jobscout.config import SourceConfig
from jobscout.models import DeliveryOutcome, DeliveryResult, SourceState
from jobscout.notices import FailureNoticeSender, shared_transport_cause
from jobscout.storage import Storage

DNS = "connection failed: [Errno 11001] getaddrinfo failed retryable"
SLOW = "request timed out retryable outcome-unknown"


class Clock:
    def __init__(self, moment: str = "2026-10-24T23:00:00Z") -> None:
        self._moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))

    def __call__(self) -> str:
        return self._moment.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def advance(self, minutes: int) -> None:
        self._moment += timedelta(minutes=minutes)


class Transport:
    def __init__(self, results: list[DeliveryResult] | None = None) -> None:
        self.results = list(results or [])
        self.sent: list[str] = []

    def send(self, chat_id: str, text: str) -> DeliveryResult:
        self.sent.append(text)
        index = len(self.sent) - 1
        if index < len(self.results):
            return self.results[index]
        return DeliveryResult(DeliveryOutcome.SENT, message_id=str(index + 1))


class ExplodingTransport:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, chat_id: str, text: str) -> DeliveryResult:
        self.sent.append(text)
        raise RuntimeError("socket closed")


def source(source_id: str, *, employer: str | None = None) -> SourceConfig:
    return SourceConfig(
        id=source_id,
        employer=employer or source_id.title(),
        adapter="greenhouse",
        options={"board": source_id},
    )


def failing(storage: Storage, config: SourceConfig, error: str, strikes: int = 3) -> None:
    """Put one source into a failure streak exactly as the scanner would leave it."""
    storage.ensure_source(config)
    storage.connection.execute(
        """
        UPDATE sources
        SET consecutive_failures = ?, last_error_summary = ?, last_error_at = ?
        WHERE id = ?
        """,
        (strikes, error, "2026-10-24T22:45:00Z", config.id),
    )


def states(storage: Storage, *configs: SourceConfig) -> list[SourceState]:
    return [state for config in configs if (state := storage.get_source_state(config.id))]


@pytest.fixture
def store(tmp_path: Path) -> Storage:
    with Storage.open(tmp_path / "ops.db") as storage:
        yield storage


def test_shared_cause_is_only_a_connection_that_never_opened() -> None:
    assert shared_transport_cause(DNS) is True
    assert shared_transport_cause("connection failed: [Errno 111] Connection refused") is True
    # A timeout may belong to one slow board, so it stays a per-source failure.
    assert shared_transport_cause(SLOW) is False
    assert shared_transport_cause("HTTP 503") is False
    assert shared_transport_cause(None) is False


def test_two_connectivity_failures_become_one_message(store: Storage) -> None:
    monzo, wise = source("monzo"), source("wise")
    failing(store, monzo, DNS)
    failing(store, wise, DNS)
    transport = Transport()
    sender = FailureNoticeSender(transport, clock=Clock())

    outcomes = sender.notify_pass(store, states(store, monzo, wise), chat_id="555")

    assert len(transport.sent) == 1, "one message for one outage"
    assert "2 sources" in transport.sent[0]
    assert "Monzo" in transport.sent[0] and "Wise" in transport.sent[0]
    assert "not notified again for 120 minutes" in transport.sent[0]
    assert outcomes["monzo"].sent and outcomes["monzo"].shared
    assert outcomes["wise"].sent and outcomes["wise"].shared
    assert store.last_outage_notice_at(chat_id="555") is not None


def test_a_source_joining_the_same_outage_later_is_silent(store: Storage) -> None:
    monzo, wise, arm = source("monzo"), source("wise"), source("arm")
    failing(store, monzo, DNS)
    failing(store, wise, DNS)
    clock = Clock()
    transport = Transport()
    sender = FailureNoticeSender(transport, clock=clock)
    sender.notify_pass(store, states(store, monzo, wise), chat_id="555")

    clock.advance(30)
    failing(store, arm, DNS)
    outcomes = sender.notify_pass(store, states(store, arm), chat_id="555")

    assert len(transport.sent) == 1, "the outage was already announced"
    assert outcomes["arm"].outcome == "covered_by_outage_notice"
    assert outcomes["arm"].attempted is False
    # It is still recorded as claimed, so the episode cannot notify twice later.
    state = store.get_source_state("arm")
    assert state is not None and state.failure_notice_outcome == "pending"


def test_an_ongoing_episode_never_repeats_and_a_new_one_is_announced(store: Storage) -> None:
    monzo, wise = source("monzo"), source("wise")
    failing(store, monzo, DNS)
    failing(store, wise, DNS)
    clock = Clock()
    transport = Transport()
    sender = FailureNoticeSender(transport, clock=clock)
    sender.notify_pass(store, states(store, monzo, wise), chat_id="555")

    clock.advance(121)
    failing(store, monzo, DNS, strikes=9)
    failing(store, wise, DNS, strikes=9)
    outcomes = sender.notify_pass(store, states(store, monzo, wise), chat_id="555")

    # The episode was already claimed for these two sources, so no repeat is possible: the
    # window only governs sources that cross the threshold *while* an outage is open.
    assert outcomes["monzo"].attempted is False
    assert len(transport.sent) == 1

    clock.advance(1)
    arm, tide = source("arm"), source("tide")
    failing(store, arm, DNS)
    failing(store, tide, DNS)
    sender.notify_pass(store, states(store, arm, tide), chat_id="555")
    assert len(transport.sent) == 2, "a new outage episode is announced again"


def test_a_lone_connectivity_failure_keeps_its_own_notice(store: Storage) -> None:
    monzo = source("monzo")
    failing(store, monzo, DNS)
    transport = Transport()

    outcomes = FailureNoticeSender(transport, clock=Clock()).notify_pass(
        store, states(store, monzo), chat_id="555"
    )

    assert len(transport.sent) == 1
    assert "Monzo has failed 3 consecutive scans" in transport.sent[0]
    assert outcomes["monzo"].shared is False
    assert store.last_outage_notice_at(chat_id="555") is None


def test_a_source_specific_failure_is_never_grouped(store: Storage) -> None:
    monzo, wise, arm = source("monzo"), source("wise"), source("arm")
    failing(store, monzo, DNS)
    failing(store, wise, DNS)
    failing(store, arm, SLOW)
    transport = Transport()

    outcomes = FailureNoticeSender(transport, clock=Clock()).notify_pass(
        store, states(store, monzo, wise, arm), chat_id="555"
    )

    assert len(transport.sent) == 2, "the outage plus the one source that genuinely stalled"
    assert "2 sources" in transport.sent[0]
    assert "Arm has failed 3 consecutive scans" in transport.sent[1]
    assert outcomes["arm"].shared is False
    assert outcomes["arm"].sent is True


def test_a_failed_grouped_send_is_recorded_per_source(store: Storage) -> None:
    monzo, wise = source("monzo"), source("wise")
    failing(store, monzo, DNS)
    failing(store, wise, DNS)
    transport = Transport([DeliveryResult(DeliveryOutcome.PERMANENT, error="chat not found")])

    outcomes = FailureNoticeSender(transport, clock=Clock()).notify_pass(
        store, states(store, monzo, wise), chat_id="555"
    )

    assert outcomes["monzo"].outcome == "failed"
    assert outcomes["wise"].outcome == "failed"
    assert store.last_outage_notice_at(chat_id="555") is None, "a failed notice is not a notice"
    assert store.get_source_state("monzo").failure_notice_outcome == "failed"


def test_a_raising_transport_is_recorded_rather_than_left_pending(store: Storage) -> None:
    monzo, wise = source("monzo"), source("wise")
    failing(store, monzo, DNS)
    failing(store, wise, DNS)
    transport = ExplodingTransport()

    outcomes = FailureNoticeSender(transport, clock=Clock()).notify_pass(
        store, states(store, monzo, wise), chat_id="555"
    )

    assert outcomes["monzo"].outcome == "failed"
    assert "socket closed" in (outcomes["monzo"].error or "")
    assert store.get_source_state("wise").failure_notice_outcome == "failed"


def test_a_source_below_the_threshold_is_never_announced(store: Storage) -> None:
    monzo, wise = source("monzo"), source("wise")
    failing(store, monzo, DNS, strikes=2)
    failing(store, wise, DNS, strikes=1)
    transport = Transport()

    outcomes = FailureNoticeSender(transport, clock=Clock()).notify_pass(
        store, states(store, monzo, wise), chat_id="555"
    )

    assert transport.sent == []
    assert outcomes == {}, "nothing to decide below the threshold"


def test_no_chat_id_means_no_send(store: Storage) -> None:
    monzo = source("monzo")
    failing(store, monzo, DNS)
    transport = Transport()

    outcomes = FailureNoticeSender(transport, clock=Clock()).notify_pass(
        store, states(store, monzo), chat_id=None
    )

    assert transport.sent == []
    assert outcomes["monzo"].outcome == "no_chat_id"
