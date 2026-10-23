"""Scheduler: run due sources, deliver alerts, and notify about repeated failures (M5.2).

Scope: per-source cadence with jitter, failure isolation, and restart recovery. There is
no daemon supervisor, no concurrency, and no deployment concern here; M5.4 owns the
seven-day unattended run.

Restart recovery rules:

- `sources.next_run_at` is the only schedule state, so a restart resumes from persisted
  state rather than memory.
- A missed cycle is not replayed: a source whose `next_run_at` has long passed runs once
  and is rescheduled from "now", so downtime cannot produce a burst of catch-up scans.
- `run_scan` persists a scan and its state transitions in one transaction, so an
  interrupted scan leaves no partial state; the next cycle retries it.
- Alerts queued before a restart are delivered by the notifier's own idempotent pass,
  which never re-sends a `sent` row.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .config import AppConfig, SourceConfig
from .http import HttpClient
from .matching import DEFAULT_RULES, MatchingRules
from .models import DeliveryReport, ScanOutcome
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

    # scheduling ------------------------------------------------------------

    def due_sources(self, now: str | None = None) -> tuple[SourceConfig, ...]:
        """Enabled sources that have never run or whose next run is due."""
        moment = now or self._clock()
        due: list[SourceConfig] = []
        for source in self._app.enabled_sources():
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
        """Run every due source exactly once, isolating failures per source."""
        moment = now or self._clock()
        return tuple(self._run_source(source, moment) for source in self.due_sources(moment))

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
            # Reschedule even on an unexpected failure, so a broken source cannot spin.
            next_run = self.next_run_at(source, now=now)
            self._storage.schedule_next_run(source.id, next_run_at=next_run)
            return SourceRun(
                source_id=source.id,
                scanned=False,
                error=f"{type(exc).__name__}: {exc}",
                next_run_at=next_run,
            )

        delivery = None
        if self._notifier is not None and self._chat_id:
            delivery = self._notifier.deliver_pending(self._storage, self._chat_id)

        notice = self._maybe_notify(source)

        next_run = self.next_run_at(source, now=now)
        self._storage.schedule_next_run(source.id, next_run_at=next_run)
        return SourceRun(
            source_id=source.id,
            scanned=True,
            status=str(outcome.status),
            outcome=outcome,
            delivery=delivery,
            notice=notice,
            next_run_at=next_run,
        )

    def _maybe_notify(self, source: SourceConfig) -> NoticeOutcome | None:
        if self._notice_sender is None or not self._chat_id:
            return None
        state = self._storage.get_source_state(source.id)
        if state is None:
            return None
        return self._notice_sender.maybe_notify(self._storage, state, chat_id=self._chat_id)

    def _seconds_until_next_due(self) -> float:
        moment = _parse_iso(self._clock())
        soonest: datetime | None = None
        for source in self._app.enabled_sources():
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


def _format_iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = ["DEFAULT_MAX_JITTER_SECONDS", "MAX_SLEEP_SECONDS", "Scheduler", "SourceRun"]
