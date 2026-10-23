"""Trial measurement: a read-only scorecard over persisted scan and delivery history.

The seven-day trial (M5.4) needs evidence, but nothing in this project records a process
heartbeat, and adding one would mean new operational state for little benefit. Availability
is therefore inferred from the scans a scheduler actually wrote, compared with the cycles
its cadence implies (D-064). The honest reading is "the schedule produced scans", not "the
process was up", and the output says so.

Everything here is a query and a format string: no writes, no network, no scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .models import ScanStatus
from .storage import Storage, utc_now_iso

MISSED_GAP_FACTOR = 1.5
DEFAULT_WINDOW_DAYS = 7


@dataclass(frozen=True)
class SourceTrial:
    """One source's reliability inside the measurement window."""

    source_id: str
    employer: str
    cadence_minutes: int
    expected_runs: int
    scans: int
    complete: int
    partial: int
    failed: int
    missed_episodes: int
    largest_gap_minutes: float | None
    mean_delay_minutes: float | None
    consecutive_failures: int
    last_success_at: str | None
    next_run_at: str | None
    enabled: bool = True

    @property
    def coverage(self) -> float:
        """Scans written over the cycles the cadence implies, capped at 1.0."""
        if self.expected_runs <= 0:
            return 0.0
        return min(1.0, self.scans / self.expected_runs)


@dataclass(frozen=True)
class DeliveryTally:
    """Notification states for rows created inside the window."""

    pending: int = 0
    sent: int = 0
    failed: int = 0
    ambiguous: int = 0

    @property
    def total(self) -> int:
        return self.pending + self.sent + self.failed + self.ambiguous


@dataclass(frozen=True)
class TrialReport:
    generated_at: str
    since: str
    days: int
    sources: tuple[SourceTrial, ...]
    deliveries: DeliveryTally

    @property
    def scans(self) -> int:
        return sum(source.scans for source in self.sources)

    @property
    def missed_episodes(self) -> int:
        return sum(source.missed_episodes for source in self.sources)


def build_trial_report(
    storage: Storage,
    *,
    now: str | None = None,
    days: int = DEFAULT_WINDOW_DAYS,
    source_id: str | None = None,
) -> TrialReport:
    """Summarise the window ending at `now`. Read-only; missing history is not an error.

    Disabled sources are included so their history stays visible, and flagged in the text
    output so they are not measured against the targets for sources that should be running.
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    moment = now or utc_now_iso()
    since = _iso(_parse_iso(moment) - timedelta(days=days))

    facts = storage.list_scan_facts(since=since)
    tallies = storage.count_notifications_by_status(since=since)

    trials: list[SourceTrial] = []
    for state in storage.list_source_states():
        if source_id is not None and state.id != source_id:
            continue
        own = [fact for fact in facts if fact.source_id == state.id]
        gaps = _gaps([fact.started_at for fact in own])
        cadence = state.cadence_minutes
        trials.append(
            SourceTrial(
                source_id=state.id,
                employer=state.employer,
                cadence_minutes=cadence,
                expected_runs=_expected_runs(days=days, cadence_minutes=cadence),
                scans=len(own),
                complete=sum(1 for fact in own if fact.complete),
                partial=sum(1 for fact in own if fact.status == str(ScanStatus.PARTIAL)),
                failed=sum(1 for fact in own if fact.status == str(ScanStatus.FAILED)),
                missed_episodes=sum(1 for gap in gaps if gap > MISSED_GAP_FACTOR * cadence),
                largest_gap_minutes=round(max(gaps), 1) if gaps else None,
                mean_delay_minutes=(
                    round(sum(gap - cadence for gap in gaps) / len(gaps), 1) if gaps else None
                ),
                consecutive_failures=state.consecutive_failures,
                last_success_at=state.last_success_at,
                next_run_at=state.next_run_at,
                enabled=state.enabled,
            )
        )

    return TrialReport(
        generated_at=moment,
        since=since,
        days=days,
        sources=tuple(trials),
        deliveries=DeliveryTally(
            pending=tallies.get("pending", 0),
            sent=tallies.get("sent", 0),
            failed=tallies.get("failed", 0),
            ambiguous=tallies.get("ambiguous", 0),
        ),
    )


def format_trial_report(report: TrialReport) -> str:
    """Plain text an operator can paste into a log or a review note."""
    lines = [
        f"Trial window: {report.since} to {report.generated_at} ({report.days} day(s))",
        f"Sources: {len(report.sources)} recorded ({_count_enabled(report)} enabled),"
        f" scans recorded: {report.scans}, missed episodes: {report.missed_episodes}",
        "",
    ]
    if not report.sources:
        lines.append("No source rows yet: run a scan or `jobscout schedule --once` first.")
    for source in report.sources:
        suffix = "" if source.enabled else " [disabled: not a trial target]"
        lines.append(
            f"{source.source_id} ({source.employer}) cadence {source.cadence_minutes}m{suffix}"
        )
        lines.append(
            f"  scans {source.scans}/{source.expected_runs} cycles"
            f" ({source.coverage * 100:.0f}%): complete {source.complete},"
            f" partial {source.partial}, failed {source.failed}"
        )
        lines.append(f"  gaps {_format_gaps(source)}")
        lines.append(f"  state {_format_state(source)}")
        lines.append("")

    tally = report.deliveries
    lines.append(
        f"Deliveries queued in window: {tally.total} (sent {tally.sent},"
        f" failed {tally.failed}, ambiguous {tally.ambiguous}, pending {tally.pending})"
    )
    lines.append("")
    lines.append(
        "Availability is inferred from scans written per expected cycle; no heartbeat is"
        " recorded, so a process that was down shows up as missing scans and long gaps."
    )
    return "\n".join(lines)


def _count_enabled(report: TrialReport) -> int:
    return sum(1 for source in report.sources if source.enabled)


def _format_gaps(source: SourceTrial) -> str:
    if source.largest_gap_minutes is None:
        return "none yet (fewer than two scans in the window)"
    delay = source.mean_delay_minutes or 0.0
    sign = "+" if delay >= 0 else ""
    return (
        f"longest {source.largest_gap_minutes:.0f}m,"
        f" {source.missed_episodes} missed episode(s), mean delay {sign}{delay:.1f}m"
    )


def _format_state(source: SourceTrial) -> str:
    return (
        f"consecutive failures {source.consecutive_failures},"
        f" last success {source.last_success_at or 'never'},"
        f" next run {source.next_run_at or 'unscheduled'}"
    )


def _expected_runs(*, days: int, cadence_minutes: int) -> int:
    if cadence_minutes <= 0:
        return 0
    return int(days * 24 * 60 // cadence_minutes)


def _gaps(started_at_values: list[str]) -> list[float]:
    """Minutes between consecutive scans, which is where downtime and missed runs show."""
    moments = [_parse_iso(value) for value in started_at_values]
    moments.sort()
    pairs = zip(moments, moments[1:], strict=False)
    return [(later - earlier).total_seconds() / 60 for earlier, later in pairs]


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "DeliveryTally",
    "SourceTrial",
    "TrialReport",
    "build_trial_report",
    "format_trial_report",
]
