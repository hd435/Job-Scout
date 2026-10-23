"""Scan orchestration: baseline, new-role, update, closure, and restart rules.

Only a complete scan may establish a baseline, make a newly discovered role
alert-eligible, or count toward closure (architecture section 4).
"""

from __future__ import annotations

from collections.abc import Callable

from .config import SourceConfig
from .http import HttpClient
from .matching import DEFAULT_RULES, MatchingRules, match_job
from .models import (
    AlertCandidate,
    Job,
    JobStatus,
    MatchDecision,
    MatchOutcome,
    ScanOutcome,
    ScanResult,
    ScanStatus,
)
from .sources.base import SourceAdapter
from .storage import Storage, utc_now_iso

CLOSURE_MISS_THRESHOLD = 2

Clock = Callable[[], str]


def run_scan(
    storage: Storage,
    adapter: SourceAdapter,
    config: SourceConfig,
    http_client: HttpClient,
    *,
    accept_empty: bool = False,
    notify_chat_id: str | None = None,
    rules: MatchingRules = DEFAULT_RULES,
    clock: Clock = utc_now_iso,
) -> ScanOutcome:
    """Scan one source and apply every state transition in a single transaction."""
    storage.ensure_source(config)
    state = storage.get_source_state(config.id)
    baseline_existed = bool(state and state.baseline_scan_id is not None)

    result = _fetch(adapter, config, http_client, clock(), storage.job_revisions(config.id))
    if result.status is ScanStatus.SUCCESS_EMPTY and not accept_empty:
        previous_parsed = storage.last_complete_scan_records_parsed(config.id)
        if previous_parsed:
            result = result.downgraded_to_partial(
                "empty result from a previously non-empty source; "
                "re-run with --accept-empty to confirm the source is empty"
            )

    is_baseline = result.complete and not baseline_existed
    now = clock()

    with storage.transaction():
        scan_id = storage.record_scan(result, is_baseline=is_baseline)
        seen_ids: set[str] = set()
        candidates: list[AlertCandidate] = []
        new_jobs = 0
        updated_jobs = 0
        reopened_jobs = 0
        needs_review = 0
        notifications_queued = 0
        observed_only_jobs = 0

        for job in result.jobs:
            decision = match_job(job, rules)
            if decision.decision is MatchOutcome.NEEDS_REVIEW:
                needs_review += 1
            seen_ids.add(job.source_job_id)
            existing = storage.get_job(job.source_id, job.source_job_id)
            eligible = result.complete and not is_baseline and baseline_existed

            if existing is None:
                job_id = storage.insert_job(
                    job,
                    scan_id=scan_id,
                    decision=decision,
                    first_complete_scan_id=scan_id if result.complete else None,
                    now=now,
                )
                new_jobs += 1
                if eligible and decision.is_match:
                    candidates.append(_candidate(job_id, job, decision))
                    notifications_queued += _queue_notification(
                        storage, notify_chat_id, job_id, now=now
                    )
                continue

            changed = existing.job.content_hash != job.content_hash
            if result.complete and existing.status is JobStatus.CLOSED:
                reopened_jobs += 1
            storage.update_job_observation(
                existing.id,
                job,
                scan_id=scan_id,
                decision=decision,
                first_complete_scan_id=scan_id if result.complete else None,
                now=now,
            )
            if changed:
                updated_jobs += 1
            newly_eligible = eligible and existing.first_complete_scan_id is None
            if newly_eligible and decision.is_match:
                candidates.append(_candidate(existing.id, job, decision))
                notifications_queued += _queue_notification(
                    storage, notify_chat_id, existing.id, now=now
                )

        for source_job_id in result.observed_only_ids:
            existing = storage.get_job(config.id, source_job_id)
            if existing is None:
                # The adapter reported an unknown role as observed; it cannot be alert
                # eligible without details, and it stays a miss for closure purposes.
                continue
            seen_ids.add(source_job_id)
            observed_only_jobs += 1
            if result.complete and existing.status is JobStatus.CLOSED:
                reopened_jobs += 1
            storage.mark_job_observed(existing.id, scan_id=scan_id, reopen=result.complete)

        closed_jobs = 0
        if result.complete:
            closed_jobs = _apply_closure(storage, config.id, seen_ids, now=now)

        storage.update_source_after_scan(config.id, result, now=now, is_baseline=is_baseline)

    return ScanOutcome(
        scan_id=scan_id,
        source_id=config.id,
        status=result.status,
        complete=result.complete,
        is_baseline=is_baseline,
        records_seen=result.records_seen,
        records_parsed=result.records_parsed,
        skipped=result.skipped,
        new_jobs=new_jobs,
        updated_jobs=updated_jobs,
        reopened_jobs=reopened_jobs,
        closed_jobs=closed_jobs,
        needs_review=needs_review,
        candidates=tuple(candidates),
        error_summary=result.error_summary,
        notifications_queued=notifications_queued,
        observed_only_jobs=observed_only_jobs,
    )


def _queue_notification(storage: Storage, chat_id: str | None, job_id: int, *, now: str) -> int:
    """Queue one alert per role. Returns 0 when delivery is not configured or already queued."""
    if not chat_id:
        return 0
    return 1 if storage.create_pending_notification(chat_id, job_id, now=now) is not None else 0


def _fetch(
    adapter: SourceAdapter,
    config: SourceConfig,
    http_client: HttpClient,
    started_at: str,
    known: dict[str, str | None],
) -> ScanResult:
    """Adapters report their own failures; this is the last line of defence."""
    try:
        return adapter.fetch_scan(config, http_client, known=known)
    except Exception as exc:  # noqa: BLE001 - one bad scan must not stop the process
        return ScanResult(
            source_id=config.id,
            started_at=started_at,
            finished_at=utc_now_iso(),
            status=ScanStatus.FAILED,
            records_seen=0,
            records_parsed=0,
            error_summary=f"adapter error: {type(exc).__name__}: {exc}",
        )


def _apply_closure(storage: Storage, source_id: str, seen_ids: set[str], *, now: str) -> int:
    closed = 0
    for stored in storage.list_open_jobs(source_id):
        if stored.job.source_job_id in seen_ids:
            continue
        misses = storage.register_miss(stored.id, now=now)
        if misses >= CLOSURE_MISS_THRESHOLD:
            storage.close_job(stored.id, now=now)
            closed += 1
    return closed


def _candidate(job_id: int, job: Job, decision: MatchDecision) -> AlertCandidate:
    return AlertCandidate(
        job_id=job_id,
        title=job.title,
        employer=job.employer,
        location_raw=job.location_raw,
        url=job.url,
        salary_text=job.salary_text,
        closing_at=job.closing_at.isoformat() if job.closing_at else None,
        decision=decision,
    )
