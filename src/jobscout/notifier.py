"""Notification pipeline: formatting, delivery state, and duplicate prevention.

M2.1 is offline: there is no Telegram transport yet. The pipeline is driven by a
`Transport` implementation; production uses the real one from M2.2, tests use a
recording fake. Transports classify each attempt and must not raise.

Delivery policy (approved, D-036):

- Ambiguous outcomes (a timeout after the request may have been delivered) are
  recorded as `ambiguous` and are never retried automatically.
- Transient failures (429, 5xx, network) retry a bounded number of times inside
  the pass and stay `pending` afterwards, so the next pass tries again.
- Permanent rejections (other 4xx) become `failed` and are not retried.
- A `sent` row is never sent again, so retries and restarts cannot duplicate.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from typing import Protocol

from .models import (
    DeliveryOutcome,
    DeliveryReport,
    DeliveryResult,
    PendingNotification,
)
from .storage import Storage, utc_now_iso

MAX_SEND_ATTEMPTS = 3
REDACTED = "[redacted]"

# Telegram bot tokens look like "<bot id>:<secret>".
_BOT_TOKEN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")


def redact(text: str | None, *, secret_values: Iterable[str] = ()) -> str | None:
    """Remove bot-token shapes and any known secret values from log/error text."""
    if text is None:
        return None
    cleaned = _BOT_TOKEN.sub(REDACTED, text)
    for secret in secret_values:
        if secret:
            cleaned = cleaned.replace(secret, REDACTED)
    return cleaned


class Transport(Protocol):
    def send(self, chat_id: str, text: str) -> DeliveryResult: ...


def format_message(notification: PendingNotification) -> str:
    """One concise message. Missing optional fields are omitted, never shown empty."""
    lines = [
        f"New role: {notification.title.strip()}",
        notification.employer.strip(),
        notification.location_raw.strip() or "Location not stated",
    ]
    if notification.salary_text:
        lines.append(f"Salary: {notification.salary_text}")
    if notification.closing_at:
        lines.append(f"Closes: {notification.closing_at}")
    lines.extend(["", notification.url])
    return "\n".join(lines)


def format_delivery_check(now: str) -> str:
    """The one message `notify-test` sends. It must be unmistakably not a job alert."""
    return "\n".join(
        [
            "Job-Scout delivery check",
            "The configured bot token and chat id can send a Telegram message.",
            "This is not a job alert: no role was created, matched, queued or baselined.",
            f"Sent: {now}",
        ]
    )


class Notifier:
    """Delivers queued notifications and records the outcome of every attempt."""

    def __init__(
        self,
        transport: Transport,
        *,
        max_attempts: int = MAX_SEND_ATTEMPTS,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], str] = utc_now_iso,
        secret_values: Iterable[str] = (),
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._transport = transport
        self._max_attempts = max_attempts
        self._backoff = backoff_seconds
        self._sleep = sleep
        self._clock = clock
        self._secret_values = tuple(secret_values)

    def deliver_pending(self, storage: Storage, chat_id: str, *, limit: int = 50) -> DeliveryReport:
        sent = failed = ambiguous = retrying = 0
        delivered: list[str] = []

        for notification in storage.list_pending_notifications(chat_id, limit=limit):
            text = format_message(notification)
            outcome = self._attempt(storage, notification, text)
            if outcome is DeliveryOutcome.SENT:
                sent += 1
                delivered.append(text)
            elif outcome is DeliveryOutcome.PERMANENT:
                failed += 1
            elif outcome is DeliveryOutcome.AMBIGUOUS:
                ambiguous += 1
            else:
                retrying += 1

        return DeliveryReport(
            sent=sent,
            failed=failed,
            ambiguous=ambiguous,
            retrying=retrying,
            messages=tuple(delivered),
        )

    def _attempt(
        self, storage: Storage, notification: PendingNotification, text: str
    ) -> DeliveryOutcome:
        outcome = DeliveryOutcome.RETRYABLE
        for attempt in range(1, self._max_attempts + 1):
            result = self._transport.send(notification.chat_id, text)
            now = self._clock()
            error = redact(
                result.error,
                secret_values=(notification.chat_id, *self._secret_values),
            )
            outcome = result.outcome

            if result.outcome is DeliveryOutcome.SENT:
                storage.record_notification_sent(
                    notification.id, message_id=result.message_id, now=now
                )
                return DeliveryOutcome.SENT
            if result.outcome is DeliveryOutcome.AMBIGUOUS:
                storage.record_notification_ambiguous(
                    notification.id,
                    error=error or "delivery outcome unknown",
                    now=now,
                )
                return DeliveryOutcome.AMBIGUOUS
            if result.outcome is DeliveryOutcome.PERMANENT:
                storage.record_notification_failed(
                    notification.id,
                    error=error or "permanent delivery failure",
                    now=now,
                )
                return DeliveryOutcome.PERMANENT

            storage.record_notification_retry(
                notification.id, error=error or "transient delivery failure", now=now
            )
            if attempt < self._max_attempts:
                self._sleep(self._backoff * (2 ** (attempt - 1)))
        return outcome
