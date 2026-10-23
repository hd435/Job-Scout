"""Contract tests for observed-but-not-refreshed roles (M4 incremental scanning)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from jobscout.config import SourceConfig
from jobscout.matching import DEFAULT_RULES
from jobscout.models import JobStatus, ScanOutcome, ScanResult, ScanStatus, SkipReason
from jobscout.scanner import run_scan
from jobscout.sources.base import build_job
from jobscout.storage import Storage

SOURCE_ID = "incremental"


@pytest.fixture
def source() -> SourceConfig:
    return SourceConfig(id=SOURCE_ID, employer="Incremental Ltd", adapter="scripted", options={})


def make_job(
    source_job_id: str,
    *,
    title: str = "Graduate Software Engineer",
    revision: str | None = "rev-1",
    description: str = "A role description.",
) -> Any:
    return build_job(
        source_id=SOURCE_ID,
        source_job_id=source_job_id,
        employer="Incremental Ltd",
        title=title,
        location_raw="London, United Kingdom",
        url=f"https://example.test/{source_job_id}",
        description=description,
        source_revision=revision,
    )


class ScriptedAdapter:
    """Returns a scripted ScanResult and records the `known` hint it received."""

    adapter_id = "scripted"

    def __init__(self, build: Callable[[dict[str, str | None]], ScanResult]) -> None:
        self._build = build
        self.known: list[dict[str, str | None]] = []

    def fetch_scan(self, config: SourceConfig, http_client: Any, *, known=None) -> ScanResult:
        snapshot = dict(known or {})
        self.known.append(snapshot)
        result = self._build(snapshot)
        return result


def result_of(
    *,
    jobs: tuple = (),
    observed_ids: tuple[str, ...] = (),
    status: ScanStatus = ScanStatus.SUCCESS,
    skipped: tuple[SkipReason, ...] = (),
) -> ScanResult:
    return ScanResult(
        source_id=SOURCE_ID,
        started_at="2026-10-23T09:00:00Z",
        finished_at="2026-10-23T09:00:05Z",
        status=status,
        records_seen=len(jobs) + len(observed_ids),
        records_parsed=len(jobs) + len(observed_ids),
        jobs=jobs,
        skipped=skipped,
        observed_ids=observed_ids,
    )


def scripted_scan(storage: Storage, source: SourceConfig, adapter: ScriptedAdapter) -> ScanOutcome:
    return run_scan(storage, adapter, source, http_client=None, rules=DEFAULT_RULES)  # type: ignore[arg-type]


def test_observed_only_roles_keep_details_and_stay_open(store: Storage, source) -> None:
    scripted_scan(
        store,
        source,
        ScriptedAdapter(
            lambda _known: result_of(
                jobs=(make_job("R1"), make_job("R2", title="Graduate Analyst"))
            )
        ),
    )
    before = store.get_job(SOURCE_ID, "R2")

    outcome = scripted_scan(
        store, source, ScriptedAdapter(lambda _known: result_of(observed_ids=("R1", "R2")))
    )

    assert outcome.status is ScanStatus.SUCCESS
    assert outcome.observed_only_jobs == 2
    assert outcome.closed_jobs == 0
    after = store.get_job(SOURCE_ID, "R2")
    assert after.status is JobStatus.OPEN
    assert after.consecutive_misses == 0
    assert after.job.title == before.job.title
    assert after.job.content_hash == before.job.content_hash
    assert after.last_seen_scan_id == outcome.scan_id


def test_observed_roles_are_not_mistaken_for_closures(store: Storage, source) -> None:
    scripted_scan(
        store,
        source,
        ScriptedAdapter(lambda _known: result_of(jobs=(make_job("R1"), make_job("R2")))),
    )

    # R1 is refreshed, R2 is unchanged but still listed: neither is a closure.
    scripted_scan(
        store,
        source,
        ScriptedAdapter(lambda _known: result_of(jobs=(make_job("R1"),), observed_ids=("R2",))),
    )
    assert store.get_job(SOURCE_ID, "R2").consecutive_misses == 0

    # R2 really disappears twice: only then does it close.
    scripted_scan(store, source, ScriptedAdapter(lambda _known: result_of(jobs=(make_job("R1"),))))
    assert store.get_job(SOURCE_ID, "R2").consecutive_misses == 1
    assert store.get_job(SOURCE_ID, "R2").status is JobStatus.OPEN

    final = scripted_scan(
        store, source, ScriptedAdapter(lambda _known: result_of(jobs=(make_job("R1"),)))
    )
    assert final.closed_jobs == 1
    assert store.get_job(SOURCE_ID, "R2").status is JobStatus.CLOSED


def test_observing_a_role_again_resets_closure_misses(store: Storage, source) -> None:
    scripted_scan(store, source, ScriptedAdapter(lambda _known: result_of(jobs=(make_job("R1"),))))
    scripted_scan(store, source, ScriptedAdapter(lambda _known: result_of(jobs=())))
    assert store.get_job(SOURCE_ID, "R1").consecutive_misses == 1

    scripted_scan(store, source, ScriptedAdapter(lambda _known: result_of(observed_ids=("R1",))))

    assert store.get_job(SOURCE_ID, "R1").consecutive_misses == 0
    assert store.get_job(SOURCE_ID, "R1").status is JobStatus.OPEN


def test_unknown_observed_role_is_not_alert_eligible(store: Storage, source) -> None:
    scripted_scan(store, source, ScriptedAdapter(lambda _known: result_of(jobs=(make_job("R1"),))))

    outcome = scripted_scan(
        store,
        source,
        ScriptedAdapter(
            lambda _known: result_of(jobs=(make_job("R1"),), observed_ids=("NEVER-SEEN",))
        ),
    )

    assert outcome.candidates == ()
    assert outcome.observed_only_jobs == 0
    assert store.get_job(SOURCE_ID, "NEVER-SEEN") is None


def test_partial_scan_with_observed_roles_changes_nothing_but_observation(
    store: Storage, source
) -> None:
    scripted_scan(store, source, ScriptedAdapter(lambda _known: result_of(jobs=(make_job("R1"),))))

    outcome = scripted_scan(
        store,
        source,
        ScriptedAdapter(
            lambda _known: result_of(
                jobs=(make_job("R1", title="Changed title"),),
                observed_ids=("R9",),
                status=ScanStatus.PARTIAL,
                skipped=(SkipReason("R9", "missing_title"),),
            )
        ),
    )

    assert outcome.status is ScanStatus.PARTIAL
    assert outcome.closed_jobs == 0
    assert outcome.candidates == ()
    stored = store.get_job(SOURCE_ID, "R1")
    assert stored.job.title == "Changed title"
    assert stored.first_complete_scan_id is not None


def test_adapter_receives_stored_revisions(store: Storage, source) -> None:
    first = ScriptedAdapter(
        lambda _known: result_of(jobs=(make_job("R1", revision="v1"), make_job("R2")))
    )
    scripted_scan(store, source, first)
    assert first.known == [{}]

    second = ScriptedAdapter(lambda _known: result_of(observed_ids=("R1", "R2")))
    scripted_scan(store, source, second)

    assert second.known == [{"R1": "v1", "R2": "rev-1"}]
    assert store.job_revisions(SOURCE_ID) == {"R1": "v1", "R2": "rev-1"}


def test_revision_change_alone_is_not_a_content_update(store: Storage, source) -> None:
    scripted_scan(store, source, ScriptedAdapter(lambda _known: result_of(jobs=(make_job("R1"),))))

    same_content = scripted_scan(
        store,
        source,
        ScriptedAdapter(
            lambda _known: result_of(
                jobs=(make_job("R1", revision="rev-9"),), status=ScanStatus.SUCCESS
            )
        ),
    )
    assert same_content.updated_jobs == 0
    assert store.get_job(SOURCE_ID, "R1").job.source_revision == "rev-9"

    changed = scripted_scan(
        store,
        source,
        ScriptedAdapter(
            lambda _known: result_of(
                jobs=(make_job("R1", revision="rev-10", description="Rewritten description."),)
            )
        ),
    )
    assert changed.updated_jobs == 1


def test_observed_only_ids_exclude_refreshed_roles() -> None:
    result = result_of(jobs=(make_job("R1"),), observed_ids=("R1", "R2", "R2"))

    assert result.observed_only_ids == ("R2",)


def test_observing_a_closed_role_reopens_it(store: Storage, source) -> None:
    scripted_scan(store, source, ScriptedAdapter(lambda _known: result_of(jobs=(make_job("R1"),))))
    scripted_scan(store, source, ScriptedAdapter(lambda _known: result_of()))
    scripted_scan(store, source, ScriptedAdapter(lambda _known: result_of()))
    assert store.get_job(SOURCE_ID, "R1").status is JobStatus.CLOSED

    outcome = scripted_scan(
        store, source, ScriptedAdapter(lambda _known: result_of(observed_ids=("R1",)))
    )

    assert outcome.reopened_jobs == 1
    assert store.get_job(SOURCE_ID, "R1").status is JobStatus.OPEN


def test_partial_scan_does_not_reopen_a_closed_role(store: Storage, source) -> None:
    scripted_scan(store, source, ScriptedAdapter(lambda _known: result_of(jobs=(make_job("R1"),))))
    scripted_scan(store, source, ScriptedAdapter(lambda _known: result_of()))
    scripted_scan(store, source, ScriptedAdapter(lambda _known: result_of()))
    closed = store.get_job(SOURCE_ID, "R1")
    assert closed.status is JobStatus.CLOSED
    assert closed.consecutive_misses == 2

    outcome = scripted_scan(
        store,
        source,
        ScriptedAdapter(
            lambda _known: result_of(
                observed_ids=("R1",),
                status=ScanStatus.PARTIAL,
                skipped=(SkipReason("R9", "missing_title"),),
            )
        ),
    )

    assert outcome.status is ScanStatus.PARTIAL
    assert outcome.reopened_jobs == 0
    assert outcome.candidates == ()
    stored = store.get_job(SOURCE_ID, "R1")
    assert stored.status is JobStatus.CLOSED
    assert stored.closed_at == closed.closed_at
    assert stored.consecutive_misses == closed.consecutive_misses
    assert stored.last_seen_scan_id == outcome.scan_id


def test_complete_scan_after_a_partial_observation_still_reopens(store: Storage, source) -> None:
    scripted_scan(store, source, ScriptedAdapter(lambda _known: result_of(jobs=(make_job("R1"),))))
    scripted_scan(store, source, ScriptedAdapter(lambda _known: result_of()))
    scripted_scan(store, source, ScriptedAdapter(lambda _known: result_of()))

    scripted_scan(
        store,
        source,
        ScriptedAdapter(lambda _known: result_of(observed_ids=("R1",), status=ScanStatus.PARTIAL)),
    )
    assert store.get_job(SOURCE_ID, "R1").status is JobStatus.CLOSED

    outcome = scripted_scan(
        store, source, ScriptedAdapter(lambda _known: result_of(observed_ids=("R1",)))
    )

    assert outcome.reopened_jobs == 1
    assert store.get_job(SOURCE_ID, "R1").status is JobStatus.OPEN
    assert store.get_job(SOURCE_ID, "R1").consecutive_misses == 0


def test_partial_observation_keeps_an_open_role_open_but_does_not_reopen(
    store: Storage, source
) -> None:
    scripted_scan(store, source, ScriptedAdapter(lambda _known: result_of(jobs=(make_job("R1"),))))
    scripted_scan(store, source, ScriptedAdapter(lambda _known: result_of()))
    assert store.get_job(SOURCE_ID, "R1").consecutive_misses == 1

    outcome = scripted_scan(
        store,
        source,
        ScriptedAdapter(lambda _known: result_of(observed_ids=("R1",), status=ScanStatus.PARTIAL)),
    )

    stored = store.get_job(SOURCE_ID, "R1")
    assert outcome.reopened_jobs == 0
    assert stored.status is JobStatus.OPEN
    assert stored.consecutive_misses == 0


def test_empty_observed_set_changes_nothing(store: Storage, source) -> None:
    scripted_scan(store, source, ScriptedAdapter(lambda _known: result_of(jobs=(make_job("R1"),))))

    first = scripted_scan(store, source, ScriptedAdapter(lambda _known: result_of()))
    second = scripted_scan(store, source, ScriptedAdapter(lambda _known: result_of()))

    assert first.observed_only_jobs == 0
    assert first.closed_jobs == 0
    assert second.closed_jobs == 1
    assert store.get_job(SOURCE_ID, "R1").status is JobStatus.CLOSED


def test_revisions_survive_a_restart(tmp_path: Path, source) -> None:
    db_path = tmp_path / "incremental.db"

    with Storage.open(db_path) as storage:
        scripted_scan(
            storage,
            source,
            ScriptedAdapter(lambda _known: result_of(jobs=(make_job("R1", revision="v1"),))),
        )
    with Storage.open(db_path) as storage:
        assert storage.job_revisions(SOURCE_ID) == {"R1": "v1"}
