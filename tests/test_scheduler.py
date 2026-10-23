"""M5.2 scheduler, failure-notice, and restart-recovery tests. Fully offline."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobscout.config import AppConfig, SourceConfig
from jobscout.matching import DEFAULT_RULES
from jobscout.models import (
    DeliveryOutcome,
    DeliveryResult,
    JobStatus,
    ScanResult,
    ScanStatus,
)
from jobscout.notices import FailureNoticeSender, format_failure_notice
from jobscout.scheduler import Scheduler
from jobscout.sources.base import build_job
from jobscout.storage import Storage

START = "2026-10-23T09:00:00Z"


class StepClock:
    """Deterministic ISO clock that only moves when a test moves it."""

    def __init__(self, start: str = START) -> None:
        self.current = datetime.fromisoformat(start.replace("Z", "+00:00"))

    def __call__(self) -> str:
        return (
            self.current.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )

    def advance(self, *, minutes: float = 0, seconds: float = 0) -> None:
        self.current += timedelta(minutes=minutes, seconds=seconds)


def job(source_id: str, job_id: str = "R1", title: str = "Graduate Software Engineer") -> object:
    return build_job(
        source_id=source_id,
        source_job_id=job_id,
        employer="Test Employer",
        title=title,
        location_raw="London, United Kingdom",
        url=f"https://example.test/{job_id}",
        description="A role description.",
    )


def result(
    source_id: str,
    status: ScanStatus = ScanStatus.SUCCESS,
    *,
    jobs: tuple = (),
    error: str | None = None,
) -> ScanResult:
    return ScanResult(
        source_id=source_id,
        started_at=START,
        finished_at=START,
        status=status,
        records_seen=len(jobs),
        records_parsed=len(jobs),
        jobs=jobs,
        error_summary=error,
    )


class ScriptedAdapter:
    """Serves a queued list of ScanResults; the last one repeats."""

    adapter_id = "scripted"

    def __init__(self, results: list[ScanResult]) -> None:
        self._results = results
        self.calls = 0

    def fetch_scan(self, config, http_client=None, *, known=None) -> ScanResult:
        index = min(self.calls, len(self._results) - 1)
        self.calls += 1
        return self._results[index]


def palantir(**overrides) -> SourceConfig:
    options = {"site": "palantir"}
    return SourceConfig(
        id="palantir",
        employer="Palantir Technologies",
        adapter="scripted",
        options=options,
        **overrides,
    )


def second_source() -> SourceConfig:
    return SourceConfig(id="acme", employer="Acme", adapter="scripted", cadence_minutes=15)


def app_config(*sources: SourceConfig) -> AppConfig:
    return AppConfig(database_path=Path("data/jobscout.db"), sources=tuple(sources))


def make_scheduler(
    storage: Storage,
    app: AppConfig,
    clock: StepClock,
    *,
    adapters: dict[str, ScriptedAdapter] | None = None,
    notifier=None,
    notice_sender=None,
    chat_id: str = "555000111",
    jitter_seconds: float = 0.0,
    sleep=None,
) -> Scheduler:
    registry = adapters or {}

    def factory(adapter_id: str):
        if adapter_id in registry:
            return registry[adapter_id]
        raise AssertionError(f"unexpected adapter '{adapter_id}'")

    return Scheduler(
        storage,
        app,
        http_client=object(),  # type: ignore[arg-type] - adapters here ignore it
        notifier=notifier,
        notice_sender=notice_sender,
        chat_id=chat_id,
        rules=DEFAULT_RULES,
        clock=clock,
        sleep=sleep or (lambda _seconds: None),
        jitter_seconds=jitter_seconds,
        random_source=random.Random(0),
        adapter_factory=factory,
    )


# due-ness and cadence -----------------------------------------------------


def test_first_pass_scans_a_source_that_has_never_run(store: Storage) -> None:
    clock = StepClock()
    adapter = ScriptedAdapter([result("palantir", jobs=(job("palantir"),))])
    scheduler = make_scheduler(store, app_config(palantir()), clock, adapters={"scripted": adapter})

    runs = scheduler.run_once()

    assert [run.source_id for run in runs] == ["palantir"]
    assert runs[0].scanned is True
    assert runs[0].status == "success"
    assert runs[0].outcome.new_jobs == 1
    assert adapter.calls == 1
    assert runs[0].next_run_at == "2026-10-23T09:30:00Z"
    assert store.get_source_state("palantir").next_run_at == "2026-10-23T09:30:00Z"


def test_a_source_is_not_due_again_before_its_cadence(store: Storage) -> None:
    clock = StepClock()
    adapter = ScriptedAdapter([result("palantir")])
    scheduler = make_scheduler(store, app_config(palantir()), clock, adapters={"scripted": adapter})

    scheduler.run_once()
    clock.advance(minutes=29)
    assert scheduler.run_once() == ()

    clock.advance(minutes=1)
    assert [run.source_id for run in scheduler.run_once()] == ["palantir"]
    assert adapter.calls == 2


def test_jitter_delays_the_next_run(store: Storage) -> None:
    clock = StepClock()
    adapter = ScriptedAdapter([result("palantir")])
    scheduler = make_scheduler(
        store,
        app_config(palantir()),
        clock,
        adapters={"scripted": adapter},
        jitter_seconds=120.0,
    )

    runs = scheduler.run_once()

    assert runs[0].next_run_at > "2026-10-23T09:30:00Z"
    assert runs[0].next_run_at < "2026-10-23T09:33:00Z"


def test_disabled_sources_are_never_scheduled(store: Storage) -> None:
    clock = StepClock()
    adapter = ScriptedAdapter([result("palantir")])
    disabled = SourceConfig(
        id="acme", employer="Acme", adapter="scripted", enabled=False, cadence_minutes=15
    )
    scheduler = make_scheduler(
        store,
        app_config(palantir(), disabled),
        clock,
        adapters={"scripted": adapter},
    )

    runs = scheduler.run_once()

    assert [run.source_id for run in runs] == ["palantir"]
    assert store.get_source_state("acme") is None


# failure isolation --------------------------------------------------------


def test_one_broken_source_does_not_stop_the_others(store: Storage) -> None:
    clock = StepClock()
    good = ScriptedAdapter([result("acme", jobs=(job("acme"),))])
    app = app_config(palantir(), second_source())

    def factory(adapter_id: str):
        return good

    scheduler = Scheduler(
        store,
        app,
        http_client=object(),  # type: ignore[arg-type]
        chat_id=None,
        clock=clock,
        adapter_factory=factory,
    )
    # Make the first source's adapter raise while the second one works.
    original = scheduler._adapter_factory  # noqa: SLF001 - test seam

    def selective(adapter_id: str):
        if adapter_id == "scripted" and selective.first is None:
            selective.first = True
            raise RuntimeError("adapter exploded")
        return good

    selective.first = None
    scheduler._adapter_factory = selective  # noqa: SLF001 - test seam
    assert original is not None

    runs = scheduler.run_once()

    assert [run.source_id for run in runs] == ["palantir", "acme"]
    assert runs[0].scanned is False
    assert "adapter exploded" in (runs[0].error or "")
    assert runs[1].scanned is True
    # A broken source is still rescheduled, so it cannot spin.
    assert runs[0].next_run_at is not None


# failure notices ----------------------------------------------------------


def _failing_scheduler(
    store: Storage, clock: StepClock, transport, *, chat_id: str = "555000111"
) -> Scheduler:
    adapter = ScriptedAdapter([result("palantir", ScanStatus.FAILED, error="request failed")])
    return make_scheduler(
        store,
        app_config(palantir()),
        clock,
        adapters={"scripted": adapter},
        notice_sender=FailureNoticeSender(transport, clock=clock),
        chat_id=chat_id,
    )


def test_failure_notice_is_sent_once_after_three_failures(store: Storage, fake_transport) -> None:
    clock = StepClock()
    transport = fake_transport()
    scheduler = _failing_scheduler(store, clock, transport)

    first = scheduler.run_once()[0]
    clock.advance(minutes=30)
    second = scheduler.run_once()[0]
    clock.advance(minutes=30)
    third = scheduler.run_once()[0]
    clock.advance(minutes=30)
    fourth = scheduler.run_once()[0]

    assert first.notice is None or first.notice.attempted is False
    assert second.notice is None or second.notice.attempted is False
    assert third.notice is not None and third.notice.attempted is True
    assert third.notice.outcome == "sent"
    assert fourth.notice is None or fourth.notice.attempted is False
    assert len(transport.sent) == 1
    assert "has failed 3 consecutive scans" in transport.sent[0][1]
    assert store.get_source_state("palantir").failure_notice_outcome == "sent"


def test_recovery_clears_the_episode_so_a_later_streak_notifies_again(
    store: Storage, fake_transport
) -> None:
    clock = StepClock()
    transport = fake_transport()
    failing = ScriptedAdapter([result("palantir", ScanStatus.FAILED, error="boom")])
    healthy = ScriptedAdapter([result("palantir", jobs=(job("palantir"),))])
    scheduler = make_scheduler(
        store,
        app_config(palantir()),
        clock,
        adapters={"scripted": failing},
        notice_sender=FailureNoticeSender(transport, clock=clock),
    )

    for _ in range(3):
        scheduler.run_once()
        clock.advance(minutes=30)
    assert len(transport.sent) == 1
    assert store.get_source_state("palantir").failure_notice_at is not None

    scheduler._adapter_factory = lambda _adapter_id: healthy  # noqa: SLF001 - test seam
    scheduler.run_once()
    state = store.get_source_state("palantir")
    assert state.consecutive_failures == 0
    assert state.failure_notice_at is None
    assert state.failure_notice_outcome is None

    scheduler._adapter_factory = lambda _adapter_id: failing  # noqa: SLF001 - test seam
    clock.advance(minutes=30)
    for _ in range(3):
        scheduler.run_once()
        clock.advance(minutes=30)

    assert len(transport.sent) == 2


def test_no_notice_without_a_chat_id(store: Storage, fake_transport) -> None:
    clock = StepClock()
    transport = fake_transport()
    scheduler = _failing_scheduler(store, clock, transport, chat_id="")

    for _ in range(4):
        scheduler.run_once()
        clock.advance(minutes=30)

    assert transport.sent == []
    assert store.get_source_state("palantir").failure_notice_at is None


def test_ambiguous_notice_is_recorded_and_not_retried(store: Storage, fake_transport) -> None:
    clock = StepClock()
    transport = fake_transport([DeliveryResult(DeliveryOutcome.AMBIGUOUS, error="timeout")])
    scheduler = _failing_scheduler(store, clock, transport)

    for _ in range(5):
        scheduler.run_once()
        clock.advance(minutes=30)

    state = store.get_source_state("palantir")
    assert len(transport.sent) == 1
    assert state.failure_notice_outcome == "ambiguous"


def test_partial_scans_do_not_count_as_failures(store: Storage, fake_transport) -> None:
    clock = StepClock()
    transport = fake_transport()
    partial = ScriptedAdapter([result("palantir", ScanStatus.PARTIAL)])
    scheduler = make_scheduler(
        store,
        app_config(palantir()),
        clock,
        adapters={"scripted": partial},
        notice_sender=FailureNoticeSender(transport, clock=clock),
    )

    for _ in range(4):
        scheduler.run_once()
        clock.advance(minutes=30)

    assert transport.sent == []
    assert store.get_source_state("palantir").consecutive_failures == 0


def test_notice_text_omits_missing_detail() -> None:
    from jobscout.models import SourceState

    state = SourceState(
        id="palantir",
        employer="Palantir Technologies",
        adapter="lever",
        enabled=True,
        cadence_minutes=30,
        baseline_scan_id=1,
        baseline_completed_at=START,
        last_success_at=None,
        last_scan_status="failed",
        last_scan_complete=False,
        last_error_at=None,
        last_error_summary=None,
        consecutive_failures=3,
        next_run_at=None,
        records_seen=None,
        records_parsed=None,
        skipped_count=None,
    )

    text = format_failure_notice(state)

    assert "Palantir Technologies" in text
    assert "Last error" not in text
    assert "Check: jobscout status" in text


# restart recovery ---------------------------------------------------------


def test_restart_resumes_from_persisted_next_run_at(tmp_path: Path) -> None:
    db_path = tmp_path / "scheduler.db"
    clock = StepClock()

    with Storage.open(db_path) as storage:
        adapter = ScriptedAdapter([result("palantir")])
        scheduler = make_scheduler(
            storage, app_config(palantir()), clock, adapters={"scripted": adapter}
        )
        scheduler.run_once()
        assert adapter.calls == 1

    with Storage.open(db_path) as storage:
        adapter = ScriptedAdapter([result("palantir")])
        scheduler = make_scheduler(
            storage, app_config(palantir()), clock, adapters={"scripted": adapter}
        )
        assert scheduler.run_once() == ()  # not due yet after the restart
        assert adapter.calls == 0

        clock.advance(minutes=31)
        assert [run.source_id for run in scheduler.run_once()] == ["palantir"]
        assert adapter.calls == 1


def test_missed_cycles_do_not_cause_a_burst(store: Storage) -> None:
    clock = StepClock()
    adapter = ScriptedAdapter([result("palantir")])
    scheduler = make_scheduler(store, app_config(palantir()), clock, adapters={"scripted": adapter})

    scheduler.run_once()
    clock.advance(minutes=300)  # ten missed cycles

    runs = scheduler.run_once()

    assert len(runs) == 1
    assert adapter.calls == 2
    assert runs[0].next_run_at == "2026-10-23T14:30:00Z"


def test_queued_alerts_are_delivered_after_a_restart(
    tmp_path: Path, clock, make_notifier, fake_transport
) -> None:
    db_path = tmp_path / "scheduler.db"
    step = StepClock()
    new_job = result("palantir", jobs=(job("palantir", "R1"),))
    with Storage.open(db_path) as storage:
        # Queue an alert without delivering it: no notifier in these passes.
        baseline = ScriptedAdapter([result("palantir")])
        make_scheduler(
            storage,
            app_config(palantir()),
            step,
            adapters={"scripted": baseline},
        ).run_once()
        step.advance(minutes=30)
        make_scheduler(
            storage,
            app_config(palantir()),
            step,
            adapters={"scripted": ScriptedAdapter([new_job])},
        ).run_once()

    with Storage.open(db_path) as storage:
        pending = storage.list_pending_notifications("555000111")
        assert len(pending) == 1

        transport = fake_transport()
        step.advance(minutes=30)
        runs = make_scheduler(
            storage,
            app_config(palantir()),
            step,
            adapters={"scripted": ScriptedAdapter([new_job])},
            notifier=make_notifier(transport),
        ).run_once()

    assert runs[0].delivery is not None and runs[0].delivery.sent == 1
    assert len(transport.sent) == 1
    assert "R1" in transport.sent[0][1] or "Graduate Software Engineer" in transport.sent[0][1]


# loop behaviour -----------------------------------------------------------


def test_run_forever_honours_max_iterations_and_sleeps(store: Storage) -> None:
    clock = StepClock()
    adapter = ScriptedAdapter([result("palantir")])
    sleeps: list[float] = []
    scheduler = make_scheduler(
        store,
        app_config(palantir()),
        clock,
        adapters={"scripted": adapter},
        sleep=sleeps.append,
    )
    batches: list[int] = []

    runs = scheduler.run_forever(max_iterations=2, on_run=lambda batch: batches.append(len(batch)))

    assert len(batches) == 2
    assert batches[0] == 1  # first pass: the source is due
    assert batches[1] == 0  # second pass: nothing due yet
    assert len(sleeps) == 2
    assert all(value >= 0 for value in sleeps)
    assert adapter.calls == 1
    assert runs[0].scanned is True


def test_baseline_then_new_role_flows_through_the_scheduler(store: Storage) -> None:
    clock = StepClock()
    adapter = ScriptedAdapter(
        [
            result("palantir", jobs=(job("palantir", "R1"),)),
            result(
                "palantir",
                jobs=(job("palantir", "R1"), job("palantir", "R2", "Graduate Data Engineer")),
            ),
        ]
    )
    scheduler = make_scheduler(store, app_config(palantir()), clock, adapters={"scripted": adapter})

    baseline = scheduler.run_once()[0]
    clock.advance(minutes=30)
    later = scheduler.run_once()[0]

    assert baseline.outcome.is_baseline is True
    assert baseline.outcome.notifications_queued == 0
    assert later.outcome.new_jobs == 1
    assert later.outcome.notifications_queued == 1
    assert store.get_job("palantir", "R2").status is JobStatus.OPEN
