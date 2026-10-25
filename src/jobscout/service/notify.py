"""Per-user evaluation over a shared scan (D-092, D-095).

Scanning stays global: one request per employer per cadence, whatever the subscriber count.
This module is the second half — after a scan, decide for each user, using their own rules, which
of the newly appearing roles they should hear about.

The rules that matter:

- a role only counts once a **complete** scan has seen it (`first_complete_scan_id`), exactly as in
  the CLI path, so a partial scan can never create an alert;
- candidates are roles with `id > watermark`, so changing preferences cannot re-announce history;
- `needs_review` is never sent to a user: review exists to tune the operator's rules, and
  forwarding it would be noise dressed as an alert;
- the watermark always advances, matched or not, so the next pass does not re-read the same rows.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from ..matching import DEFAULT_RULES, MatchingRules, match_job
from ..models import MatchOutcome
from ..storage import Storage
from .store import ServiceStore, ServiceUser

#: Roles per digest page. Five fits a phone screen with the link for every role visible.
DIGEST_PAGE_SIZE = 5


@dataclass(frozen=True)
class DigestEntry:
    """One already-open role the user could apply to today."""

    job_id: int
    title: str
    employer: str
    location: str
    url: str
    posted_at: date | None


@dataclass(frozen=True)
class DigestPage:
    """A slice of the full match set, plus the facts needed to describe it truthfully."""

    entries: tuple[DigestEntry, ...]
    total: int
    page: int
    page_size: int

    @classmethod
    def paginate(cls, matches: Sequence[DigestEntry], *, page: int, page_size: int) -> DigestPage:
        """Clamp an out-of-range page instead of failing: buttons go stale."""
        size = max(1, page_size)
        total = len(matches)
        pages = max(1, math.ceil(total / size))
        current = min(max(1, page), pages)
        start = (current - 1) * size
        return cls(
            entries=tuple(matches[start : start + size]),
            total=total,
            page=current,
            page_size=size,
        )

    @property
    def pages(self) -> int:
        return max(1, math.ceil(self.total / self.page_size))

    @property
    def first_index(self) -> int:
        return 0 if self.total == 0 else (self.page - 1) * self.page_size + 1

    @property
    def last_index(self) -> int:
        return (self.page - 1) * self.page_size + len(self.entries)

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages


@dataclass(frozen=True)
class UserAlertReport:
    user_id: int
    candidates: int
    matched: int
    queued: int
    review: int

    @property
    def quiet(self) -> bool:
        return self.queued == 0


def user_rules(settings: Mapping[str, object] | None) -> MatchingRules:
    """Build a user's rules from their stored settings, using the CLI's own parser.

    Reusing `parse_preferences` → `build_rules` means the bot cannot express a scope the config
    file could not, and both modes decide identically for identical settings.
    """
    if not settings:
        return DEFAULT_RULES
    from ..config import ConfigError, parse_preferences
    from ..matching import build_rules

    try:
        preferences = parse_preferences(dict(settings))
        return build_rules(**preferences) if preferences else DEFAULT_RULES
    except (ConfigError, ValueError) as exc:  # a bad stored scope must not stop everyone
        raise ValueError(f"invalid stored preferences: {exc}") from exc


def notify_pass(
    storage: Storage,
    store: ServiceStore,
    *,
    now: str | None = None,
    limit_per_user: int = 500,
    rules_provider: Callable[[Mapping[str, object] | None], MatchingRules] = user_rules,
) -> tuple[UserAlertReport, ...]:
    """Queue alerts for every active subscriber, each judged by their own rules."""
    reports: list[UserAlertReport] = []
    for user in store.active_users():
        reports.append(
            _notify_one(
                storage,
                store,
                user,
                now=now,
                limit=limit_per_user,
                rules_provider=rules_provider,
            )
        )
    return tuple(reports)


def _notify_one(
    storage: Storage,
    store: ServiceStore,
    user: ServiceUser,
    *,
    now: str | None,
    limit: int,
    rules_provider: Callable[[Mapping[str, object] | None], MatchingRules],
) -> UserAlertReport:
    sources = store.list_subscriptions(user.id)
    if not sources:
        return UserAlertReport(user.id, 0, 0, 0, 0)

    try:
        rules = rules_provider(store.get_preferences(user.id))
    except ValueError:
        # A corrupt scope is a per-user problem: skip this user, keep everyone else working.
        return UserAlertReport(user.id, 0, 0, 0, 0)

    candidates = storage.list_new_jobs(sources, after_id=user.watermark, limit=limit)
    if not candidates:
        return UserAlertReport(user.id, 0, 0, 0, 0)

    matched = review = queued = 0
    highest = user.watermark
    for stored in candidates:
        highest = max(highest, stored.id)
        decision = match_job(stored.job, rules)
        if decision.decision is MatchOutcome.NEEDS_REVIEW:
            review += 1
            continue
        if decision.decision is not MatchOutcome.MATCH:
            continue
        matched += 1
        if store.record_alert(
            user.id,
            user.chat_id,
            stored.id,
            reason=", ".join(decision.reasons),
            now=now,
        ):
            queued += 1

    store.set_watermark(user.id, highest)
    return UserAlertReport(user.id, len(candidates), matched, queued, review)


def digest_entries(
    storage: Storage,
    store: ServiceStore,
    user: ServiceUser,
    *,
    rules_provider: Callable[[Mapping[str, object] | None], MatchingRules] = user_rules,
) -> tuple[DigestEntry, ...]:
    """Every already-open role that matches this user's own scope, newest first.

    Every eligible role is evaluated — the subscriptions decide the source set and the user's
    rules decide the matches — so the stated total is the real total, not a page or a sample.
    `NEEDS_REVIEW` is never counted: review exists to tune rules, not to be browsed as a match.

    Read-only: it creates no notification rows, moves no watermark and changes no alerting state
    (D-095).
    """
    sources = store.list_subscriptions(user.id)
    if not sources:
        return ()
    try:
        rules = rules_provider(store.get_preferences(user.id))
    except ValueError:
        return ()

    matches: list[DigestEntry] = []
    for stored in storage.iter_open_jobs_for(sources):
        decision = match_job(stored.job, rules)
        if decision.decision is not MatchOutcome.MATCH:
            continue
        matches.append(
            DigestEntry(
                job_id=stored.id,
                title=stored.job.title,
                employer=stored.job.employer,
                location=stored.job.location_raw,
                url=stored.job.url,
                posted_at=stored.job.posted_at,
            )
        )

    # Posting date first where the source provides one, then first-observed order (job id) as a
    # stable tie-break. This is a real ordering, not "whatever the database returned".
    matches.sort(key=lambda entry: (entry.posted_at or date.min, entry.job_id), reverse=True)
    return tuple(matches)


def digest_page(
    storage: Storage,
    store: ServiceStore,
    user: ServiceUser,
    *,
    page: int = 1,
    page_size: int = DIGEST_PAGE_SIZE,
    rules_provider: Callable[[Mapping[str, object] | None], MatchingRules] = user_rules,
) -> DigestPage:
    """One page of `digest_entries`, for callers that do not cache the full set themselves."""
    entries = digest_entries(storage, store, user, rules_provider=rules_provider)
    return DigestPage.paginate(entries, page=page, page_size=page_size)


def format_digest_page(page: DigestPage) -> str:
    """The message body for one digest page. Plain text on purpose: no parse mode to escape."""
    if page.total == 0:
        return (
            "Nothing matches your current filters right now. You can widen them with /settings, "
            "and I will message you when something new matches."
        )
    lines = [
        "Your current job matches",
        f"{page.total} matching open role{'' if page.total == 1 else 's'}"
        " across your selected employers.",
        f"Showing {page.first_index}–{page.last_index} of {page.total}"
        " · Already-open roles, not new alerts.",
        "",
    ]
    for offset, entry in enumerate(page.entries, start=page.first_index):
        lines.append(f"{offset}. {entry.title} — {entry.employer}")
        where = entry.location or "location not stated"
        if entry.posted_at is not None:
            where += f" · posted {entry.posted_at.strftime('%d %b %Y').lstrip('0')}"
        lines.append(f"    {where}")
        lines.append(f"    {entry.url}")
        lines.append("")
    if page.pages > 1:
        lines.append(f"Page {page.page} of {page.pages}.")
    return "\n".join(lines).rstrip()


__all__ = [
    "DIGEST_PAGE_SIZE",
    "DigestEntry",
    "DigestPage",
    "UserAlertReport",
    "digest_entries",
    "digest_page",
    "format_digest_page",
    "notify_pass",
    "user_rules",
]
