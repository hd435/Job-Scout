from __future__ import annotations

from pathlib import Path

from jobscout.http import HttpError
from jobscout.models import JobStatus, ScanStatus
from jobscout.scanner import run_scan
from jobscout.sources import get_adapter
from jobscout.storage import Storage


def test_first_complete_scan_establishes_a_silent_baseline(scan, store) -> None:
    outcome = scan(
        [
            {"id": "a", "text": "Graduate Software Engineer", "hostedUrl": "https://x/a"},
            {"id": "b", "text": "Senior Software Engineer", "hostedUrl": "https://x/b"},
        ]
    )

    assert outcome.status is ScanStatus.SUCCESS
    assert outcome.complete is True
    assert outcome.is_baseline is True
    assert outcome.new_jobs == 2
    assert outcome.candidates == ()

    state = store.get_source_state("palantir")
    assert state.baseline_scan_id == outcome.scan_id
    assert state.baseline_completed_at is not None
    assert state.last_scan_status == "success"
    assert state.last_scan_complete is True
    assert state.consecutive_failures == 0


def test_new_role_after_baseline_produces_one_candidate(scan, record) -> None:
    scan([record("a", "Graduate Software Engineer")])

    outcome = scan(
        [
            record("a", "Graduate Software Engineer"),
            record("b", "Data Engineer Intern"),
        ]
    )

    assert outcome.is_baseline is False
    assert outcome.new_jobs == 1
    assert [candidate.title for candidate in outcome.candidates] == ["Data Engineer Intern"]
    assert outcome.candidates[0].decision.is_match is True


def test_repeating_the_same_snapshot_produces_no_duplicate_candidate(scan, record) -> None:
    payload = [record("a", "Graduate Software Engineer")]
    scan(payload)

    outcome = scan(payload)

    assert outcome.candidates == ()
    assert outcome.new_jobs == 0


def test_changed_role_details_update_without_re_alerting(scan, store, record) -> None:
    scan([record("a", "Graduate Software Engineer", salary={"min": 40000, "currency": "GBP"})])
    before = store.get_job("palantir", "a")

    outcome = scan(
        [
            record(
                "a",
                "Graduate Software Engineer",
                "Manchester, United Kingdom",
                salary={"min": 45000, "currency": "GBP"},
                description="Updated description.",
            )
        ]
    )
    after = store.get_job("palantir", "a")

    assert outcome.candidates == ()
    assert outcome.updated_jobs == 1
    assert after.job.location_raw == "Manchester, United Kingdom"
    assert after.job.content_hash != before.job.content_hash
    assert after.first_complete_scan_id == before.first_complete_scan_id


def test_role_closes_after_two_consecutive_complete_misses(scan, store, record) -> None:
    scan([record("a", "Graduate Software Engineer"), record("b", "Graduate Data Engineer")])

    first_miss = scan([record("a", "Graduate Software Engineer")])
    still_open = store.get_job("palantir", "b")

    assert first_miss.closed_jobs == 0
    assert still_open.status is JobStatus.OPEN
    assert still_open.consecutive_misses == 1

    second_miss = scan([record("a", "Graduate Software Engineer")])
    closed = store.get_job("palantir", "b")

    assert second_miss.closed_jobs == 1
    assert closed.status is JobStatus.CLOSED
    assert closed.closed_at is not None


def test_partial_scan_never_increments_closure_misses(scan, store, record) -> None:
    scan([record("a", "Graduate Software Engineer"), record("b", "Graduate Data Engineer")])

    outcome = scan([record("a", "Graduate Software Engineer"), {"id": "bad", "text": "No URL"}])

    assert outcome.status is ScanStatus.PARTIAL
    assert outcome.complete is False
    assert outcome.closed_jobs == 0
    assert store.get_job("palantir", "b").consecutive_misses == 0


def test_failed_scan_never_increments_closure_misses(scan, store, record) -> None:
    scan([record("a", "Graduate Software Engineer"), record("b", "Graduate Data Engineer")])

    outcome = scan(None, error=HttpError("upstream down", status=503, retryable=True))

    assert outcome.status is ScanStatus.FAILED
    assert outcome.complete is False
    assert store.get_job("palantir", "b").consecutive_misses == 0
    assert store.get_source_state("palantir").consecutive_failures == 1


def test_role_first_seen_in_a_partial_scan_becomes_eligible_in_a_complete_scan(
    scan, store, record
) -> None:
    scan([record("a", "Graduate Software Engineer")])

    partial = scan(
        [
            record("a", "Graduate Software Engineer"),
            record("b", "Graduate Data Engineer"),
            {"id": "bad", "text": "No URL"},
        ]
    )
    partial_job = store.get_job("palantir", "b")

    assert partial.status is ScanStatus.PARTIAL
    assert partial.candidates == ()
    assert partial_job.first_complete_scan_id is None

    complete = scan(
        [record("a", "Graduate Software Engineer"), record("b", "Graduate Data Engineer")]
    )

    assert [candidate.title for candidate in complete.candidates] == ["Graduate Data Engineer"]
    assert store.get_job("palantir", "b").first_complete_scan_id == complete.scan_id


def test_partial_first_scan_does_not_establish_a_baseline(scan, store, record) -> None:
    partial = scan([record("a", "Graduate Software Engineer"), {"id": "bad", "text": "No URL"}])

    assert partial.status is ScanStatus.PARTIAL
    assert partial.is_baseline is False
    assert store.get_source_state("palantir").baseline_scan_id is None

    complete = scan([record("a", "Graduate Software Engineer")])

    assert complete.is_baseline is True
    assert complete.candidates == ()
    assert store.get_source_state("palantir").baseline_scan_id == complete.scan_id


def test_valid_empty_first_scan_is_a_complete_baseline(scan, store) -> None:
    outcome = scan([])

    assert outcome.status is ScanStatus.SUCCESS_EMPTY
    assert outcome.complete is True
    assert outcome.is_baseline is True
    assert store.get_source_state("palantir").has_baseline is True


def test_suspicious_empty_result_from_non_empty_source_is_partial(scan, store, record) -> None:
    scan([record("a", "Graduate Software Engineer"), record("b", "Graduate Data Engineer")])

    outcome = scan([])

    assert outcome.status is ScanStatus.PARTIAL
    assert outcome.complete is False
    assert outcome.closed_jobs == 0
    assert outcome.error_summary is not None
    assert store.get_job("palantir", "a").consecutive_misses == 0


def test_accepted_empty_result_follows_the_normal_closure_rule(scan, store, record) -> None:
    scan([record("a", "Graduate Software Engineer")])

    first = scan([], accept_empty=True)
    assert first.status is ScanStatus.SUCCESS_EMPTY
    assert first.closed_jobs == 0
    assert store.get_job("palantir", "a").consecutive_misses == 1

    second = scan([], accept_empty=True)
    assert second.closed_jobs == 1
    assert store.get_job("palantir", "a").status is JobStatus.CLOSED


def test_closed_role_reappearing_reopens_without_re_alerting(scan, store, record) -> None:
    payload = [record("a", "Graduate Software Engineer"), record("b", "Graduate Data Engineer")]
    scan(payload)
    scan([payload[0]])
    scan([payload[0]])
    assert store.get_job("palantir", "b").status is JobStatus.CLOSED

    outcome = scan(payload)

    assert outcome.reopened_jobs == 1
    assert outcome.candidates == ()
    reopened = store.get_job("palantir", "b")
    assert reopened.status is JobStatus.OPEN
    assert reopened.consecutive_misses == 0


def test_failure_counters_reset_only_on_success(scan, store, record) -> None:
    errors = HttpError("upstream down", status=500, retryable=True)
    scan(None, error=errors)
    scan(None, error=errors)
    assert store.get_source_state("palantir").consecutive_failures == 2

    scan([record("a", "x"), {"id": "bad", "text": "No URL"}])
    assert store.get_source_state("palantir").consecutive_failures == 2

    scan([record("a", "Graduate Software Engineer")])
    assert store.get_source_state("palantir").consecutive_failures == 0


def test_restart_preserves_state_and_prevents_duplicate_candidates(
    tmp_path: Path, palantir, clock, fake_http, record
) -> None:
    db_path = tmp_path / "jobscout.db"
    payload = [record("a", "Graduate Software Engineer")]

    with Storage.open(db_path) as first_session:
        baseline = run_scan(
            first_session, get_adapter("lever"), palantir, fake_http(payload), clock=clock
        )
    with Storage.open(db_path) as second_session:
        follow_up = run_scan(
            second_session, get_adapter("lever"), palantir, fake_http(payload), clock=clock
        )
        state = second_session.get_source_state("palantir")

    assert baseline.is_baseline is True
    assert follow_up.is_baseline is False
    assert follow_up.candidates == ()
    assert state.baseline_scan_id == baseline.scan_id


def test_baseline_candidates_are_never_produced_even_for_matching_roles(scan) -> None:
    outcome = scan(
        [
            {"id": "a", "text": "Graduate Software Engineer", "hostedUrl": "https://x/a"},
            {"id": "b", "text": "Machine Learning Engineer, Intern", "hostedUrl": "https://x/b"},
        ]
    )

    assert outcome.candidates == ()
    assert outcome.new_jobs == 2
