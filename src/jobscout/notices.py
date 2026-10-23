"""Proactive source-failure notices (M5.2, D-038).

A source that fails three scans in a row is worth telling the user about, but a broken
source can fail every cycle for days. The rules here follow the approved operational
defaults:

- one notice per failure episode, claimed atomically before sending, so a streak cannot
  produce repeated spam (D-010);
- a completed scan clears the episode, so a later streak notifies again;
- one send attempt only. An ambiguous outcome is recorded and never retried, matching the
  delivery policy for alerts (D-024); a notice is not worth risking a duplicate for. If a
  transport raises instead of returning a failed result, the episode is recorded as `failed`
  rather than left `pending`, and it is still not retried.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .models import DeliveryOutcome, DeliveryResult, SourceState
from .notifier import Transport, redact
from .storage import Storage, utc_now_iso

FAILURE_NOTICE_THRESHOLD = 3


@dataclass(frozen=True)
class NoticeOutcome:
    """What happened when the sender considered a source failure notice."""

    source_id: str
    attempted: bool
    outcome: str | None = None
    error: str | None = None

    @property
    def sent(self) -> bool:
        return self.outcome == "sent"


def format_failure_notice(state: SourceState) -> str:
    """A short, actionable message. Missing detail is omitted, never shown empty."""
    lines = [
        f"Job-Scout: {state.employer} has failed {state.consecutive_failures} consecutive scans.",
    ]
    if state.last_error_summary:
        lines.append(f"Last error: {state.last_error_summary}")
    if state.last_error_at:
        lines.append(f"At: {state.last_error_at}")
    lines.extend(["", "Check: jobscout status"])
    return "\n".join(lines)


class FailureNoticeSender:
    """Sends at most one notice per failure episode, through the injected transport."""

    def __init__(
        self,
        transport: Transport,
        *,
        threshold: int = FAILURE_NOTICE_THRESHOLD,
        clock: Callable[[], str] = utc_now_iso,
        secret_values: Iterable[str] = (),
    ) -> None:
        if threshold < 1:
            raise ValueError("threshold must be at least 1")
        self._transport = transport
        self._threshold = threshold
        self._clock = clock
        self._secret_values = tuple(secret_values)

    def maybe_notify(
        self, storage: Storage, state: SourceState, *, chat_id: str | None
    ) -> NoticeOutcome:
        if not chat_id:
            return NoticeOutcome(state.id, attempted=False, outcome="no_chat_id")
        if state.consecutive_failures < self._threshold:
            return NoticeOutcome(state.id, attempted=False)

        now = self._clock()
        if not storage.claim_failure_notice(state.id, now=now, threshold=self._threshold):
            # Already notified for this episode; do not spam.
            return NoticeOutcome(state.id, attempted=False)

        text = format_failure_notice(state)
        try:
            result: DeliveryResult = self._transport.send(chat_id, text)
        except Exception as exc:  # noqa: BLE001 - transports must not raise, but if one does
            # The claim is already made and there is no retry (D-060), so record the
            # episode as failed rather than leaving it pending for ever.
            error = redact(
                f"{type(exc).__name__}: {exc}",
                secret_values=(chat_id, *self._secret_values),
            )
            storage.record_failure_notice_outcome(state.id, outcome="failed")
            return NoticeOutcome(state.id, attempted=True, outcome="failed", error=error)

        outcome = _outcome_name(result.outcome)
        error = redact(result.error, secret_values=(chat_id, *self._secret_values))
        storage.record_failure_notice_outcome(state.id, outcome=outcome)
        return NoticeOutcome(state.id, attempted=True, outcome=outcome, error=error)


def _outcome_name(outcome: DeliveryOutcome) -> str:
    if outcome is DeliveryOutcome.SENT:
        return "sent"
    if outcome is DeliveryOutcome.AMBIGUOUS:
        return "ambiguous"
    return "failed"


__all__ = [
    "FAILURE_NOTICE_THRESHOLD",
    "FailureNoticeSender",
    "NoticeOutcome",
    "format_failure_notice",
]
