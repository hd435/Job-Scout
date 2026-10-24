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

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ..matching import DEFAULT_RULES, MatchingRules, match_job
from ..models import MatchOutcome
from ..storage import Storage
from .store import ServiceStore, ServiceUser


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


def digest_matches(
    storage: Storage,
    store: ServiceStore,
    user: ServiceUser,
    *,
    limit: int = 2000,
    rules_provider: Callable[[Mapping[str, object] | None], MatchingRules] = user_rules,
) -> tuple[str, ...]:
    """Every currently-open role matching this user's scope, newest first.

    Read-only: this is what `/digest` shows, and it creates no notification rows (D-095).
    """
    sources = store.list_subscriptions(user.id)
    if not sources:
        return ()
    rules = rules_provider(store.get_preferences(user.id))
    titles: list[str] = []
    for stored in storage.open_jobs_for(sources, limit=limit):
        decision = match_job(stored.job, rules)
        if decision.decision is MatchOutcome.MATCH:
            titles.append(
                f"· {stored.job.title} — {stored.job.employer} ({stored.job.location_raw})\n"
                f"  {stored.job.url}"
            )
    return tuple(titles)


__all__ = ["UserAlertReport", "digest_matches", "notify_pass", "user_rules"]
