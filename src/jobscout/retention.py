"""Data-retention policy and prune report (M5.3, D-025).

The policy separates *active* state from *historical* state:

- Active: source rows, open jobs, and the scans that jobs or sources point at. These are
  needed for matching, duplicate prevention, reopening, and closure accounting. They are
  never pruned, however old they are.
- Historical: closed-role content, closed-role identity, scan history nobody references,
  and stale delivery error text. These are safe to purge once they are old enough.

Defaults follow the approved shape in the architecture (30 days for closed content, 12
months for closed identity, 90 days for scan history) rather than inventing new numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class RetentionPolicy:
    """How long historical data survives. Days are measured from a stored timestamp."""

    closed_content_days: int = 30
    closed_job_days: int = 365
    scan_days: int = 90
    notification_error_days: int = 90
    batch_size: int = 500
    max_batches: int = 10

    def __post_init__(self) -> None:
        for name in (
            "closed_content_days",
            "closed_job_days",
            "scan_days",
            "notification_error_days",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("batch_size", "max_batches"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.closed_job_days < self.closed_content_days:
            raise ValueError(
                "closed_job_days must be at least closed_content_days,"
                " otherwise content is purged after its row is deleted"
            )


@dataclass(frozen=True)
class PruneReport:
    """What one prune run did, or would do when `dry_run` is set."""

    cleared_job_content: int = 0
    cleared_notification_errors: int = 0
    deleted_notifications: int = 0
    deleted_jobs: int = 0
    deleted_scans: int = 0
    truncated: bool = False
    dry_run: bool = False

    @property
    def total_changes(self) -> int:
        return (
            self.cleared_job_content
            + self.cleared_notification_errors
            + self.deleted_notifications
            + self.deleted_jobs
            + self.deleted_scans
        )


def cutoff(now: str, days: int) -> str:
    """The stored-timestamp boundary for a retention horizon."""
    moment = datetime.fromisoformat(now.replace("Z", "+00:00"))
    boundary = moment.astimezone(UTC) - timedelta(days=days)
    return boundary.replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = ["PruneReport", "RetentionPolicy", "cutoff"]
