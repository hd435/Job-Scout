"""Per-user state for the hosted service (D-091…D-096).

Every method takes an explicit `user_id`: there is deliberately no accessor that returns "all
preferences" or "everyone's alerts", so a read that crosses tenants has to be written on purpose
rather than by accident. Deletion is explicit per user, which is what `/delete` relies on.

Nothing here scans, matches or sends. It owns rows; `notify` decides who is told what, and `bot`
turns that into chat messages.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..storage import Storage, utc_now_iso


@dataclass(frozen=True)
class ServiceUser:
    id: int
    telegram_user_id: int
    chat_id: str
    created_at: str
    paused: bool
    watermark: int

    @property
    def active(self) -> bool:
        return not self.paused


@dataclass(frozen=True)
class UserAlert:
    """One role a user was actually told about."""

    job_id: int
    title: str
    employer: str
    location_raw: str
    url: str
    status: str
    created_at: str
    reason: str | None


def _row_to_user(row: Any) -> ServiceUser:
    return ServiceUser(
        id=int(row["id"]),
        telegram_user_id=int(row["telegram_user_id"]),
        chat_id=str(row["chat_id"]),
        created_at=str(row["created_at"]),
        paused=bool(row["paused"]),
        watermark=int(row["watermark"]),
    )


class ServiceStore:
    """The service's view of storage, over the same SQLite file the CLI uses."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    @property
    def connection(self) -> Any:
        return self._storage.connection

    @property
    def storage(self) -> Storage:
        """The shared storage, for the scanner and the notifier (not for tenant reads)."""
        return self._storage

    @classmethod
    def open(cls, path: Path) -> ServiceStore:
        return cls(Storage.open(path))

    def close(self) -> None:
        self._storage.close()

    def __enter__(self) -> ServiceStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # users -----------------------------------------------------------------

    def ensure_user(
        self, telegram_user_id: int, chat_id: str, *, now: str | None = None
    ) -> ServiceUser:
        """Create the user on first contact; keep the newest chat id afterwards.

        A private chat id equals the user id today, but the two are stored separately because a
        migrated Telegram account can change one without the other.
        """
        moment = now or utc_now_iso()
        self.connection.execute(
            """
            INSERT INTO service_users (telegram_user_id, chat_id, created_at, watermark)
            VALUES (?, ?, ?, (SELECT coalesce(max(id), 0) FROM jobs))
            ON CONFLICT(telegram_user_id) DO UPDATE SET chat_id = excluded.chat_id
            """,
            (telegram_user_id, str(chat_id), moment),
        )
        user = self.get_user(telegram_user_id)
        assert user is not None
        return user

    def get_user(self, telegram_user_id: int) -> ServiceUser | None:
        row = self.connection.execute(
            "SELECT * FROM service_users WHERE telegram_user_id = ?", (telegram_user_id,)
        ).fetchone()
        return None if row is None else _row_to_user(row)

    def get_user_by_id(self, user_id: int) -> ServiceUser | None:
        row = self.connection.execute(
            "SELECT * FROM service_users WHERE id = ?", (user_id,)
        ).fetchone()
        return None if row is None else _row_to_user(row)

    def active_users(self) -> tuple[ServiceUser, ...]:
        rows = self.connection.execute(
            "SELECT * FROM service_users WHERE paused = 0 AND deleted_at IS NULL ORDER BY id"
        ).fetchall()
        return tuple(_row_to_user(row) for row in rows)

    def count_users(self) -> int:
        row = self.connection.execute(
            "SELECT count(*) FROM service_users WHERE deleted_at IS NULL"
        ).fetchone()
        return int(row[0])

    def set_paused(self, user_id: int, paused: bool) -> None:
        self.connection.execute(
            "UPDATE service_users SET paused = ? WHERE id = ?", (1 if paused else 0, user_id)
        )

    def delete_user(self, user_id: int) -> None:
        """Remove everything belonging to one user, and nothing else."""
        user = self.get_user_by_id(user_id)
        if user is None:
            return
        with self._storage.transaction() as conn:
            conn.execute("DELETE FROM notifications WHERE chat_id = ?", (user.chat_id,))
            conn.execute("DELETE FROM service_preferences WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM service_subscriptions WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM service_sessions WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM service_users WHERE id = ?", (user_id,))

    # preferences -----------------------------------------------------------

    def get_preferences(self, user_id: int) -> dict | None:
        row = self.connection.execute(
            "SELECT settings_json FROM service_preferences WHERE user_id = ?", (user_id,)
        ).fetchone()
        return None if row is None else json.loads(row["settings_json"])

    def get_fingerprint(self, user_id: int) -> str | None:
        row = self.connection.execute(
            "SELECT fingerprint FROM service_preferences WHERE user_id = ?", (user_id,)
        ).fetchone()
        return None if row is None else str(row["fingerprint"])

    def save_preferences(
        self, user_id: int, settings: dict, *, fingerprint: str, now: str | None = None
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO service_preferences (user_id, settings_json, fingerprint, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                settings_json = excluded.settings_json,
                fingerprint = excluded.fingerprint,
                updated_at = excluded.updated_at
            """,
            (user_id, json.dumps(settings, sort_keys=True), fingerprint, now or utc_now_iso()),
        )

    # subscriptions ---------------------------------------------------------

    def list_subscriptions(self, user_id: int) -> tuple[str, ...]:
        rows = self.connection.execute(
            "SELECT source_id FROM service_subscriptions WHERE user_id = ? ORDER BY source_id",
            (user_id,),
        ).fetchall()
        return tuple(str(row["source_id"]) for row in rows)

    def set_subscriptions(
        self, user_id: int, source_ids: tuple[str, ...], *, now: str | None = None
    ) -> None:
        """Replace the user's subscriptions. Sources kept in both keep their history."""
        moment = now or utc_now_iso()
        with self._storage.transaction() as conn:
            if source_ids:
                placeholders = ",".join("?" * len(source_ids))
                conn.execute(
                    "DELETE FROM service_subscriptions WHERE user_id = ?"
                    f" AND source_id NOT IN ({placeholders})",
                    (user_id, *source_ids),
                )
            else:
                conn.execute("DELETE FROM service_subscriptions WHERE user_id = ?", (user_id,))
            for source_id in source_ids:
                conn.execute(
                    """
                    INSERT INTO service_subscriptions (user_id, source_id, subscribed_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id, source_id) DO NOTHING
                    """,
                    (user_id, source_id, moment),
                )

    def set_watermark(self, user_id: int, watermark: int) -> None:
        """Move the watermark forward only: history is never re-announced (D-095)."""
        self.connection.execute(
            "UPDATE service_users SET watermark = ? WHERE id = ? AND watermark < ?",
            (watermark, user_id, watermark),
        )

    def subscribers_by_source(self) -> dict[str, tuple[int, ...]]:
        """Which users follow which employer — the input to the per-user notify pass."""
        found: dict[str, list[int]] = {}
        for row in self.connection.execute(
            """
            SELECT s.source_id, s.user_id FROM service_subscriptions s
            JOIN service_users u ON u.id = s.user_id
            WHERE u.paused = 0 AND u.deleted_at IS NULL
            ORDER BY s.source_id, s.user_id
            """
        ):
            found.setdefault(str(row["source_id"]), []).append(int(row["user_id"]))
        return {source: tuple(users) for source, users in found.items()}

    # wizard session --------------------------------------------------------

    def get_session(self, user_id: int) -> tuple[str, dict] | None:
        row = self.connection.execute(
            "SELECT step, draft_json FROM service_sessions WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        return str(row["step"]), json.loads(row["draft_json"])

    def save_session(self, user_id: int, step: str, draft: dict, *, now: str | None = None) -> None:
        self.connection.execute(
            """
            INSERT INTO service_sessions (user_id, step, draft_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                step = excluded.step,
                draft_json = excluded.draft_json,
                updated_at = excluded.updated_at
            """,
            (user_id, step, json.dumps(draft, sort_keys=True), now or utc_now_iso()),
        )

    def clear_session(self, user_id: int) -> None:
        self.connection.execute("DELETE FROM service_sessions WHERE user_id = ?", (user_id,))

    # notifications ---------------------------------------------------------

    def record_alert(
        self, user_id: int, chat_id: str, job_id: int, *, reason: str, now: str | None = None
    ) -> bool:
        """Queue one alert for one user. False when this user was already told about the role."""
        cursor = self.connection.execute(
            """
            INSERT INTO notifications (chat_id, job_id, status, created_at, reason, user_id)
            VALUES (?, ?, 'pending', ?, ?, ?)
            ON CONFLICT(chat_id, job_id) DO NOTHING
            """,
            (chat_id, job_id, now or utc_now_iso(), reason, user_id),
        )
        return cursor.rowcount == 1

    def list_alerts(self, user_id: int, *, limit: int = 10) -> tuple[UserAlert, ...]:
        rows = self.connection.execute(
            """
            SELECT n.job_id, n.status, n.created_at, n.reason,
                   j.title, j.employer, j.location_raw, j.url
            FROM notifications n
            JOIN service_users u ON u.chat_id = n.chat_id
            JOIN jobs j ON j.id = n.job_id
            WHERE u.id = ?
            ORDER BY n.id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return tuple(
            UserAlert(
                job_id=int(row["job_id"]),
                title=str(row["title"]),
                employer=str(row["employer"]),
                location_raw=str(row["location_raw"]),
                url=str(row["url"]),
                status=str(row["status"]),
                created_at=str(row["created_at"]),
                reason=row["reason"],
            )
            for row in rows
        )

    def count_alerts(self, user_id: int) -> dict[str, int]:
        rows = self.connection.execute(
            """
            SELECT n.status, count(*) AS n
            FROM notifications n
            JOIN service_users u ON u.chat_id = n.chat_id
            WHERE u.id = ?
            GROUP BY n.status
            """,
            (user_id,),
        ).fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}
