from __future__ import annotations

from typing import Any

import pytest

from jobscout.config import TelegramConfig
from jobscout.http import HttpError
from jobscout.models import DeliveryOutcome
from jobscout.notifier import Notifier
from jobscout.telegram import TelegramTransport

TOKEN = "123456789:AAFakeTokenValueForRedactionTests"
CHAT_ID = "555000111"
API_BASE = "https://api.telegram.org"


class FakePoster:
    """Stands in for UrllibHttpClient.post_json; no network access."""

    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post_json(self, url: str, payload: dict[str, Any]) -> Any:
        self.calls.append((url, payload))
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture
def config() -> TelegramConfig:
    return TelegramConfig(token=TOKEN, chat_id=CHAT_ID)


def make_transport(config: TelegramConfig, poster: FakePoster) -> TelegramTransport:
    return TelegramTransport(config, poster=poster)


def test_success_returns_the_message_id(config) -> None:
    poster = FakePoster({"ok": True, "result": {"message_id": 42}})

    result = make_transport(config, poster).send(CHAT_ID, "hello")

    assert result.outcome is DeliveryOutcome.SENT
    assert result.message_id == "42"
    assert poster.calls == [
        (f"{API_BASE}/bot{TOKEN}/sendMessage", {"chat_id": CHAT_ID, "text": "hello"})
    ]


def test_rejected_message_is_permanent(config) -> None:
    poster = FakePoster({"ok": False, "description": "Bad Request: chat not found"})

    result = make_transport(config, poster).send(CHAT_ID, "hello")

    assert result.outcome is DeliveryOutcome.PERMANENT
    assert "chat not found" in (result.error or "")


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_rate_limit_and_server_errors_are_retryable(config, status) -> None:
    poster = FakePoster(error=HttpError("request failed", status=status, retryable=True))

    result = make_transport(config, poster).send(CHAT_ID, "hello")

    assert result.outcome is DeliveryOutcome.RETRYABLE
    assert result.error == "request failed"


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_errors_are_permanent(config, status) -> None:
    poster = FakePoster(error=HttpError("request rejected", status=status))

    result = make_transport(config, poster).send(CHAT_ID, "hello")

    assert result.outcome is DeliveryOutcome.PERMANENT


def test_timeout_after_send_is_ambiguous(config) -> None:
    poster = FakePoster(error=HttpError("request timed out", retryable=True, uncertain=True))

    result = make_transport(config, poster).send(CHAT_ID, "hello")

    assert result.outcome is DeliveryOutcome.AMBIGUOUS
    assert "unknown" in (result.error or "")


def test_connection_refusal_is_retryable(config) -> None:
    poster = FakePoster(error=HttpError("connection failed: refused", retryable=True))

    result = make_transport(config, poster).send(CHAT_ID, "hello")

    assert result.outcome is DeliveryOutcome.RETRYABLE
    assert "unknown" not in (result.error or "")


def test_unexpected_transport_exception_is_ambiguous(config) -> None:
    poster = FakePoster(error=RuntimeError("boom"))

    result = make_transport(config, poster).send(CHAT_ID, "hello")

    assert result.outcome is DeliveryOutcome.AMBIGUOUS
    assert "RuntimeError" in (result.error or "")


def test_errors_never_echo_the_token_or_chat_id(config) -> None:
    leaky = HttpError(
        f"request failed for {API_BASE}/bot{TOKEN}/sendMessage chat {CHAT_ID}", status=400
    )
    poster = FakePoster(error=leaky)

    result = make_transport(config, poster).send(CHAT_ID, "hello")

    assert TOKEN not in (result.error or "")
    assert CHAT_ID not in (result.error or "")
    assert "[redacted]" in (result.error or "")


def test_api_base_trailing_slash_is_tolerated(config) -> None:
    poster = FakePoster({"ok": True, "result": {"message_id": 1}})

    make_transport(config, poster).send(CHAT_ID, "hello")
    poster.calls.clear()
    TelegramTransport(config, poster=poster, api_base=f"{API_BASE}/").send(CHAT_ID, "hello")

    assert poster.calls[0][0] == f"{API_BASE}/bot{TOKEN}/sendMessage"


def queue_one_alert(scan, record) -> None:
    scan([record("a", "Graduate Data Engineer")], notify_chat_id=CHAT_ID)
    scan(
        [
            record("a", "Graduate Data Engineer"),
            record("b", "Graduate Software Engineer"),
        ],
        notify_chat_id=CHAT_ID,
    )


def test_successful_delivery_records_the_message_id(store, scan, record, config) -> None:
    queue_one_alert(scan, record)
    poster = FakePoster({"ok": True, "result": {"message_id": 99}})

    notifier = Notifier(make_transport(config, poster), sleep=lambda _s: None)
    report = notifier.deliver_pending(store, CHAT_ID)

    job = store.get_job("palantir", "b")
    row = store.notification_for_job(CHAT_ID, job.id)
    assert report.sent == 1
    assert row["status"] == "sent"
    assert row["telegram_message_id"] == "99"
    assert "Graduate Software Engineer" in poster.calls[0][1]["text"]
    assert TOKEN not in poster.calls[0][1]["text"]


def test_ambiguous_transport_outcome_is_recorded_and_not_retried(
    store, scan, record, config
) -> None:
    queue_one_alert(scan, record)
    poster = FakePoster(error=HttpError("request timed out", retryable=True, uncertain=True))
    notifier = Notifier(make_transport(config, poster), sleep=lambda _s: None)

    first = notifier.deliver_pending(store, CHAT_ID)
    second = notifier.deliver_pending(store, CHAT_ID)

    assert first.ambiguous == 1
    assert second.attempted == 0
    assert store.notification_status_counts() == {"ambiguous": 1}
    assert len(poster.calls) == 1


def test_permanent_rejection_is_recorded_without_retry(store, scan, record, config) -> None:
    queue_one_alert(scan, record)
    poster = FakePoster({"ok": False, "description": "Forbidden: bot was blocked by the user"})
    notifier = Notifier(make_transport(config, poster), sleep=lambda _s: None)

    first = notifier.deliver_pending(store, CHAT_ID)
    second = notifier.deliver_pending(store, CHAT_ID)

    assert first.failed == 1
    assert second.attempted == 0
    row = store.notification_for_job(CHAT_ID, store.get_job("palantir", "b").id)
    assert row["status"] == "failed"
    assert "blocked" in row["last_error"]
