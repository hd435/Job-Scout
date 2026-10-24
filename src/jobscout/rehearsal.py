"""Rehearse the whole alert journey against a scratch database (D-089).

Every other check proves one link: `validate` proves a payload shape, `notify-test` proves the
Telegram transport, the test suite proves the scanner and the notifier against fakes. None of
them shows the chain an operator actually depends on — a new matching role becoming a recorded,
delivered notification.

This module runs that chain with three synthetic Lever records in a throwaway database:

1. first complete scan → baseline, silent by design;
2. second scan with one additional matching role → detected as new and queued;
3. delivery through the real transport → recorded as sent.

Nothing here touches the live database, the live sources, the catalogue, or the operator's
alert history: the scratch database is its own file and the records are obviously synthetic
("Job-Scout Rehearsal"). What it cannot prove is that a real employer will post a role that
matches a real preference — only a genuine new role does that, and the caller must not claim
otherwise.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AppConfig, SourceConfig
from .http import FixtureHttpClient
from .matching import DEFAULT_RULES, MatchingRules
from .models import DeliveryReport, MatchOutcome, ScanOutcome
from .notifier import Notifier, format_message
from .scanner import run_scan
from .sources import get_adapter
from .storage import Storage, utc_now_iso

REHEARSAL_SOURCE_ID = "rehearsal"
REHEARSAL_EMPLOYER = "Job-Scout Rehearsal (synthetic)"
# A dry run still queues a notification, so the message can be shown; it is never sent.
DRY_RUN_CHAT_ID = "rehearsal-local"

_CREATED_AT = 1759276800000  # fixed, so two runs of the rehearsal are comparable


def _record(
    *, role_id: str, title: str, location: str, commitment: str, text: str
) -> dict[str, Any]:
    return {
        "id": role_id,
        "text": title,
        "categories": {
            "team": "Rehearsal",
            "location": location,
            "allLocations": [location],
            "commitment": commitment,
        },
        "workplaceType": "onsite",
        "descriptionPlain": text,
        "createdAt": _CREATED_AT,
        "hostedUrl": f"https://example.invalid/rehearsal/{role_id}",
    }


def baseline_payload() -> list[dict[str, Any]]:
    """Two roles: one that should match an early-career London scope, one that should not."""
    return [
        _record(
            role_id="rehearsal-graduate-swe",
            title="Graduate Software Engineer",
            location="London, United Kingdom",
            commitment="Full-time",
            text="Graduate scheme for software engineers. No prior experience required.",
        ),
        _record(
            role_id="rehearsal-staff-swe",
            title="Staff Software Engineer",
            location="London, United Kingdom",
            commitment="Full-time",
            text="Eight or more years of experience leading platform work.",
        ),
    ]


def follow_up_payload() -> list[dict[str, Any]]:
    """The same board one scan later, with one genuinely new matching role."""
    return [
        *baseline_payload(),
        _record(
            role_id="rehearsal-graduate-data",
            title="Graduate Data Analyst",
            location="London, United Kingdom",
            commitment="Full-time",
            text="Graduate analyst role in the data team. No prior experience required.",
        ),
    ]


@dataclass(frozen=True)
class RehearsalResult:
    """What the rehearsal observed, so the caller can report it without re-deriving anything."""

    database: Path
    baseline: ScanOutcome
    follow_up: ScanOutcome
    delivery: DeliveryReport | None
    preview: tuple[str, ...]
    decisions: tuple[tuple[str, str, str], ...]
    notification_counts: dict[str, int]
    sent_message_id: str | None
    delivery_error: str | None
    scratch_kept: bool

    @property
    def baseline_was_silent(self) -> bool:
        return self.baseline.notifications_queued == 0

    @property
    def detected_new_role(self) -> bool:
        return self.follow_up.notifications_queued == 1

    @property
    def delivered(self) -> bool:
        return bool(self.delivery and self.delivery.sent == 1)

    @property
    def recorded(self) -> bool:
        return self.notification_counts.get("sent", 0) == 1

    @property
    def complete(self) -> bool:
        """True when the whole chain ran: silent baseline, detection, and a sent record."""
        return (
            self.baseline_was_silent
            and self.detected_new_role
            and (self.delivered or self.delivery is None)
            and (self.recorded or self.delivery is None)
        )


def run_rehearsal(
    app: AppConfig,
    *,
    scratch_dir: Path,
    chat_id: str | None = None,
    notifier: Notifier | None = None,
    keep: bool = False,
    clock: Callable[[], str] = utc_now_iso,
    rules: MatchingRules | None = None,
) -> RehearsalResult:
    """Run the three-step journey in a scratch database and report what was observed.

    `chat_id` is where a real rehearsal delivers; without one (a dry run) the notification is
    queued against a local placeholder so the message can still be shown. `notifier` is the
    real notifier (so the real transport and redaction apply), or None for a dry run. The
    operator's own matching rules are used, so the rehearsal also says something true about
    their scope: if their preferences exclude a London graduate engineering role, it reports
    that instead of pretending the pipeline failed.
    """
    scratch_dir.mkdir(parents=True, exist_ok=True)
    database = scratch_dir / "rehearsal.db"
    for leftover in (database, *(Path(f"{database}{suffix}") for suffix in ("-wal", "-shm"))):
        leftover.unlink(missing_ok=True)

    payload_dir = scratch_dir / "payloads"
    payload_dir.mkdir(exist_ok=True)
    baseline_path = payload_dir / "baseline.json"
    follow_up_path = payload_dir / "follow-up.json"
    baseline_path.write_text(json.dumps(baseline_payload(), indent=2), encoding="utf-8")
    follow_up_path.write_text(json.dumps(follow_up_payload(), indent=2), encoding="utf-8")

    source = SourceConfig(
        id=REHEARSAL_SOURCE_ID,
        employer=REHEARSAL_EMPLOYER,
        adapter="lever",
        options={"site": REHEARSAL_SOURCE_ID},
        access_note="synthetic records served from local files; no network request is made",
    )
    adapter = get_adapter("lever")
    effective_rules = rules or app.matching or DEFAULT_RULES
    delivery_chat = chat_id or DRY_RUN_CHAT_ID
    with Storage.open(database) as storage:
        baseline = run_scan(
            storage,
            adapter,
            source,
            FixtureHttpClient(baseline_path),
            notify_chat_id=delivery_chat,
            rules=effective_rules,
            clock=clock,
        )
        follow_up = run_scan(
            storage,
            adapter,
            source,
            FixtureHttpClient(follow_up_path),
            notify_chat_id=delivery_chat,
            rules=effective_rules,
            clock=clock,
        )
        preview = tuple(
            format_message(notification)
            for notification in storage.list_pending_notifications(delivery_chat)
        )
        delivery = None
        if notifier is not None:
            delivery = notifier.deliver_pending(storage, delivery_chat)
        counts = storage.notification_counts(delivery_chat)
        decisions = tuple(
            (stored.job.title, str(stored.match.decision), ", ".join(stored.match.reasons))
            for decision in (
                MatchOutcome.MATCH,
                MatchOutcome.NEEDS_REVIEW,
                MatchOutcome.NO_MATCH,
            )
            for stored in storage.list_jobs_by_decision(source.id, decision)
            if stored.match is not None
        )
        message_id = _first_message_id(storage)
        error = _first_delivery_error(storage)

    result = RehearsalResult(
        database=database,
        baseline=baseline,
        follow_up=follow_up,
        delivery=delivery,
        preview=preview,
        decisions=decisions,
        notification_counts=counts,
        sent_message_id=message_id,
        delivery_error=error,
        scratch_kept=keep or not _chain_ran(baseline, follow_up, delivery, counts),
    )
    # Keep the scratch database whenever the journey did not complete: it holds the delivery
    # error and the payloads, which are exactly what a diagnosis needs.
    if not result.scratch_kept:
        shutil.rmtree(scratch_dir, ignore_errors=True)
    return result


def _chain_ran(
    baseline: ScanOutcome,
    follow_up: ScanOutcome,
    delivery: DeliveryReport | None,
    counts: dict[str, int],
) -> bool:
    if baseline.notifications_queued != 0 or follow_up.notifications_queued != 1:
        return False
    if delivery is None:
        return True
    return delivery.sent == 1 and counts.get("sent", 0) == 1


def _first_delivery_error(storage: Storage) -> str | None:
    row = storage.connection.execute(
        "SELECT last_error FROM notifications WHERE last_error IS NOT NULL ORDER BY id LIMIT 1"
    ).fetchone()
    return None if row is None else str(row[0])


def _first_message_id(storage: Storage) -> str | None:
    row = storage.connection.execute(
        "SELECT telegram_message_id FROM notifications WHERE telegram_message_id IS NOT NULL"
        " ORDER BY id LIMIT 1"
    ).fetchone()
    return None if row is None else str(row[0])


__all__ = [
    "DRY_RUN_CHAT_ID",
    "REHEARSAL_EMPLOYER",
    "REHEARSAL_SOURCE_ID",
    "RehearsalResult",
    "baseline_payload",
    "follow_up_payload",
    "run_rehearsal",
]
