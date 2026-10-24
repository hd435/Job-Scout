"""Normalized contracts shared by adapters, storage, matching, and the CLI.

These types are the internal boundary described in `docs/architecture.md`.
Adapters produce `Job` and `ScanResult`; the matcher produces `MatchDecision`;
storage persists and returns `StoredJob`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from typing import Any


class ScanStatus(StrEnum):
    """Outcome of a single source scan (architecture section 3.2)."""

    SUCCESS = "success"
    SUCCESS_EMPTY = "success_empty"
    PARTIAL = "partial"
    FAILED = "failed"

    @property
    def is_complete(self) -> bool:
        """Only complete scans may baseline, alert, or count toward closure."""
        return self in (ScanStatus.SUCCESS, ScanStatus.SUCCESS_EMPTY)

    @property
    def is_successful(self) -> bool:
        return self.is_complete


class MatchOutcome(StrEnum):
    MATCH = "match"
    NO_MATCH = "no_match"
    NEEDS_REVIEW = "needs_review"


class JobStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class NotificationStatus(StrEnum):
    """Persisted delivery state (architecture section 5.1)."""

    PENDING = "pending"
    SENT = "sent"
    AMBIGUOUS = "ambiguous"
    FAILED = "failed"


class DeliveryOutcome(StrEnum):
    """How a transport classifies a single send attempt."""

    SENT = "sent"
    RETRYABLE = "retryable"
    PERMANENT = "permanent"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class LocationResult:
    """Normalized location verdict. Location is never guessed."""

    qualifies: bool
    reason: str
    city: str | None = None
    country: str | None = None
    workplace_type: str | None = None
    ambiguous: bool = False

    def to_json(self) -> str:
        payload = {
            "qualifies": self.qualifies,
            "reason": self.reason,
            "city": self.city,
            "country": self.country,
            "workplace_type": self.workplace_type,
            "ambiguous": self.ambiguous,
        }
        return json.dumps(payload, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | None) -> LocationResult | None:
        if not raw:
            return None
        data = json.loads(raw)
        return cls(
            qualifies=bool(data["qualifies"]),
            reason=str(data["reason"]),
            city=data.get("city"),
            country=data.get("country"),
            workplace_type=data.get("workplace_type"),
            ambiguous=bool(data.get("ambiguous", False)),
        )


_CONTENT_HASH_FIELDS = (
    "source_id",
    "source_job_id",
    "employer",
    "title",
    "location_raw",
    "url",
    "description",
    "posted_at",
    "closing_at",
    "salary_text",
    "workplace_type",
)


def _hashable(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    return value


def compute_content_hash(
    *,
    source_id: str,
    source_job_id: str,
    employer: str,
    title: str,
    location_raw: str,
    url: str,
    description: str | None = None,
    posted_at: date | None = None,
    closing_at: date | None = None,
    salary_text: str | None = None,
    workplace_type: str | None = None,
) -> str:
    """Stable hash of the user-visible role fields, used to detect updates."""
    payload = {
        "source_id": source_id,
        "source_job_id": source_job_id,
        "employer": employer,
        "title": title,
        "location_raw": location_raw,
        "url": url,
        "description": description,
        "posted_at": _hashable(posted_at),
        "closing_at": _hashable(closing_at),
        "salary_text": salary_text,
        "workplace_type": workplace_type,
    }
    assert tuple(payload) == _CONTENT_HASH_FIELDS
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Job:
    """A single normalized role observed in one scan."""

    source_id: str
    source_job_id: str
    employer: str
    title: str
    location_raw: str
    location_normalized: LocationResult
    url: str
    content_hash: str
    description: str | None = None
    posted_at: date | None = None
    closing_at: date | None = None
    salary_text: str | None = None
    workplace_type: str | None = None
    source_revision: str | None = None

    @property
    def location_display(self) -> str:
        return self.location_raw.strip() or "unknown location"


@dataclass(frozen=True)
class SkipReason:
    """An observed-but-unusable record. Skips are always reported, never silent."""

    source_job_id: str | None
    reason: str
    detail: str | None = None

    def describe(self) -> str:
        parts = [self.reason]
        if self.source_job_id:
            parts.append(f"id={self.source_job_id}")
        if self.detail:
            parts.append(self.detail)
        return " | ".join(parts)


@dataclass(frozen=True)
class ScanResult:
    """Raw adapter output for one source scan.

    `jobs` holds the roles whose details were (re)read in this scan. `observed_ids` holds
    roles the source advertised without a detail refresh, because the adapter fetches a
    detail page only when a role is new or its source revision changed. The scan merge
    treats both sets as observed, so an unchanged role is never mistaken for a closure.
    """

    source_id: str
    started_at: str
    finished_at: str
    status: ScanStatus
    records_seen: int
    records_parsed: int
    jobs: tuple[Job, ...] = ()
    skipped: tuple[SkipReason, ...] = ()
    source_reported_total: int | None = None
    error_summary: str | None = None
    observed_ids: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.status.is_complete

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    @property
    def observed_only_ids(self) -> tuple[str, ...]:
        """Roles observed without a detail refresh in this scan."""
        refreshed = {job.source_job_id for job in self.jobs}
        return tuple(
            dict.fromkeys(
                source_job_id
                for source_job_id in self.observed_ids
                if source_job_id not in refreshed
            )
        )

    def downgraded_to_partial(self, reason: str) -> ScanResult:
        """Guard against treating a suspicious empty result as a complete scan."""
        return replace(
            self,
            status=ScanStatus.PARTIAL,
            error_summary=reason,
        )


@dataclass(frozen=True)
class MatchDecision:
    decision: MatchOutcome
    reasons: tuple[str, ...]
    rules_version: str

    @property
    def is_match(self) -> bool:
        return self.decision is MatchOutcome.MATCH

    def reason_text(self) -> str:
        return ", ".join(self.reasons) if self.reasons else "no reasons recorded"


@dataclass(frozen=True)
class StoredJob:
    """A job row as persisted in SQLite."""

    id: int
    job: Job
    status: JobStatus
    first_complete_scan_id: int | None
    last_seen_scan_id: int
    consecutive_misses: int
    match: MatchDecision | None
    closed_at: str | None = None

    @property
    def title(self) -> str:
        return self.job.title

    @property
    def employer(self) -> str:
        return self.job.employer

    @property
    def url(self) -> str:
        return self.job.url


@dataclass(frozen=True)
class AlertCandidate:
    """A role that would be delivered to the user once notifications exist (M2)."""

    job_id: int
    title: str
    employer: str
    location_raw: str
    url: str
    salary_text: str | None
    closing_at: str | None
    decision: MatchDecision


@dataclass(frozen=True)
class SourceCoverage:
    """What one source has contributed, for judging whether it is worth keeping."""

    source_id: str
    open_roles: int = 0
    matches: int = 0
    reviews: int = 0
    matched_in_window: int = 0


@dataclass(frozen=True)
class ScanOutcome:
    """Result of orchestrating one scan: adapter output plus state transitions."""

    scan_id: int
    source_id: str
    status: ScanStatus
    complete: bool
    is_baseline: bool
    records_seen: int
    records_parsed: int
    skipped: tuple[SkipReason, ...]
    new_jobs: int
    updated_jobs: int
    reopened_jobs: int
    closed_jobs: int
    needs_review: int
    candidates: tuple[AlertCandidate, ...]
    error_summary: str | None = None
    notifications_queued: int = 0
    observed_only_jobs: int = 0

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)


@dataclass(frozen=True)
class SourceState:
    """Operational status for one configured source."""

    id: str
    employer: str
    adapter: str
    enabled: bool
    cadence_minutes: int
    baseline_scan_id: int | None
    baseline_completed_at: str | None
    last_success_at: str | None
    last_scan_status: str | None
    last_scan_complete: bool | None
    last_error_at: str | None
    last_error_summary: str | None
    consecutive_failures: int
    next_run_at: str | None
    records_seen: int | None
    records_parsed: int | None
    skipped_count: int | None
    failure_notice_at: str | None = None
    failure_notice_outcome: str | None = None
    failure_notice_attempts: int = 0

    @property
    def has_baseline(self) -> bool:
        return self.baseline_scan_id is not None


@dataclass(frozen=True)
class ScanFacts:
    """The fields trial measurement needs from one recorded scan."""

    source_id: str
    started_at: str
    status: str
    complete: bool


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of one send attempt, as reported by a transport."""

    outcome: DeliveryOutcome
    message_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class PendingNotification:
    """A notification row joined with the role fields needed to format a message."""

    id: int
    chat_id: str
    job_id: int
    attempt_count: int
    title: str
    employer: str
    location_raw: str
    url: str
    salary_text: str | None = None
    closing_at: str | None = None


@dataclass(frozen=True)
class DeliveryReport:
    """What one delivery pass did. Counts are per notification row."""

    sent: int = 0
    failed: int = 0
    ambiguous: int = 0
    retrying: int = 0
    messages: tuple[str, ...] = ()

    @property
    def attempted(self) -> int:
        return self.sent + self.failed + self.ambiguous + self.retrying
