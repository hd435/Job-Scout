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
    ScanFacts,
    ScanResult,
    ScanStatus,
    SourceState,
    StoredJob,
)
from .retention import PruneReport, RetentionPolicy, cutoff

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

_MIGRATION_4 = """
ALTER TABLE sources ADD COLUMN failure_notice_at TEXT NULL;
ALTER TABLE sources ADD COLUMN failure_notice_outcome TEXT NULL;
"""

_MIGRATIONS: tuple[str, ...] = (_MIGRATION_1, _MIGRATION_2, _MIGRATION_3, _MIGRATION_4)


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
            failure_notice_at=row["failure_notice_at"],
            failure_notice_outcome=row["failure_notice_outcome"],
        )

    def list_source_states(self) -> list[SourceState]:
        """Every persisted source, ordered by id. Reporting uses the DB, not the config."""
        ids = [row["id"] for row in self._conn.execute("SELECT id FROM sources ORDER BY id")]
        states = [self.get_source_state(source_id) for source_id in ids]
        return [state for state in states if state is not None]

    def list_scan_facts(self, *, since: str) -> list[ScanFacts]:
        rows = self._conn.execute(
            """
            SELECT source_id, started_at, status, complete
            FROM scans WHERE started_at >= ?
            ORDER BY source_id, started_at
            """,
            (since,),
        )
        return [
            ScanFacts(
                source_id=row["source_id"],
                started_at=row["started_at"],
                status=row["status"],
                complete=bool(row["complete"]),
            )
            for row in rows
        ]

    def count_notifications_by_status(self, *, since: str) -> dict[str, int]:
        rows = self._conn.execute(
            """
            SELECT status, COUNT(*) AS total FROM notifications
            WHERE created_at >= ? GROUP BY status
            """,
            (since,),
        )
        return {row["status"]: int(row["total"]) for row in rows}

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
            # A completed scan ends the failure episode, so a new streak notifies again.
            assignments.append("failure_notice_at = NULL")
            assignments.append("failure_notice_outcome = NULL")
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

    def claim_source_run(
        self,
        source_id: str,
        *,
        expected_next_run_at: str | None,
        next_run_at: str,
        now: str,
    ) -> bool:
        """Claim one scheduled turn by advancing `next_run_at` (compare-and-swap).

        Returns True only for the caller whose read value still matched *and* was itself
        due, so two scheduler processes cannot scan the same source in one cycle (D-063).
        Claiming before the run also means a process killed mid-scan is retried at the next
        cadence rather than immediately, so a crash cannot produce a retry storm (D-059).

        The "was due" half of the guard matters when both processes tick on the same
        instant: the loser reads the value the winner just wrote, which equals its own
        target, so comparing values alone would let both run. It assumes a cadence of at
        least one minute, which configuration validation enforces.
        """
        cursor = self._conn.execute(
            """
            UPDATE sources SET next_run_at = ?
            WHERE id = ? AND next_run_at IS ?
              AND (next_run_at IS NULL OR next_run_at <= ?)
            """,
            (next_run_at, source_id, expected_next_run_at, now),
        )
        return cursor.rowcount == 1

    def defer_source_run(
        self, source_id: str, *, expected_next_run_at: str | None, next_run_at: str
    ) -> bool:
        """Best-effort move of the next run forward after a claim failed (D-063).

        Claiming can fail for a transient reason (a busy write lock, a failing disk) while
        the source stays due. Advancing the schedule is what keeps the loop's sleep
        meaningful: without it a repeatedly failing claim makes every source look due and
        the loop spins. It only rewrites the value this process last read, so it cannot
        overwrite another process's claim.
        """
        cursor = self._conn.execute(
            "UPDATE sources SET next_run_at = ? WHERE id = ? AND next_run_at IS ?",
            (next_run_at, source_id, expected_next_run_at),
        )
        return cursor.rowcount == 1

    def claim_failure_notice(self, source_id: str, *, now: str, threshold: int) -> bool:
        """Reserve the single failure notice for a source's current failure episode.

        Returns True only for the caller that claimed it, so a streak of failures sends
        one notice rather than one per scan (D-010, D-038).
        """
        cursor = self._conn.execute(
            """
            UPDATE sources SET failure_notice_at = ?, failure_notice_outcome = 'pending'
            WHERE id = ? AND failure_notice_at IS NULL AND consecutive_failures >= ?
            """,
            (now, source_id, threshold),
        )
        return cursor.rowcount == 1

    def record_failure_notice_outcome(self, source_id: str, *, outcome: str) -> None:
        self._conn.execute(
            "UPDATE sources SET failure_notice_outcome = ? WHERE id = ?", (outcome, source_id)
        )

    # retention -------------------------------------------------------------

    def prune(self, policy: RetentionPolicy, *, now: str, dry_run: bool = False) -> PruneReport:
        """Delete or strip historical data, bounded, idempotent, and resumable.

        Phases run in dependency order: closed-role content, stale delivery error text,
        notifications of expiring roles (children first), the roles themselves, then
        scans that nothing references any more. Each batch is committed on its own, so an
        interrupted run resumes where it stopped, and `truncated` states plainly whether
        eligible work remains.

        A dry run executes exactly the same statements inside one transaction and rolls
        it back, so its counts predict a single real invocation under the same batch
        limits rather than the whole backlog.
        """
        if not dry_run:
            return self._prune_all(policy, now)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            report = self._prune_all(policy, now)
        finally:
            self._conn.execute("ROLLBACK")
        return PruneReport(
            cleared_job_content=report.cleared_job_content,
            cleared_notification_errors=report.cleared_notification_errors,
            deleted_notifications=report.deleted_notifications,
            deleted_jobs=report.deleted_jobs,
            deleted_scans=report.deleted_scans,
            truncated=report.truncated,
            dry_run=True,
        )

    def _prune_all(self, policy: RetentionPolicy, now: str) -> PruneReport:
        content_cutoff = cutoff(now, policy.closed_content_days)
        job_cutoff = cutoff(now, policy.closed_job_days)
        scan_cutoff = cutoff(now, policy.scan_days)
        error_cutoff = cutoff(now, policy.notification_error_days)

        cleared_content, content_remains = self._prune_job_content(content_cutoff, policy)
        cleared_errors, errors_remain = self._prune_notification_errors(error_cutoff, policy)
        deleted_notifications, deleted_jobs, jobs_remain = self._prune_closed_jobs(
            job_cutoff, policy
        )
        deleted_scans, scans_remain = self._prune_scans(scan_cutoff, policy)

        return PruneReport(
            cleared_job_content=cleared_content,
            cleared_notification_errors=cleared_errors,
            deleted_notifications=deleted_notifications,
            deleted_jobs=deleted_jobs,
            deleted_scans=deleted_scans,
            truncated=content_remains or errors_remain or jobs_remain or scans_remain,
        )

    def _prune_job_content(self, boundary: str, policy: RetentionPolicy) -> tuple[int, bool]:
        where = """
            status = ? AND closed_at IS NOT NULL AND closed_at <= ?
            AND (description IS NOT NULL OR salary_text IS NOT NULL)
        """
        params: tuple[object, ...] = (str(JobStatus.CLOSED), boundary)
        cleared = 0
        for _ in range(policy.max_batches):
            cursor = self._conn.execute(
                f"""
                UPDATE jobs SET description = NULL, salary_text = NULL
                WHERE id IN (SELECT id FROM jobs WHERE {where} ORDER BY id LIMIT ?)
                """,
                (*params, policy.batch_size),
            )
            cleared += cursor.rowcount
            if cursor.rowcount < policy.batch_size:
                break
        return cleared, self._exists("jobs", where, params)

    def _prune_notification_errors(
        self, boundary: str, policy: RetentionPolicy
    ) -> tuple[int, bool]:
        # The error belongs to the most recent attempt, not to the row's creation.
        where = """
            last_error IS NOT NULL
            AND COALESCE(last_attempt_at, created_at) <= ?
        """
        params: tuple[object, ...] = (boundary,)
        cleared = 0
        for _ in range(policy.max_batches):
            cursor = self._conn.execute(
                f"""
                UPDATE notifications SET last_error = NULL
                WHERE id IN (SELECT id FROM notifications WHERE {where} ORDER BY id LIMIT ?)
                """,
                (boundary, policy.batch_size),
            )
            cleared += cursor.rowcount
            if cursor.rowcount < policy.batch_size:
                break
        return cleared, self._exists("notifications", where, params)

    def _prune_closed_jobs(self, boundary: str, policy: RetentionPolicy) -> tuple[int, int, bool]:
        """Delete expiring roles and their notifications, one deterministic batch at a time."""
        select_ids = """
            SELECT id FROM jobs
            WHERE status = ? AND closed_at IS NOT NULL AND closed_at <= ?
            ORDER BY closed_at, id
            LIMIT ?
        """
        params: tuple[object, ...] = (str(JobStatus.CLOSED), boundary, policy.batch_size)
        deleted_notifications = 0
        deleted_jobs = 0

        for _ in range(policy.max_batches):
            ids = [row["id"] for row in self._conn.execute(select_ids, params)]
            if not ids:
                break
            placeholders = ",".join("?" * len(ids))
            cursor = self._conn.execute(
                f"DELETE FROM notifications WHERE job_id IN ({placeholders})", ids
            )
            deleted_notifications += cursor.rowcount

            # Never delete a role whose notification rows still exist.
            orphaned = self._count(
                f"SELECT COUNT(*) AS total FROM notifications WHERE job_id IN ({placeholders})",
                tuple(ids),
            )
            if orphaned:
                return deleted_notifications, deleted_jobs, True

            cursor = self._conn.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", ids)
            deleted_jobs += cursor.rowcount
            if cursor.rowcount < len(ids):
                # A role survived its own batch; stop rather than loop over it.
                return deleted_notifications, deleted_jobs, True

        remains = self._exists(
            "jobs",
            "status = ? AND closed_at IS NOT NULL AND closed_at <= ?",
            (str(JobStatus.CLOSED), boundary),
        )
        return deleted_notifications, deleted_jobs, remains

    def _prune_scans(self, boundary: str, policy: RetentionPolicy) -> tuple[int, bool]:
        where = """
            started_at <= ?
            AND id NOT IN (SELECT first_seen_scan_id FROM jobs WHERE first_seen_scan_id IS NOT NULL)
            AND id NOT IN (
                SELECT first_complete_scan_id FROM jobs WHERE first_complete_scan_id IS NOT NULL
            )
            AND id NOT IN (SELECT last_seen_scan_id FROM jobs WHERE last_seen_scan_id IS NOT NULL)
            AND id NOT IN (SELECT baseline_scan_id FROM sources WHERE baseline_scan_id IS NOT NULL)
        """
        params: tuple[object, ...] = (boundary,)
        deleted = 0
        for _ in range(policy.max_batches):
            cursor = self._conn.execute(
                f"""
                DELETE FROM scans
                WHERE id IN (SELECT id FROM scans WHERE {where} ORDER BY id LIMIT ?)
                """,
                (*params, policy.batch_size),
            )
            deleted += cursor.rowcount
            if cursor.rowcount < policy.batch_size:
                break
        return deleted, self._exists("scans", where, params)

    def _exists(self, table: str, where: str, params: tuple[object, ...]) -> bool:
        row = self._conn.execute(
            f"SELECT 1 AS hit FROM {table} WHERE {where} LIMIT 1", params
        ).fetchone()
        return row is not None

    def _count(self, sql: str, params: tuple[object, ...]) -> int:
        row = self._conn.execute(sql, params).fetchone()
        return int(row["total"])

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
        reopen: bool = False,
    ) -> None:
        """Store refreshed details for a role the database already knows.

        Reopening a closed role is a closure-state change, so it happens only when the
        caller reports a complete scan (D-014). An incomplete scan still refreshes the
        details, but keeps the role's status, `closed_at`, and miss count untouched.
        """
        location = job.location_normalized
        self._conn.execute(
            """
            UPDATE jobs SET
                employer = :employer, title = :title, location_raw = :location_raw,
                location_normalized_json = :location_json, city = :city, country = :country,
                workplace_type = :workplace_type, url = :url, description = :description,
                posted_at = :posted_at, closing_at = :closing_at, salary_text = :salary_text,
                content_hash = :content_hash, source_revision = :source_revision,
                first_complete_scan_id = COALESCE(first_complete_scan_id, :first_complete_scan),
                last_seen_scan_id = :scan_id,
                status = CASE WHEN :reopen = 1 THEN :open_status ELSE status END,
                consecutive_misses = CASE
                    WHEN :reopen = 1 OR status = :open_status THEN 0
                    ELSE consecutive_misses
                END,
                closed_at = CASE WHEN :reopen = 1 THEN NULL ELSE closed_at END,
                match_decision = :match_decision, match_reason = :match_reason,
                rules_version = :rules_version, updated_at = :now
            WHERE id = :job_id
            """,
            {
                "employer": job.employer,
                "title": job.title,
                "location_raw": job.location_raw,
                "location_json": location.to_json(),
                "city": location.city,
                "country": location.country,
                "workplace_type": job.workplace_type,
                "url": job.url,
                "description": job.description,
                "posted_at": job.posted_at.isoformat() if job.posted_at else None,
                "closing_at": job.closing_at.isoformat() if job.closing_at else None,
                "salary_text": job.salary_text,
                "content_hash": job.content_hash,
                "source_revision": job.source_revision,
                "first_complete_scan": first_complete_scan_id,
                "scan_id": scan_id,
                "reopen": int(reopen),
                "open_status": str(JobStatus.OPEN),
                "match_decision": str(decision.decision),
                "match_reason": json.dumps(list(decision.reasons)),
                "rules_version": decision.rules_version,
                "now": now,
                "job_id": job_id,
            },
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
