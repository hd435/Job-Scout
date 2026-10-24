"""Scheduler: run due sources, deliver alerts, and notify about repeated failures.

Scope: per-source cadence with jitter, failure isolation, and restart recovery. There is
no daemon supervisor, no concurrency, and no deployment concern here; M5.4 owns the
seven-day unattended run.

Restart recovery rules:

- `sources.next_run_at` is the only schedule state, so a restart resumes from persisted
  state rather than memory.
- A source is claimed by advancing `next_run_at` before the scan starts (D-063), so two
  scheduler processes cannot scan the same source in one cycle, and a process killed
  mid-scan is retried at the next cadence instead of immediately.
- A claim that fails is backed off in memory as well as in the database, so a failing write
  cannot make every source look due and spin the loop with zero-second sleeps.
- A missed cycle is not replayed: a source whose `next_run_at` has long passed runs once
  and is rescheduled from "now", so downtime cannot produce a burst of catch-up scans.
- `run_scan` persists a scan and its state transitions in one transaction, so an
  interrupted scan leaves no partial state; the next cycle retries it.
- An exception outside the adapter's own error handling is recorded as a failed scan
  (D-063), so the failure streak, the three-failure notice, and `status` still reflect it.
- Alerts queued before a restart are delivered by the notifier's own idempotent pass,
  which never re-sends a `sent` row.
"""

from __future__ import annotations

import contextlib
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from .config import AppConfig, SourceConfig
from .http import HttpClient
from .matching import DEFAULT_RULES, MatchingRules
from .models import DeliveryReport, ScanOutcome, ScanResult, ScanStatus
from .notices import FailureNoticeSender, NoticeOutcome
from .notifier import Notifier
from .scanner import run_scan
from .sources import get_adapter
from .storage import Storage, utc_now_iso

DEFAULT_MAX_JITTER_SECONDS = 0.0
MAX_SLEEP_SECONDS = 60.0


@dataclass(frozen=True)
class SourceRun:
    """One source's turn in the schedule."""

    source_id: str
    scanned: bool
    status: str | None = None
    outcome: ScanOutcome | None = None
    delivery: DeliveryReport | None = None
    notice: NoticeOutcome | None = None
    next_run_at: str | None = None
    error: str | None = None
    skipped_reason: str | None = None
    post_run_error: str | None = None


class Scheduler:
    """Drives scans and deliveries for the configured sources, one at a time."""

    def __init__(
        self,
        storage: Storage,
        app: AppConfig,
        *,
        http_client: HttpClient,
        notifier: Notifier | None = None,
        notice_sender: FailureNoticeSender | None = None,
        chat_id: str | None = None,
        rules: MatchingRules = DEFAULT_RULES,
        clock: Callable[[], str] = utc_now_iso,
        sleep: Callable[[float], None] = time.sleep,
        jitter_seconds: float = DEFAULT_MAX_JITTER_SECONDS,
        random_source: random.Random | None = None,
        adapter_factory: Callable[[str], object] = get_adapter,
    ) -> None:
        self._storage = storage
        self._app = app
        self._http_client = http_client
        self._notifier = notifier
        self._notice_sender = notice_sender
        self._chat_id = chat_id
        self._rules = rules
        self._clock = clock
        self._sleep = sleep
        self._jitter_seconds = max(0.0, jitter_seconds)
        self._random = random_source or random.Random()
        self._adapter_factory = adapter_factory
        # Sources this process could not claim; memory only, so a failing database cannot
        # turn every source into a zero-second sleep (D-063).
        self._claim_retries: dict[str, datetime] = {}

    # scheduling ------------------------------------------------------------

    def due_sources(self, now: str | None = None) -> tuple[SourceConfig, ...]:
        """Enabled sources that have never run or whose next run is due."""
        moment = now or self._clock()
        moment_at = _parse_iso(moment)
        due: list[SourceConfig] = []
        for source in self._app.enabled_sources():
            retry_at = self._claim_retries.get(source.id)
            if retry_at is not None and retry_at > moment_at:
                continue  # this process is backing off a failed claim
            self._storage.ensure_source(source)
            state = self._storage.get_source_state(source.id)
            if state is None or state.next_run_at is None or state.next_run_at <= moment:
                due.append(source)
        return tuple(due)

    def next_run_at(self, source: SourceConfig, *, now: str | None = None) -> str:
        moment = _parse_iso(now or self._clock())
        delay = timedelta(minutes=source.cadence_minutes)
        if self._jitter_seconds:
            delay += timedelta(seconds=self._random.uniform(0.0, self._jitter_seconds))
        return _format_iso(moment + delay)

    # running ---------------------------------------------------------------

    def run_once(self, *, now: str | None = None) -> tuple[SourceRun, ...]:
        """Run every due source exactly once, then decide notices for the pass as a whole.

        Notices are decided after the pass, not per source, because several sources failing
        for the same connectivity reason are one problem worth one message (D-086).
        """
        moment = now or self._clock()
        runs = tuple(self._run_source(source, moment) for source in self.due_sources(moment))
        return self._notices_after_pass(runs)

    def run_forever(
        self,
        *,
        max_iterations: int | None = None,
        on_run: Callable[[Sequence[SourceRun]], None] | None = None,
    ) -> tuple[SourceRun, ...]:
        """Loop until `max_iterations` (tests, bounded runs) or interruption."""
        collected: list[SourceRun] = []
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            iterations += 1
            runs = self.run_once()
            collected.extend(runs)
            if on_run is not None:
                on_run(runs)
            self._sleep(self._seconds_until_next_due())
        return tuple(collected)

    def _run_source(self, source: SourceConfig, now: str) -> SourceRun:
        state = self._storage.get_source_state(source.id)
        next_run = self.next_run_at(source, now=now)
        try:
            claimed = self._storage.claim_source_run(
                source.id,
                expected_next_run_at=None if state is None else state.next_run_at,
                next_run_at=next_run,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - one source must never stop the schedule
            expected = None if state is None else state.next_run_at
            self._back_off_failed_claim(source, expected=expected, now=now, next_run_at=next_run)
            return SourceRun(
                source_id=source.id,
                scanned=False,
                error=f"claim failed: {type(exc).__name__}: {exc}",
                next_run_at=next_run,
            )
        if not claimed:
            return SourceRun(
                source_id=source.id,
                scanned=False,
                skipped_reason="another scheduler already claimed this run",
            )
        self._claim_retries.pop(source.id, None)

        try:
            adapter = self._adapter_factory(source.adapter)
            outcome = run_scan(
                self._storage,
                adapter,  # type: ignore[arg-type]
                source,
                self._http_client,
                notify_chat_id=self._chat_id,
                rules=self._rules,
                clock=self._clock,
            )
        except Exception as exc:  # noqa: BLE001 - one source must never stop the schedule
            # The source is already rescheduled, so a broken source cannot spin.
            error = f"{type(exc).__name__}: {exc}"
            if not self._record_unexpected_failure(source, now=now, error=error):
                error = f"{error} (failure could not be recorded)"
            delivery, delivery_error = self._deliver()
            return SourceRun(
                source_id=source.id,
                scanned=False,
                error=error,
                delivery=delivery,
                next_run_at=next_run,
                post_run_error=delivery_error,
            )

        delivery, delivery_error = self._deliver()
        return SourceRun(
            source_id=source.id,
            scanned=True,
            status=str(outcome.status),
            outcome=outcome,
            delivery=delivery,
            next_run_at=next_run,
            post_run_error=delivery_error,
        )

    def _back_off_failed_claim(
        self, source: SourceConfig, *, expected: str | None, now: str, next_run_at: str
    ) -> None:
        """Back off after a failed claim without depending on a database write.

        Two failures in a row (the claim and the best-effort deferral) used to leave the
        source due for ever, and `run_forever` then spun with zero-second sleeps. The
        in-memory marker bounds attempts to one per cadence even when nothing can be
        written; it is per process, so another scheduler is unaffected, and the caller still
        reports the original claim error.
        """
        with contextlib.suppress(Exception):  # best effort: the claim error is reported
            self._storage.defer_source_run(
                source.id, expected_next_run_at=expected, next_run_at=next_run_at
            )
        delay = timedelta(minutes=max(1, source.cadence_minutes))
        self._claim_retries[source.id] = _parse_iso(now) + delay

    def _record_unexpected_failure(self, source: SourceConfig, *, now: str, error: str) -> bool:
        """Persist a failure raised outside the adapter's own error handling."""
        result = ScanResult(
            source_id=source.id,
            started_at=now,
            finished_at=now,
            status=ScanStatus.FAILED,
            records_seen=0,
            records_parsed=0,
            error_summary=f"scheduler error: {error}",
        )
        try:
            with self._storage.transaction():
                self._storage.record_scan(result, is_baseline=False)
                self._storage.update_source_after_scan(
                    source.id, result, now=now, is_baseline=False
                )
        except Exception:  # noqa: BLE001 - reporting the failure must not stop the schedule
            return False
        return True

    def _deliver(self) -> tuple[DeliveryReport | None, str | None]:
        if self._notifier is None or not self._chat_id:
            return None, None
        try:
            return self._notifier.deliver_pending(self._storage, self._chat_id), None
        except Exception as exc:  # noqa: BLE001 - delivery must not abort the pass
            return None, f"delivery error: {type(exc).__name__}: {exc}"

    def _notices_after_pass(self, runs: Sequence[SourceRun]) -> tuple[SourceRun, ...]:
        """Attach this pass's notice decisions to the runs they belong to."""
        if self._notice_sender is None or not self._chat_id or not runs:
            return tuple(runs)
        try:
            states = [
                state
                for state in (self._storage.get_source_state(run.source_id) for run in runs)
                if state is not None
            ]
            outcomes = self._notice_sender.notify_pass(self._storage, states, chat_id=self._chat_id)
        except Exception as exc:  # noqa: BLE001 - a notice must not abort the pass
            message = f"notice error: {type(exc).__name__}: {exc}"
            return tuple(
                replace(run, post_run_error=_join_errors(run.post_run_error, message))
                for run in runs
            )
        return tuple(
            replace(run, notice=outcomes[run.source_id]) if run.source_id in outcomes else run
            for run in runs
        )

    def _seconds_until_next_due(self) -> float:
        moment = _parse_iso(self._clock())
        soonest: datetime | None = None
        for source in self._app.enabled_sources():
            retry_at = self._claim_retries.get(source.id)
            if retry_at is not None and retry_at > moment:
                # A failed claim this process could not record still has to pace the loop.
                candidate = retry_at
            else:
                state = self._storage.get_source_state(source.id)
                if state is None or state.next_run_at is None:
                    return 0.0
                candidate = _parse_iso(state.next_run_at)
            if soonest is None or candidate < soonest:
                soonest = candidate
        if soonest is None:
            return MAX_SLEEP_SECONDS
        return max(0.0, min((soonest - moment).total_seconds(), MAX_SLEEP_SECONDS))


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _join_errors(*errors: str | None) -> str | None:
    present = [error for error in errors if error]
    return "; ".join(present) or None


def _format_iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = ["DEFAULT_MAX_JITTER_SECONDS", "MAX_SLEEP_SECONDS", "Scheduler", "SourceRun"]
