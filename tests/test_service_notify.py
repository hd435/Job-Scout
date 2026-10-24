"""Per-user evaluation over a shared scan (D-092, D-095).

These are the tests that matter most for the hosted service: two users, one scan, and the
assertion that each is told only their own decisions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobscout.config import SourceConfig
from jobscout.http import FixtureHttpClient
from jobscout.matching import DEFAULT_RULES
from jobscout.scanner import run_scan
from jobscout.service.notify import digest_matches, notify_pass, user_rules
from jobscout.service.store import ServiceStore
from jobscout.sources import get_adapter
from jobscout.storage import Storage

SOFTWARE = SourceConfig(
    id="software_co",
    employer="Software Co",
    adapter="lever",
    options={"site": "software_co"},
    access_note="test fixture",
)
FINANCE = SourceConfig(
    id="finance_co",
    employer="Finance Co",
    adapter="lever",
    options={"site": "finance_co"},
    access_note="test fixture",
)

LONDON_SCOPE = {
    "role_families": ["software", "data"],
    "employment_types": ["graduate_scheme", "internship", "placement", "entry_level_permanent"],
    "locations": {"mode": "pinned", "cities": ["London"], "remote_uk": True, "hybrid_uk": True},
}
UK_FINANCE_SCOPE = {
    "role_families": ["finance", "business_analysis"],
    "employment_types": ["graduate_scheme", "entry_level_permanent"],
    "locations": {"mode": "uk", "cities": [], "remote_uk": True, "hybrid_uk": True},
}


def record(role_id: str, title: str, location: str) -> dict:
    return {
        "id": role_id,
        "text": title,
        "categories": {"location": location, "allLocations": [location]},
        "workplaceType": "onsite",
        "descriptionPlain": "Fixture record.",
        "createdAt": 1759276800000,
        "hostedUrl": f"https://example.test/{role_id}",
    }


def scan(tmp_path: Path, storage: Storage, source: SourceConfig, records: list[dict]) -> None:
    payload = tmp_path / f"{source.id}-{len(records)}.json"
    payload.write_text(json.dumps(records), encoding="utf-8")
    run_scan(
        storage,
        get_adapter("lever"),
        source,
        FixtureHttpClient(payload),
        rules=DEFAULT_RULES,
        clock=lambda: "2026-10-25T08:00:00Z",
    )


@pytest.fixture
def store(tmp_path: Path) -> ServiceStore:
    service = ServiceStore.open(tmp_path / "service.db")
    try:
        yield service
    finally:
        service.close()


def two_users(store: ServiceStore) -> tuple[int, int]:
    """A London software seeker and a UK-wide finance seeker, both subscribed."""
    storage = store.storage
    alice = store.ensure_user(111, "111", now="2026-10-25T07:00:00Z")
    bob = store.ensure_user(222, "222", now="2026-10-25T07:00:00Z")
    store.save_preferences(alice.id, LONDON_SCOPE, fingerprint="fp-a")
    store.save_preferences(bob.id, UK_FINANCE_SCOPE, fingerprint="fp-b")
    store.set_subscriptions(alice.id, (SOFTWARE.id,))
    store.set_subscriptions(bob.id, (SOFTWARE.id, FINANCE.id))
    # The watermark is what makes "new" mean new (D-095).
    store.set_watermark(alice.id, storage.highest_job_id())
    store.set_watermark(bob.id, storage.highest_job_id())
    return alice.id, bob.id


def test_each_user_is_told_only_their_own_matches(tmp_path: Path, store: ServiceStore) -> None:
    alice_id, bob_id = two_users(store)
    storage = store.storage
    scan(
        tmp_path,
        storage,
        SOFTWARE,
        [
            record("s1", "Graduate Software Engineer", "London, United Kingdom"),
            record("s2", "Graduate Finance Analyst", "Manchester, United Kingdom"),
        ],
    )
    scan(
        tmp_path,
        storage,
        FINANCE,
        [record("f1", "Graduate Finance Analyst", "Manchester, United Kingdom")],
    )

    reports = notify_pass(storage, store)

    # Alice: one London software role. Bob: the same-titled finance role on two employers.
    assert sum(report.queued for report in reports) == 3
    assert [alert.title for alert in store.list_alerts(alice_id)] == ["Graduate Software Engineer"]
    assert [alert.title for alert in store.list_alerts(bob_id)] == [
        "Graduate Finance Analyst",
        "Graduate Finance Analyst",
    ]
    assert store.list_alerts(alice_id)[0].reason, "the reason travels with the alert"


def test_a_second_pass_over_the_same_roles_queues_nothing(
    tmp_path: Path, store: ServiceStore
) -> None:
    alice_id, _bob_id = two_users(store)
    storage = store.storage
    scan(tmp_path, storage, SOFTWARE, [record("s1", "Graduate Software Engineer", "London, UK")])

    notify_pass(storage, store)
    second = notify_pass(storage, store)

    assert sum(report.queued for report in second) == 0
    assert second[0].candidates == 0, "the watermark advanced past everything read"
    assert len(store.list_alerts(alice_id)) == 1


def test_a_new_role_after_the_watermark_is_announced_once(
    tmp_path: Path, store: ServiceStore
) -> None:
    alice_id, _bob_id = two_users(store)
    storage = store.storage
    scan(tmp_path, storage, SOFTWARE, [record("s1", "Graduate Software Engineer", "London, UK")])
    notify_pass(storage, store)

    scan(
        tmp_path,
        storage,
        SOFTWARE,
        [
            record("s1", "Graduate Software Engineer", "London, UK"),
            record("s2", "Graduate Data Analyst", "London, UK"),
        ],
    )
    notify_pass(storage, store)
    notify_pass(storage, store)

    titles = [alert.title for alert in store.list_alerts(alice_id)]
    assert titles == ["Graduate Data Analyst", "Graduate Software Engineer"]


def test_roles_needing_review_are_never_sent_to_a_user(tmp_path: Path, store: ServiceStore) -> None:
    """Review exists to tune the operator's rules; forwarding it would be noise as an alert."""
    alice_id, _bob_id = two_users(store)
    storage = store.storage
    scan(tmp_path, storage, SOFTWARE, [record("s1", "Software Engineer", "London, UK")])

    reports = notify_pass(storage, store)

    assert store.list_alerts(alice_id) == ()
    assert reports[0].review == 1
    assert reports[0].queued == 0


def test_a_paused_user_is_skipped_but_keeps_their_subscription(
    tmp_path: Path, store: ServiceStore
) -> None:
    alice_id, _bob_id = two_users(store)
    storage = store.storage
    store.set_paused(alice_id, True)
    scan(tmp_path, storage, SOFTWARE, [record("s1", "Graduate Software Engineer", "London, UK")])

    reports = notify_pass(storage, store)

    assert all(report.user_id != alice_id for report in reports)
    assert store.list_alerts(alice_id) == ()
    assert store.list_subscriptions(alice_id) == (SOFTWARE.id,)


def test_a_user_without_subscriptions_is_never_evaluated(store: ServiceStore) -> None:
    user = store.ensure_user(333, "333")

    reports = notify_pass(store.storage, store)

    assert [report.user_id for report in reports] == [user.id]
    assert reports[0].candidates == 0


def test_deleting_a_user_removes_only_their_data(tmp_path: Path, store: ServiceStore) -> None:
    alice_id, bob_id = two_users(store)
    storage = store.storage
    scan(
        tmp_path,
        storage,
        SOFTWARE,
        [
            record("s1", "Graduate Software Engineer", "London, UK"),
            record("s2", "Graduate Finance Analyst", "Manchester, United Kingdom"),
        ],
    )
    scan(tmp_path, storage, FINANCE, [record("f1", "Graduate Finance Analyst", "Manchester, UK")])
    notify_pass(storage, store)
    assert store.list_alerts(alice_id) and store.list_alerts(bob_id)

    store.delete_user(alice_id)

    assert store.get_user_by_id(alice_id) is None
    assert store.get_preferences(alice_id) is None
    assert store.list_subscriptions(alice_id) == ()
    assert store.list_alerts(alice_id) == ()
    assert store.list_alerts(bob_id), "the other user is untouched"
    assert store.list_subscriptions(bob_id)


def test_the_digest_lists_current_matches_without_queueing_alerts(
    tmp_path: Path, store: ServiceStore
) -> None:
    alice_id, _bob_id = two_users(store)
    storage = store.storage
    scan(
        tmp_path,
        storage,
        SOFTWARE,
        [
            record("s1", "Graduate Software Engineer", "London, UK"),
            record("s2", "Staff Software Engineer", "London, UK"),
        ],
    )
    user = store.get_user_by_id(alice_id)
    assert user is not None

    entries = digest_matches(storage, store, user)

    assert len(entries) == 1
    assert "Graduate Software Engineer" in entries[0]
    assert store.list_alerts(alice_id) == (), "a digest is read-only"


def test_an_invalid_stored_scope_skips_only_that_user(store: ServiceStore) -> None:
    broken = store.ensure_user(444, "444")
    store.save_preferences(broken.id, {"role_families": ["not_a_family"]}, fingerprint="bad")
    store.set_subscriptions(broken.id, (SOFTWARE.id,))

    reports = notify_pass(store.storage, store)

    assert [report.user_id for report in reports] == [broken.id]
    assert reports[0].queued == 0


def test_user_rules_match_the_cli_for_the_same_settings() -> None:
    rules = user_rules(LONDON_SCOPE)

    assert rules.allowed_families == ("software", "data")
    assert rules.locations.mode == "pinned"
    assert rules.locations.cities == ("London",)
    assert rules.version.startswith("m1.1+")
