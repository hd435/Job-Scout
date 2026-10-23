"""SQLite persistence: sources, scans, and jobs.

Storage is deliberately thin. It owns SQL and row mapping; baseline, new-role,
update, and closure *decisions* live in `scanner`. There is no ORM
(architecture section 5).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from .config import SourceConfig
from .models import (
    Job,
    JobStatus,
    LocationResult,
    MatchDecision,
    MatchOutcome,
    NotificationStatus,
    PendingNotification,
    ScanResult,
    ScanStatus,
    SourceState,
    StoredJob,
)

_MISSING_LOCATION = LocationResult(qualifies=False, reason="location_missing", ambiguous=True)

_MIGRATION_1 = """
CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    employer TEXT NOT NULL,
    adapter TEXT NOT NULL,
    config_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    cadence_minutes INTEGER NOT NULL,
    baseline_scan_id INTEGER NULL REFERENCES scans(id),
    baseline_completed_at TEXT NULL,
    last_success_at TEXT NULL,
    last_scan_status TEXT NULL,
    last_scan_complete INTEGER NULL,
    last_error_at TEXT NULL,
    last_error_summary TEXT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    next_run_at TEXT NULL
);

CREATE TABLE scans (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL,
    complete INTEGER NOT NULL,
    records_seen INTEGER NOT NULL,
    records_parsed INTEGER NOT NULL,
    source_reported_total INTEGER NULL,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT NULL,
    is_baseline INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    source_job_id TEXT NOT NULL,
    employer TEXT NOT NULL,
    title TEXT NOT NULL,
    location_raw TEXT NOT NULL,
    location_normalized_json TEXT NULL,
    city TEXT NULL,
    country TEXT NULL,
    workplace_type TEXT NULL,
    url TEXT NOT NULL,
    description TEXT NULL,
    posted_at TEXT NULL,
    closing_at TEXT NULL,
    salary_text TEXT NULL,
    content_hash TEXT NOT NULL,
    first_seen_scan_id INTEGER NOT NULL REFERENCES scans(id),
    first_complete_scan_id INTEGER NULL REFERENCES scans(id),
    last_seen_scan_id INTEGER NOT NULL REFERENCES scans(id),
    status TEXT NOT NULL,
    consecutive_misses INTEGER NOT NULL DEFAULT 0,
    closed_at TEXT NULL,
    match_decision TEXT NULL,
    match_reason TEXT NULL,
    rules_version TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_id, source_job_id)
);

CREATE INDEX idx_jobs_source_status ON jobs(source_id, status);
CREATE INDEX idx_jobs_source_job ON jobs(source_id, source_job_id);
CREATE INDEX idx_scans_source_started ON scans(source_id, started_at);
"""

_MIGRATION_2 = """
CREATE TABLE notifications (
    id INTEGER PRIMARY KEY,
    chat_id TEXT NOT NULL,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    telegram_message_id TEXT NULL,
    created_at TEXT NOT NULL,
    last_attempt_at TEXT NULL,
    sent_at TEXT NULL,
    last_error TEXT NULL,
    UNIQUE(chat_id, job_id)
);

CREATE INDEX idx_notifications_status ON notifications(status);
"""

_MIGRATION_3 = """
ALTER TABLE jobs ADD COLUMN source_revision TEXT NULL;
"""

_MIGRATIONS: tuple[str, ...] = (_MIGRATION_1, _MIGRATION_2, _MIGRATION_3)


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        source_id=row["source_id"],
        source_job_id=row["source_job_id"],
        employer=row["employer"],
        title=row["title"],
        location_raw=row["location_raw"],
        location_normalized=LocationResult.from_json(row["location_normalized_json"])
        or _MISSING_LOCATION,
        url=row["url"],
        content_hash=row["content_hash"],
        description=row["description"],
        posted_at=_parse_date(row["posted_at"]),
        closing_at=_parse_date(row["closing_at"]),
        salary_text=row["salary_text"],
        workplace_type=row["workplace_type"],
        source_revision=row["source_revision"],
    )


def _row_to_stored_job(row: sqlite3.Row) -> StoredJob:
    decision = None
    if row["match_decision"]:
        decision = MatchDecision(
            decision=MatchOutcome(row["match_decision"]),
            reasons=tuple(json.loads(row["match_reason"] or "[]")),
            rules_version=row["rules_version"] or "",
        )
    return StoredJob(
        id=row["id"],
        job=_row_to_job(row),
        status=JobStatus(row["status"]),
        first_complete_scan_id=row["first_complete_scan_id"],
        last_seen_scan_id=row["last_seen_scan_id"],
        consecutive_misses=row["consecutive_misses"],
        match=decision,
        closed_at=row["closed_at"],
    )


class Storage:
    """One SQLite connection, one writer (the scan orchestrator)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._conn.row_factory = sqlite3.Row

    @classmethod
    def open(cls, path: Path) -> Storage:
        if path.parent and str(path.parent) not in ("", "."):
            path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        storage = cls(connection)
        storage._apply_migrations()
        return storage

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def _apply_migrations(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        for number, script in enumerate(_MIGRATIONS[version:], start=version + 1):
            self._conn.executescript(script)
            self._conn.execute(f"PRAGMA user_version = {number}")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # sources ---------------------------------------------------------------

    def ensure_source(self, config: SourceConfig, *, now: str | None = None) -> None:
        self._conn.execute(
            """
            INSERT INTO sources (id, employer, adapter, config_json, enabled, cadence_minutes)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                employer = excluded.employer,
                adapter = excluded.adapter,
                config_json = excluded.config_json,
                enabled = excluded.enabled,
                cadence_minutes = excluded.cadence_minutes
            """,
            (
                config.id,
                config.employer,
                config.adapter,
                json.dumps(config.options, sort_keys=True),
                int(config.enabled),
                config.cadence_minutes,
            ),
        )

    def get_source_state(self, source_id: str) -> SourceState | None:
        row = self._conn.execute(
            """
            SELECT s.*, sc.records_seen, sc.records_parsed, sc.skipped_count
            FROM sources s
            LEFT JOIN scans sc ON sc.id = (
                SELECT id FROM scans WHERE source_id = s.id ORDER BY id DESC LIMIT 1
            )
            WHERE s.id = ?
            """,
            (source_id,),
        ).fetchone()
        if row is None:
            return None
        return SourceState(
            id=row["id"],
            employer=row["employer"],
            adapter=row["adapter"],
            enabled=bool(row["enabled"]),
            cadence_minutes=row["cadence_minutes"],
            baseline_scan_id=row["baseline_scan_id"],
            baseline_completed_at=row["baseline_completed_at"],
            last_success_at=row["last_success_at"],
            last_scan_status=row["last_scan_status"],
            last_scan_complete=(
                None if row["last_scan_complete"] is None else bool(row["last_scan_complete"])
            ),
            last_error_at=row["last_error_at"],
            last_error_summary=row["last_error_summary"],
            consecutive_failures=row["consecutive_failures"],
            next_run_at=row["next_run_at"],
            records_seen=row["records_seen"],
            records_parsed=row["records_parsed"],
            skipped_count=row["skipped_count"],
        )

    def update_source_after_scan(
        self,
        source_id: str,
        result: ScanResult,
        *,
        now: str,
        is_baseline: bool,
    ) -> None:
        state = self.get_source_state(source_id)
        consecutive = state.consecutive_failures if state else 0
        if result.status is ScanStatus.FAILED:
            consecutive += 1
        elif result.complete:
            consecutive = 0

        assignments = [
            "last_scan_status = ?",
            "last_scan_complete = ?",
            "consecutive_failures = ?",
        ]
        params: list[object] = [str(result.status), int(result.complete), consecutive]

        if result.complete:
            assignments.append("last_success_at = ?")
            params.append(result.finished_at)
        else:
            assignments.append("last_error_at = ?")
            assignments.append("last_error_summary = ?")
            params.extend([now, result.error_summary or str(result.status)])

        if is_baseline:
            assignments.append("baseline_scan_id = ?")
            assignments.append("baseline_completed_at = ?")
            params.extend([self.latest_scan_id(source_id), now])

        params.append(source_id)
        self._conn.execute(
            f"UPDATE sources SET {', '.join(assignments)} WHERE id = ?",
            params,
        )

    # scans -----------------------------------------------------------------

    def record_scan(self, result: ScanResult, *, is_baseline: bool) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO scans (
                source_id, started_at, finished_at, status, complete,
                records_seen, records_parsed, source_reported_total,
                skipped_count, error_summary, is_baseline
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.source_id,
                result.started_at,
                result.finished_at,
                str(result.status),
                int(result.complete),
                result.records_seen,
                result.records_parsed,
                result.source_reported_total,
                result.skipped_count,
                result.error_summary,
                int(is_baseline),
            ),
        )
        return int(cursor.lastrowid)

    def latest_scan_id(self, source_id: str) -> int | None:
        row = self._conn.execute(
            "SELECT id FROM scans WHERE source_id = ? ORDER BY id DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        return int(row["id"]) if row else None

    def last_complete_scan_records_parsed(self, source_id: str) -> int | None:
        row = self._conn.execute(
            """
            SELECT records_parsed FROM scans
            WHERE source_id = ? AND complete = 1
            ORDER BY id DESC LIMIT 1
            """,
            (source_id,),
        ).fetchone()
        return int(row["records_parsed"]) if row else None

    def recent_scans(self, source_id: str, limit: int = 5) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """
                SELECT * FROM scans WHERE source_id = ? ORDER BY id DESC LIMIT ?
                """,
                (source_id, limit),
            )
        )

    # jobs ------------------------------------------------------------------

    def get_job(self, source_id: str, source_job_id: str) -> StoredJob | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE source_id = ? AND source_job_id = ?",
            (source_id, source_job_id),
        ).fetchone()
        return _row_to_stored_job(row) if row else None

    def insert_job(
        self,
        job: Job,
        *,
        scan_id: int,
        decision: MatchDecision,
        first_complete_scan_id: int | None,
        now: str,
    ) -> int:
        location = job.location_normalized
        cursor = self._conn.execute(
            """
            INSERT INTO jobs (
                source_id, source_job_id, employer, title, location_raw,
                location_normalized_json, city, country, workplace_type, url,
                description, posted_at, closing_at, salary_text, content_hash,
                source_revision,
                first_seen_scan_id, first_complete_scan_id, last_seen_scan_id,
                status, consecutive_misses, match_decision, match_reason,
                rules_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
            """,
            (
                job.source_id,
                job.source_job_id,
                job.employer,
                job.title,
                job.location_raw,
                location.to_json(),
                location.city,
                location.country,
                job.workplace_type,
                job.url,
                job.description,
                job.posted_at.isoformat() if job.posted_at else None,
                job.closing_at.isoformat() if job.closing_at else None,
                job.salary_text,
                job.content_hash,
                job.source_revision,
                scan_id,
                first_complete_scan_id,
                scan_id,
                str(JobStatus.OPEN),
                str(decision.decision),
                json.dumps(list(decision.reasons)),
                decision.rules_version,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)

    def update_job_observation(
        self,
        job_id: int,
        job: Job,
        *,
        scan_id: int,
        decision: MatchDecision,
        first_complete_scan_id: int | None,
        now: str,
    ) -> None:
        location = job.location_normalized
        self._conn.execute(
            """
            UPDATE jobs SET
                employer = ?, title = ?, location_raw = ?, location_normalized_json = ?,
                city = ?, country = ?, workplace_type = ?, url = ?, description = ?,
                posted_at = ?, closing_at = ?, salary_text = ?, content_hash = ?,
                source_revision = ?,
                first_complete_scan_id = COALESCE(first_complete_scan_id, ?),
                last_seen_scan_id = ?, status = ?, consecutive_misses = 0,
                closed_at = NULL, match_decision = ?, match_reason = ?,
                rules_version = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                job.employer,
                job.title,
                job.location_raw,
                location.to_json(),
                location.city,
                location.country,
                job.workplace_type,
                job.url,
                job.description,
                job.posted_at.isoformat() if job.posted_at else None,
                job.closing_at.isoformat() if job.closing_at else None,
                job.salary_text,
                job.content_hash,
                job.source_revision,
                first_complete_scan_id,
                scan_id,
                str(JobStatus.OPEN),
                str(decision.decision),
                json.dumps(list(decision.reasons)),
                decision.rules_version,
                now,
                job_id,
            ),
        )

    def job_revisions(self, source_id: str) -> dict[str, str | None]:
        """Stored revision marker per role, so an adapter can skip unchanged pages."""
        rows = self._conn.execute(
            "SELECT source_job_id, source_revision FROM jobs WHERE source_id = ?",
            (source_id,),
        )
        return {row["source_job_id"]: row["source_revision"] for row in rows}

    def mark_job_observed(self, job_id: int, *, scan_id: int, reopen: bool = False) -> None:
        """Record that a role was still listed, without re-reading its details.

        Stored descriptions, dates, and the match decision are left untouched. Reopening
        a closed role changes closure state, so the caller passes `reopen=True` only when
        the scan was complete (D-014); an incomplete scan leaves the closure untouched and
        only records that the role was seen again.
        """
        if reopen:
            self._conn.execute(
                """
                UPDATE jobs SET
                    last_seen_scan_id = ?, status = ?, consecutive_misses = 0, closed_at = NULL
                WHERE id = ?
                """,
                (scan_id, str(JobStatus.OPEN), job_id),
            )
            return
        self._conn.execute(
            """
            UPDATE jobs SET
                last_seen_scan_id = ?,
                consecutive_misses = CASE WHEN status = ? THEN 0 ELSE consecutive_misses END
            WHERE id = ?
            """,
            (scan_id, str(JobStatus.OPEN), job_id),
        )

    def list_open_jobs(self, source_id: str) -> list[StoredJob]:
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE source_id = ? AND status = ? ORDER BY id",
            (source_id, str(JobStatus.OPEN)),
        )
        return [_row_to_stored_job(row) for row in rows]

    def list_jobs_by_decision(
        self, source_id: str, decision: MatchOutcome, *, open_only: bool = True
    ) -> list[StoredJob]:
        query = "SELECT * FROM jobs WHERE source_id = ? AND match_decision = ?"
        params: list[object] = [source_id, str(decision)]
        if open_only:
            query += " AND status = ?"
            params.append(str(JobStatus.OPEN))
        query += " ORDER BY employer, title"
        return [_row_to_stored_job(row) for row in self._conn.execute(query, params)]

    def count_by_decision(self, source_id: str, decision: MatchOutcome) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS total FROM jobs
            WHERE source_id = ? AND match_decision = ? AND status = ?
            """,
            (source_id, str(decision), str(JobStatus.OPEN)),
        ).fetchone()
        return int(row["total"])

    def register_miss(self, job_id: int, *, now: str) -> int:
        self._conn.execute(
            """
            UPDATE jobs
            SET consecutive_misses = consecutive_misses + 1, updated_at = ?
            WHERE id = ?
            """,
            (now, job_id),
        )
        row = self._conn.execute(
            "SELECT consecutive_misses FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return int(row["consecutive_misses"])

    def close_job(self, job_id: int, *, now: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET status = ?, closed_at = ?, updated_at = ? WHERE id = ?",
            (str(JobStatus.CLOSED), now, now, job_id),
        )

    def reopen_job(self, job_id: int, *, now: str) -> None:
        self._conn.execute(
            """
            UPDATE jobs SET status = ?, consecutive_misses = 0, closed_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (str(JobStatus.OPEN), now, job_id),
        )

    def count_scans(self, source_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS total FROM scans WHERE source_id = ?", (source_id,)
        ).fetchone()
        return int(row["total"])

    # notifications ---------------------------------------------------------

    def create_pending_notification(self, chat_id: str, job_id: int, *, now: str) -> int | None:
        """Queue one alert per `(chat_id, job_id)`. Returns None when already queued."""
        cursor = self._conn.execute(
            """
            INSERT INTO notifications (chat_id, job_id, status, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, job_id) DO NOTHING
            """,
            (chat_id, job_id, str(NotificationStatus.PENDING), now),
        )
        if cursor.rowcount == 0:
            return None
        return int(cursor.lastrowid)

    def list_pending_notifications(
        self, chat_id: str, *, limit: int = 50
    ) -> list[PendingNotification]:
        rows = self._conn.execute(
            """
            SELECT n.id, n.chat_id, n.job_id, n.attempt_count,
                   j.title, j.employer, j.location_raw, j.url,
                   j.salary_text, j.closing_at
            FROM notifications n
            JOIN jobs j ON j.id = n.job_id
            WHERE n.chat_id = ? AND n.status = ?
            ORDER BY n.id
            LIMIT ?
            """,
            (chat_id, str(NotificationStatus.PENDING), limit),
        )
        return [
            PendingNotification(
                id=row["id"],
                chat_id=row["chat_id"],
                job_id=row["job_id"],
                attempt_count=row["attempt_count"],
                title=row["title"],
                employer=row["employer"],
                location_raw=row["location_raw"],
                url=row["url"],
                salary_text=row["salary_text"],
                closing_at=row["closing_at"],
            )
            for row in rows
        ]

    def record_notification_sent(
        self, notification_id: int, *, message_id: str | None, now: str
    ) -> None:
        self._conn.execute(
            """
            UPDATE notifications
            SET status = ?, attempt_count = attempt_count + 1,
                telegram_message_id = ?, last_attempt_at = ?, sent_at = ?,
                last_error = NULL
            WHERE id = ?
            """,
            (str(NotificationStatus.SENT), message_id, now, now, notification_id),
        )

    def record_notification_retry(self, notification_id: int, *, error: str, now: str) -> None:
        """Transient failure: stay `pending` so a later delivery pass retries."""
        self._conn.execute(
            """
            UPDATE notifications
            SET attempt_count = attempt_count + 1, last_attempt_at = ?, last_error = ?
            WHERE id = ?
            """,
            (now, error, notification_id),
        )

    def record_notification_failed(self, notification_id: int, *, error: str, now: str) -> None:
        self._conn.execute(
            """
            UPDATE notifications
            SET status = ?, attempt_count = attempt_count + 1,
                last_attempt_at = ?, last_error = ?
            WHERE id = ?
            """,
            (str(NotificationStatus.FAILED), now, error, notification_id),
        )

    def record_notification_ambiguous(self, notification_id: int, *, error: str, now: str) -> None:
        self._conn.execute(
            """
            UPDATE notifications
            SET status = ?, attempt_count = attempt_count + 1,
                last_attempt_at = ?, last_error = ?
            WHERE id = ?
            """,
            (str(NotificationStatus.AMBIGUOUS), now, error, notification_id),
        )

    def notification_status_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS total FROM notifications GROUP BY status"
        )
        return {row["status"]: int(row["total"]) for row in rows}

    def notification_for_job(self, chat_id: str, job_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM notifications WHERE chat_id = ? AND job_id = ?",
            (chat_id, job_id),
        ).fetchone()

    def last_notification_error(self) -> tuple[str, str] | None:
        row = self._conn.execute(
            """
            SELECT last_attempt_at, last_error FROM notifications
            WHERE last_error IS NOT NULL
            ORDER BY last_attempt_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return (row["last_attempt_at"] or "", row["last_error"] or "")
