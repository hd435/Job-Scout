"""Consistent SQLite backups for a running service.

`sqlite3.Connection.backup` copies a live database correctly, including pages still in the
write-ahead log, without stopping the bot — which matters because the deployment has no quiet
period: it polls Telegram and scans whenever a source is due.

The result is verified (`PRAGMA integrity_check` plus row counts) before it is reported, because
an unverified backup is a hope. Restoring is a file copy done while the service is stopped; see
`docs/runbook.md` section 9.3.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

#: Tables whose row counts make a backup auditable at a glance.
COUNTED_TABLES = ("jobs", "scans", "sources", "service_users", "notifications")


@dataclass(frozen=True)
class BackupResult:
    target: Path
    bytes: int
    integrity: str
    counts: dict[str, int]

    @property
    def ok(self) -> bool:
        return self.integrity == "ok"


def default_backup_path(database: Path, *, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return database.parent / "backups" / f"{database.stem}-{stamp}.db"


def backup_database(source: Path, target: Path) -> BackupResult:
    """Copy `source` to `target` and verify the copy. Raises on an unusable source."""
    if not source.exists():
        raise FileNotFoundError(f"database not found: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)

    live = sqlite3.connect(source)
    try:
        live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        copy = sqlite3.connect(target)
        try:
            with copy:
                live.backup(copy)
            integrity = str(copy.execute("PRAGMA integrity_check").fetchone()[0])
            counts = {
                table: int(copy.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
                for table in COUNTED_TABLES
            }
        finally:
            copy.close()
    finally:
        live.close()

    return BackupResult(
        target=target, bytes=target.stat().st_size, integrity=integrity, counts=counts
    )


__all__ = ["BackupResult", "COUNTED_TABLES", "backup_database", "default_backup_path"]
