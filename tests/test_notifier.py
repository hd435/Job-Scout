from __future__ import annotations

import sqlite3

import pytest

from jobscout.models import DeliveryOutcome, DeliveryResult, PendingNotification
from jobscout.notifier import REDACTED, format_message, redact
from jobscout.scanner import run_scan
from jobscout.sources import get_adapter
from jobscout.storage import Storage

CHAT_ID = "123456789"
TOKEN = "123456789:AAFakeTokenValueForRedactionTests"

SAMPLE = PendingNotification(
    id=1,
    chat_id=CHAT_ID,
    job_id=10,
    attempt_count=0,
    title="Graduate Software Engineer",
    employer="Palantir Technologies",
    location_raw="London, United Kingdom",
    url="https://jobs.lever.co/palantir/abc",
    salary_text="£55,000 per year",
    closing_at="2026-11-30",
)


def queue(scan, record, chat_id: str = CHAT_ID) -> None:
    """Baseline, then one new matching role queued for delivery."""
    scan([record("a", "Graduate Data Engineer")], notify_chat_id=chat_id)
    scan(
        [
            record("a", "Graduate Data Engineer"),
            record("b", "Graduate Software Engineer"),
        ],
        notify_chat_id=chat_id,
    )


def notification_row(store: Storage, job_source_id: str = "b") -> sqlite3.Row:
    job = store.get_job("palantir", job_source_id)
    row = store.notification_for_job(CHAT_ID, job.id)
    assert row is not None
    return row


# formatting ---------------------------------------------------------------


def test_message_contains_every_available_field():
    text = format_message(SAMPLE)

    assert text.splitlines() == [
        "New role: Graduate Software Engineer",
        "Palantir Technologies",
        "London, United Kingdom",
        "Salary: £55,000 per year",
        "Closes: 2026-11-30",
        "",
        "https://jobs.lever.co/palantir/abc",
    ]


def test_missing_optional_fields_are_omitted_not_blank():
    text = format_message(
        PendingNotification(
            id=2,
            chat_id=CHAT_ID,
            job_id=11,
            attempt_count=0,
            title="Software Engineer Intern",
            employer="Palantir Technologies",
            location_raw="London, United Kingdom",
            url="https://jobs.lever.co/palantir/def",
        )
    )

    assert "Salary" not in text
    assert "Closes" not in text
    assert text.count("\n\n") == 1


def test_unknown_location_is_labelled_not_dropped():
    text = format_message(
        PendingNotification(
            id=3,
            chat_id=CHAT_ID,
            job_id=12,
            attempt_count=0,
            title="Data Engineer",
            employer="Palantir Technologies",
            location_raw="   ",
            url="https://jobs.lever.co/palantir/ghi",
        )
    )

    assert "Location not stated" in text


# queuing and dedupe -------------------------------------------------------


def test_baseline_never_queues_notifications(scan, store, record):
    outcome = scan([record("a", "Graduate Data Engineer")], notify_chat_id=CHAT_ID)

    assert outcome.is_baseline is True
    assert outcome.notifications_queued == 0
    assert store.notification_status_counts() == {}


def test_new_matching_role_is_queued_once(scan, store, record):
    scan([record("a", "Graduate Data Engineer")], notify_chat_id=CHAT_ID)

    outcome = scan(
        [
            record("a", "Graduate Data Engineer"),
            record("b", "Graduate Software Engineer"),
            record("c", "Senior Software Engineer"),
            record("d", "Software Engineer"),
        ],
        notify_chat_id=CHAT_ID,
    )

    assert outcome.notifications_queued == 1
    assert store.notification_status_counts() == {"pending": 1}
    assert notification_row(store)["job_id"] == store.get_job("palantir", "b").id


def test_rerun_and_update_do_not_queue_again(scan, store, record):
    queue(scan, record)

    rerun = scan(
        [
            record("a", "Graduate Data Engineer"),
            record("b", "Graduate Software Engineer"),
        ],
        notify_chat_id=CHAT_ID,
    )
    updated = scan(
        [
            record("a", "Graduate Data Engineer"),
            record(
                "b",
                "Graduate Software Engineer",
                salary={"min": 60000, "currency": "GBP"},
            ),
        ],
        notify_chat_id=CHAT_ID,
    )

    assert rerun.notifications_queued == 0
    assert updated.notifications_queued == 0
    assert store.notification_status_counts() == {"pending": 1}


def test_duplicate_queue_attempt_returns_none(store, scan, record):
    queue(scan, record)
    job_id = store.get_job("palantir", "b").id

    assert store.create_pending_notification(CHAT_ID, job_id, now="2026-10-22T11:00:00Z") is None
    assert store.notification_status_counts() == {"pending": 1}


def test_role_first_seen_in_partial_scan_is_queued_only_when_complete(scan, store, record):
    scan([record("a", "Graduate Data Engineer")], notify_chat_id=CHAT_ID)

    partial = scan(
        [
            record("a", "Graduate Data Engineer"),
            record("b", "Graduate Software Engineer"),
            {"id": "bad", "text": "No URL"},
        ],
        notify_chat_id=CHAT_ID,
    )
    assert partial.notifications_queued == 0

    complete = scan(
        [
            record("a", "Graduate Data Engineer"),
            record("b", "Graduate Software Engineer"),
        ],
        notify_chat_id=CHAT_ID,
    )

    assert complete.notifications_queued == 1
    assert store.notification_status_counts() == {"pending": 1}


def test_no_chat_id_means_no_rows(scan, store, record):
    scan([record("a", "Graduate Data Engineer")])
    scan(
        [
            record("a", "Graduate Data Engineer"),
            record("b", "Graduate Software Engineer"),
        ]
    )

    assert store.notification_status_counts() == {}


# delivery outcomes --------------------------------------------------------


def test_success_is_recorded_with_message_id(scan, store, record, fake_transport, make_notifier):
    queue(scan, record)
    transport = fake_transport([DeliveryResult(DeliveryOutcome.SENT, message_id="77")])

    report = make_notifier(transport).deliver_pending(store, CHAT_ID)

    row = notification_row(store)
    assert report.sent == 1
    assert report.attempted == 1
    assert row["status"] == "sent"
    assert row["attempt_count"] == 1
    assert row["telegram_message_id"] == "77"
    assert row["sent_at"] is not None
    assert transport.sent[0][0] == CHAT_ID
    assert "Graduate Software Engineer" in transport.sent[0][1]


def test_delivering_twice_does_not_resend(scan, store, record, fake_transport, make_notifier):
    queue(scan, record)
    transport = fake_transport()
    notifier = make_notifier(transport)

    first = notifier.deliver_pending(store, CHAT_ID)
    second = notifier.deliver_pending(store, CHAT_ID)

    assert first.sent == 1
    assert second.attempted == 0
    assert len(transport.sent) == 1


def test_restart_does_not_resend(scan, store, record, fake_transport, make_notifier, tmp_path):
    queue(scan, record)
    transport = fake_transport()
    make_notifier(transport).deliver_pending(store, CHAT_ID)

    reopened = Storage.open(tmp_path / "jobscout.db")
    try:
        report = make_notifier(transport).deliver_pending(reopened, CHAT_ID)
        counts = reopened.notification_status_counts()
    finally:
        reopened.close()

    assert report.attempted == 0
    assert counts == {"sent": 1}
    assert len(transport.sent) == 1


def test_permanent_failure_is_recorded_and_not_retried(
    scan, store, record, fake_transport, make_notifier
):
    queue(scan, record)
    transport = fake_transport(
        [DeliveryResult(DeliveryOutcome.PERMANENT, error="400 Bad Request: chat not found")]
    )
    notifier = make_notifier(transport)

    report = notifier.deliver_pending(store, CHAT_ID)
    second = notifier.deliver_pending(store, CHAT_ID)

    row = notification_row(store)
    assert report.failed == 1
    assert row["status"] == "failed"
    assert row["attempt_count"] == 1
    assert "chat not found" in row["last_error"]
    assert second.attempted == 0
    assert len(transport.sent) == 1


def test_transient_failure_retries_then_stays_pending(
    scan, store, record, fake_transport, make_notifier
):
    queue(scan, record)
    transport = fake_transport(
        [DeliveryResult(DeliveryOutcome.RETRYABLE, error="429 Too Many Requests")] * 3
    )
    notifier = make_notifier(transport, max_attempts=3)

    report = notifier.deliver_pending(store, CHAT_ID)
    row = notification_row(store)

    assert report.retrying == 1
    assert report.sent == 0
    assert row["status"] == "pending"
    assert row["attempt_count"] == 3
    assert "429" in row["last_error"]

    recovered = make_notifier(fake_transport()).deliver_pending(store, CHAT_ID)
    assert recovered.sent == 1
    assert notification_row(store)["status"] == "sent"


def test_transient_failure_then_success_uses_the_same_row(
    scan, store, record, fake_transport, make_notifier
):
    queue(scan, record)
    transport = fake_transport(
        [
            DeliveryResult(DeliveryOutcome.RETRYABLE, error="503 Service Unavailable"),
            DeliveryResult(DeliveryOutcome.SENT, message_id="88"),
        ]
    )

    report = make_notifier(transport).deliver_pending(store, CHAT_ID)

    row = notification_row(store)
    assert report.sent == 1
    assert row["status"] == "sent"
    assert row["attempt_count"] == 2
    assert row["last_error"] is None
    assert len(transport.sent) == 2


def test_ambiguous_outcome_is_recorded_and_never_retried(
    scan, store, record, fake_transport, make_notifier
):
    queue(scan, record)
    transport = fake_transport(
        [DeliveryResult(DeliveryOutcome.AMBIGUOUS, error="timed out after send")]
    )
    notifier = make_notifier(transport)

    report = notifier.deliver_pending(store, CHAT_ID)
    second = notifier.deliver_pending(store, CHAT_ID)

    row = notification_row(store)
    assert report.ambiguous == 1
    assert row["status"] == "ambiguous"
    assert row["attempt_count"] == 1
    assert "timed out" in row["last_error"]
    assert second.attempted == 0
    assert len(transport.sent) == 1


def test_only_pending_rows_are_delivered(scan, store, record, fake_transport, make_notifier):
    queue(scan, record)
    job_id = store.get_job("palantir", "b").id
    notification_id = store.notification_for_job(CHAT_ID, job_id)["id"]
    store.record_notification_ambiguous(
        notification_id, error="unknown", now="2026-10-22T12:00:00Z"
    )

    report = make_notifier(fake_transport()).deliver_pending(store, CHAT_ID)

    assert report.attempted == 0


# secrets ------------------------------------------------------------------


def test_delivery_errors_are_redacted_before_storage(
    scan, store, record, fake_transport, make_notifier
):
    queue(scan, record)
    transport = fake_transport(
        [
            DeliveryResult(
                DeliveryOutcome.PERMANENT,
                error=f"403 Forbidden for chat {CHAT_ID} using token {TOKEN}",
            )
        ]
    )

    make_notifier(transport).deliver_pending(store, CHAT_ID)
    stored = notification_row(store)["last_error"]

    assert CHAT_ID not in stored
    assert TOKEN not in stored
    assert REDACTED in stored


def test_redact_handles_tokens_without_known_secrets():
    assert TOKEN not in redact(f"failed with {TOKEN}")


def test_notifier_requires_a_sane_attempt_limit(fake_transport):
    from jobscout.notifier import Notifier

    with pytest.raises(ValueError):
        Notifier(fake_transport(), max_attempts=0)


def test_delivery_report_message_matches_formatted_row(
    scan, store, record, fake_transport, make_notifier
):
    queue(scan, record)

    report = make_notifier(fake_transport()).deliver_pending(store, CHAT_ID)

    assert len(report.messages) == 1
    assert report.messages[0].startswith("New role: Graduate Software Engineer")
    assert report.messages[0].endswith("https://jobs.lever.co/palantir/b")


def test_scan_constructs_runnable_state(tmp_path, palantir, clock, fake_http, record):
    """A fresh database can go from baseline to queued notification in one session."""
    db_path = tmp_path / "fresh.db"
    with Storage.open(db_path) as storage:
        run_scan(
            storage,
            get_adapter("lever"),
            palantir,
            fake_http([record("a", "Graduate Data Engineer")]),
            notify_chat_id=CHAT_ID,
            clock=clock,
        )
        outcome = run_scan(
            storage,
            get_adapter("lever"),
            palantir,
            fake_http(
                [
                    record("a", "Graduate Data Engineer"),
                    record("b", "Graduate Software Engineer"),
                ]
            ),
            notify_chat_id=CHAT_ID,
            clock=clock,
        )

        assert outcome.notifications_queued == 1
        assert storage.notification_status_counts() == {"pending": 1}
