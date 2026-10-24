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
- several sources failing at once because the *connection* never opened are one problem,
  not several, so they share one notice (D-086). Timeouts, HTTP errors and parser failures
  stay per source: those can be properties of one employer.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import DeliveryOutcome, DeliveryResult, SourceState
from .notifier import Transport, redact
from .storage import Storage, utc_now_iso

FAILURE_NOTICE_THRESHOLD = 3

# Sources failing with a shared connectivity cause are grouped into one notice.
OUTAGE_MIN_SOURCES = 2
# After a connectivity notice reaches the chat, further sources failing the same way are
# recorded but not announced again for this long. It bounds one overnight outage to a
# couple of messages however many sources are configured.
OUTAGE_QUIET_MINUTES = 120

# `HttpError` uses this prefix only when the request provably never reached the server.
_SHARED_CAUSE_PREFIX = "connection failed:"


def shared_transport_cause(error: str | None) -> bool:
    """True when a failure says the connection never opened, not that a source misbehaved."""
    return (error or "").strip().lower().startswith(_SHARED_CAUSE_PREFIX)


@dataclass(frozen=True)
class NoticeOutcome:
    """What happened when the sender considered a source failure notice."""

    source_id: str
    attempted: bool
    outcome: str | None = None
    error: str | None = None
    shared: bool = False

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


def format_outage_notice(states: Sequence[SourceState], *, quiet_minutes: int) -> str:
    """One message for a connectivity outage affecting several sources (D-086)."""
    employers = ", ".join(state.employer for state in states)
    latest = max(states, key=lambda state: state.last_error_at or "")
    lines = [
        f"Job-Scout: {len(states)} sources have failed several consecutive scans and the"
        " connection never opened, which points at this machine's network rather than one"
        " employer.",
        "",
        f"Affected: {employers}",
    ]
    if latest.last_error_summary:
        lines.append(f"Last error: {latest.last_error_summary}")
    if latest.last_error_at:
        lines.append(f"At: {latest.last_error_at}")
    lines.extend(
        [
            "",
            f"Further sources failing for the same reason are recorded but not notified again"
            f" for {quiet_minutes} minutes.",
            "Individual source health: jobscout status",
        ]
    )
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
        outage_min_sources: int = OUTAGE_MIN_SOURCES,
        outage_quiet_minutes: int = OUTAGE_QUIET_MINUTES,
    ) -> None:
        if threshold < 1:
            raise ValueError("threshold must be at least 1")
        self._transport = transport
        self._threshold = threshold
        self._clock = clock
        self._secret_values = tuple(secret_values)
        self._outage_min_sources = max(2, outage_min_sources)
        self._outage_quiet_minutes = outage_quiet_minutes

    @property
    def threshold(self) -> int:
        return self._threshold

    def notify_pass(
        self, storage: Storage, states: Sequence[SourceState], *, chat_id: str | None
    ) -> dict[str, NoticeOutcome]:
        """Decide every notice for one scheduler pass (D-086).

        Isolated failures keep their own message. Sources whose connection never opened are
        grouped: at least two of them in one pass produce a single outage message, and one of
        them is covered by a recent outage message rather than repeating it. Individual
        source health stays in `status` and the report either way.
        """
        outcomes: dict[str, NoticeOutcome] = {}
        if not chat_id:
            return {
                state.id: NoticeOutcome(state.id, attempted=False, outcome="no_chat_id")
                for state in states
            }

        due = [state for state in states if state.consecutive_failures >= self._threshold]
        shared = [state for state in due if shared_transport_cause(state.last_error_summary)]
        isolated = [state for state in due if not shared_transport_cause(state.last_error_summary)]

        if len(shared) >= self._outage_min_sources:
            outcomes.update(self._notify_outage(storage, shared, chat_id=chat_id))
        else:
            for state in shared:
                outcomes[state.id] = self._cover_or_notify(storage, state, chat_id=chat_id)
        for state in isolated:
            outcomes[state.id] = self.maybe_notify(storage, state, chat_id=chat_id)
        return outcomes

    def _cover_or_notify(
        self, storage: Storage, state: SourceState, *, chat_id: str
    ) -> NoticeOutcome:
        """A lone connectivity failure is covered by any recent outage notice."""
        recent = storage.last_outage_notice_at(chat_id=chat_id)
        if recent and not self._outside_quiet_window(recent):
            claimed = storage.claim_failure_notice(
                state.id, now=self._clock(), threshold=self._threshold
            )
            if claimed:
                return NoticeOutcome(
                    state.id, attempted=False, outcome="covered_by_outage_notice", shared=True
                )
            return NoticeOutcome(state.id, attempted=False)
        return self.maybe_notify(storage, state, chat_id=chat_id)

    def _notify_outage(
        self, storage: Storage, states: Sequence[SourceState], *, chat_id: str
    ) -> dict[str, NoticeOutcome]:
        now = self._clock()
        claimed = [
            state
            for state in states
            if storage.claim_failure_notice(state.id, now=now, threshold=self._threshold)
        ]
        outcomes = {state.id: NoticeOutcome(state.id, attempted=False) for state in states}
        if not claimed:
            return outcomes

        text = format_outage_notice(claimed, quiet_minutes=self._outage_quiet_minutes)
        try:
            result: DeliveryResult = self._transport.send(chat_id, text)
        except Exception as exc:  # noqa: BLE001 - a notice must never abort a pass
            error = redact(
                f"{type(exc).__name__}: {exc}", secret_values=(chat_id, *self._secret_values)
            )
            self._record_outage(
                storage, claimed, outcome="failed", error=error, now=now, chat_id=chat_id
            )
            for state in claimed:
                outcomes[state.id] = NoticeOutcome(
                    state.id, attempted=True, outcome="failed", error=error, shared=True
                )
            return outcomes

        outcome = _outcome_name(result.outcome)
        error = redact(result.error, secret_values=(chat_id, *self._secret_values))
        self._record_outage(
            storage, claimed, outcome=outcome, error=error, now=now, chat_id=chat_id
        )
        for state in claimed:
            outcomes[state.id] = NoticeOutcome(
                state.id, attempted=True, outcome=outcome, error=error, shared=True
            )
        return outcomes

    def _record_outage(
        self,
        storage: Storage,
        claimed: Sequence[SourceState],
        *,
        outcome: str,
        error: str | None,
        now: str,
        chat_id: str,
    ) -> None:
        for state in claimed:
            storage.record_failure_notice_outcome(state.id, outcome=outcome)
        storage.record_outage_notice(
            chat_id=chat_id,
            outcome=outcome,
            source_ids=[state.id for state in claimed],
            error=error,
            now=now,
        )

    def _outside_quiet_window(self, since: str) -> bool:
        try:
            then = datetime.fromisoformat(since.replace("Z", "+00:00"))
            now = datetime.fromisoformat(self._clock().replace("Z", "+00:00"))
        except ValueError:
            return True
        return (now - then) > timedelta(minutes=self._outage_quiet_minutes)

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
    "OUTAGE_MIN_SOURCES",
    "OUTAGE_QUIET_MINUTES",
    "FailureNoticeSender",
    "NoticeOutcome",
    "format_failure_notice",
    "format_outage_notice",
    "shared_transport_cause",
]
