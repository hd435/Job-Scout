from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from jobscout.config import SourceConfig
from jobscout.http import HttpError
from jobscout.matching import DEFAULT_RULES
from jobscout.models import JobStatus, ScanStatus
from jobscout.scanner import run_scan
from jobscout.sources import workday
from jobscout.sources.workday import WorkdayAdapter
from jobscout.storage import Storage

REFERENCE = date(2026, 10, 23)
BARCLAYS_HOST = "https://barclays.wd3.myworkdayjobs.com"
BARCLAYS_SITE = "External_Career_Site_Barclays"
BARCLAYS_CXS = f"{BARCLAYS_HOST}/wday/cxs/barclays/{BARCLAYS_SITE}"
BARCLAYS_PUBLIC = f"{BARCLAYS_HOST}/en-US/{BARCLAYS_SITE}"


@pytest.fixture(autouse=True)
def frozen_scan_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze the adapter clock so relative posted dates are exactly assertable."""
    monkeypatch.setattr(workday, "_now_iso", lambda: f"{REFERENCE.isoformat()}T09:00:00Z")


@pytest.fixture
def adapter() -> WorkdayAdapter:
    return WorkdayAdapter(page_size=2)


def posting(requisition: str, title: str, **extra) -> dict:
    record = {
        "title": title,
        "externalPath": f"/job/London/{requisition}",
        "locationsText": "London, United Kingdom",
        "postedOn": "Posted 3 Days Ago",
        "bulletFields": [requisition],
    }
    record.update(extra)
    return record


def page(records: list[dict], total: int | None = None) -> dict:
    payload: dict = {"jobPostings": records}
    if total is not None:
        payload["total"] = total
    return payload


# happy path, identity, and pagination -------------------------------------


def test_paginates_until_the_reported_total(
    adapter, barclays, fake_workday_http, workday_payloads
) -> None:
    client = fake_workday_http(
        pages=[
            workday_payloads["workday_barclays_page1"],
            workday_payloads["workday_barclays_page2"],
        ],
        details={
            "/job/London/Graduate-Data-Analyst_JR101": workday_payloads["workday_barclays_detail"]
        },
    )

    result = adapter.fetch_scan(barclays, client)

    assert result.status is ScanStatus.SUCCESS
    assert result.complete is True
    assert result.records_seen == 4
    assert result.records_parsed == 4
    assert result.skipped == ()
    assert result.source_reported_total == 4
    assert [payload["offset"] for _url, payload in client.posts] == [0, 2]


def test_listing_request_shape(adapter, barclays, fake_workday_http) -> None:
    client = fake_workday_http(
        pages=[page([posting("JR1", "Graduate Software Engineer")], total=1)]
    )

    adapter.fetch_scan(barclays, client)

    url, payload = client.posts[0]
    assert url == f"{BARCLAYS_CXS}/jobs"
    assert payload == {"appliedFacets": {}, "searchText": "", "limit": 2, "offset": 0}


def test_listing_requests_opt_in_to_idempotent_retries(barclays) -> None:
    seen_retry_flags: list[bool] = []

    class Recorder:
        def post_json(self, url, payload, *, retry: bool = False):
            seen_retry_flags.append(retry)
            return page([posting("JR1", "Graduate Software Engineer")], total=1)

        def get_json(self, url):  # pragma: no cover - no placeholder locations here
            raise AssertionError("unexpected detail request")

    WorkdayAdapter(page_size=10).fetch_scan(barclays, Recorder())

    assert seen_retry_flags == [True]


def test_requisition_id_is_the_job_identity(adapter, barclays, fake_workday_http) -> None:
    client = fake_workday_http(
        pages=[page([posting("JR77", "Graduate Software Engineer")], total=1)]
    )

    result = adapter.fetch_scan(barclays, client)

    assert result.jobs[0].source_job_id == "JR77"
    assert result.jobs[0].url == f"{BARCLAYS_PUBLIC}/job/London/JR77"


def test_lloyds_uses_only_the_graduate_site(
    adapter, lloyds, fake_workday_http, workday_payloads
) -> None:
    client = fake_workday_http(pages=[workday_payloads["workday_lloyds_graduate"]])

    result = adapter.fetch_scan(lloyds, client)

    assert result.status is ScanStatus.SUCCESS
    assert result.records_parsed == 2
    assert all("/Graduate_careers/" in url for url, _ in client.posts)
    assert all("LBG_Careers" not in url for url, _ in client.posts)
    assert all(
        job.url.startswith("https://lbg.wd3.myworkdayjobs.com/en-US/Graduate_careers/job/")
        for job in result.jobs
    )


# relative posted dates ----------------------------------------------------


@pytest.mark.parametrize(
    ("posted_on", "expected"),
    [
        ("Posted Today", REFERENCE),
        ("Posted Yesterday", REFERENCE - timedelta(days=1)),
        ("Posted 3 Days Ago", REFERENCE - timedelta(days=3)),
        ("Posted 1 Day Ago", REFERENCE - timedelta(days=1)),
        ("Posted 30+ Days Ago", None),
        ("2026-09-15", date(2026, 9, 15)),
        ("not a date", None),
        (None, None),
    ],
)
def test_relative_posted_dates(adapter, barclays, fake_workday_http, posted_on, expected) -> None:
    record = posting("JR1", "Graduate Software Engineer")
    if posted_on is None:
        record.pop("postedOn")
    else:
        record["postedOn"] = posted_on
    client = fake_workday_http(pages=[page([record], total=1)])

    result = adapter.fetch_scan(barclays, client)

    assert result.jobs[0].posted_at == expected


# multi-location handling --------------------------------------------------


def test_placeholder_location_is_resolved_through_the_detail_endpoint(
    adapter, barclays, fake_workday_http, workday_payloads
) -> None:
    client = fake_workday_http(
        pages=[
            page(
                [posting("JR101", "Graduate Data Analyst", locationsText="2 Locations")],
                total=1,
            )
        ],
        details={"/job/London/JR101": workday_payloads["workday_barclays_detail"]},
    )

    result = adapter.fetch_scan(barclays, client)

    job = result.jobs[0]
    assert result.status is ScanStatus.SUCCESS
    assert job.location_raw == "London, United Kingdom; Manchester, United Kingdom"
    assert job.location_normalized.qualifies is True
    assert job.closing_at == date(2026, 12, 15)
    assert job.description == "Two year graduate programme in data analytics."
    assert job.url.endswith("Graduate-Data-Analyst_JR101")
    assert client.gets == [f"{BARCLAYS_CXS}/job/London/JR101"]


def test_simple_location_needs_no_detail_request(adapter, barclays, fake_workday_http) -> None:
    client = fake_workday_http(
        pages=[page([posting("JR1", "Graduate Software Engineer")], total=1)]
    )

    adapter.fetch_scan(barclays, client)

    assert client.gets == []


def test_failed_detail_lookup_keeps_the_role_but_makes_the_scan_partial(
    adapter, barclays, fake_workday_http
) -> None:
    client = fake_workday_http(
        pages=[
            page([posting("JR101", "Graduate Data Analyst", locationsText="2 Locations")], total=1)
        ],
        details={"/job/London/JR101": HttpError("detail unavailable", status=500, retryable=True)},
    )

    result = adapter.fetch_scan(barclays, client)

    assert result.status is ScanStatus.PARTIAL
    assert result.complete is False
    assert result.records_parsed == 1
    assert result.jobs[0].location_raw == "2 Locations"
    assert result.jobs[0].location_normalized.ambiguous is True
    assert "detail lookup failed for JR101" in (result.error_summary or "")


def test_detail_without_job_posting_info_is_partial(adapter, barclays, fake_workday_http) -> None:
    client = fake_workday_http(
        pages=[
            page([posting("JR101", "Graduate Data Analyst", locationsText="2 Locations")], total=1)
        ],
        details={"/job/London/JR101": {"unexpected": True}},
    )

    result = adapter.fetch_scan(barclays, client)

    assert result.status is ScanStatus.PARTIAL
    assert "had no jobPostingInfo" in (result.error_summary or "")


@pytest.mark.parametrize(
    ("detail", "expected_problem"),
    [
        ({"jobPostingInfo": {}}, "had no usable location"),
        (
            {"jobPostingInfo": {"location": "", "additionalLocations": []}},
            "had no usable location",
        ),
        (
            {"jobPostingInfo": {"location": "   ", "additionalLocations": ["", "  "]}},
            "had no usable location",
        ),
        (
            {"jobPostingInfo": {"location": None, "additionalLocations": [None, {"a": 1}]}},
            "had no usable location",
        ),
        ({"jobPostingInfo": {"location": 42}}, "had no usable location"),
        (
            {"jobPostingInfo": {"location": "Multiple Locations"}},
            "had only placeholder locations 'Multiple Locations'",
        ),
        (
            {"jobPostingInfo": {"location": "2 Locations", "additionalLocations": ["3 Locations"]}},
            "had only placeholder locations '2 Locations; 3 Locations'",
        ),
    ],
)
def test_detail_without_a_usable_location_is_partial(
    adapter, barclays, fake_workday_http, detail, expected_problem
) -> None:
    client = fake_workday_http(
        pages=[
            page([posting("JR101", "Graduate Data Analyst", locationsText="2 Locations")], total=1)
        ],
        details={"/job/London/JR101": detail},
    )

    result = adapter.fetch_scan(barclays, client)

    assert result.status is ScanStatus.PARTIAL
    assert result.complete is False
    assert f"detail lookup for JR101 {expected_problem}" in (result.error_summary or "")
    job = result.jobs[0]
    assert job.location_raw == "2 Locations"
    assert job.location_normalized.ambiguous is True
    assert job.location_normalized.qualifies is False


def test_detail_with_only_additional_locations_resolves_the_placeholder(
    adapter, barclays, fake_workday_http
) -> None:
    client = fake_workday_http(
        pages=[
            page([posting("JR101", "Graduate Data Analyst", locationsText="2 Locations")], total=1)
        ],
        details={
            "/job/London/JR101": {
                "jobPostingInfo": {
                    "location": "",
                    "additionalLocations": ["London, United Kingdom", "Leeds, United Kingdom"],
                }
            }
        },
    )

    result = adapter.fetch_scan(barclays, client)

    assert result.status is ScanStatus.SUCCESS
    assert result.jobs[0].location_raw == "London, United Kingdom; Leeds, United Kingdom"
    assert result.jobs[0].location_normalized.qualifies is True


def test_placeholder_entries_are_dropped_when_a_real_location_is_present(
    adapter, barclays, fake_workday_http
) -> None:
    client = fake_workday_http(
        pages=[
            page([posting("JR101", "Graduate Data Analyst", locationsText="2 Locations")], total=1)
        ],
        details={
            "/job/London/JR101": {
                "jobPostingInfo": {
                    "location": "London, United Kingdom",
                    "additionalLocations": ["2 Locations"],
                }
            }
        },
    )

    result = adapter.fetch_scan(barclays, client)

    assert result.status is ScanStatus.SUCCESS
    assert result.jobs[0].location_raw == "London, United Kingdom"
    assert result.jobs[0].location_normalized.qualifies is True


def test_detail_lookups_are_capped(barclays, fake_workday_http) -> None:
    adapter = WorkdayAdapter(page_size=10, max_detail_lookups=1)
    records = [
        posting("JR1", "Graduate Data Analyst", locationsText="2 Locations"),
        posting("JR2", "Graduate Risk Analyst", locationsText="3 Locations"),
    ]
    client = fake_workday_http(
        pages=[page(records, total=2)],
        details={"/job/London/JR1": {"jobPostingInfo": {"location": "London, United Kingdom"}}},
    )

    result = adapter.fetch_scan(barclays, client)

    assert result.status is ScanStatus.PARTIAL
    assert len(client.gets) == 1
    assert "detail lookups capped at 1" in (result.error_summary or "")
    assert result.records_parsed == 2


def test_a_multi_site_board_can_complete_beyond_ten_detail_lookups(
    barclays, fake_workday_http
) -> None:
    """MBDA's 73 graduate postings are mostly multi-site placeholders.

    A cap of 10 made that board `partial` on every scan, and a partial scan never baselines,
    alerts or becomes digest-eligible. The default budget now covers a whole board of them.
    """
    adapter = WorkdayAdapter(page_size=100)
    records = [
        posting(f"JR{index}", "Graduate Software Engineer", locationsText="2 Locations")
        for index in range(1, 13)
    ]
    details = {
        f"/job/London/JR{index}": {"jobPostingInfo": {"location": "Stevenage"}}
        for index in range(1, 13)
    }
    client = fake_workday_http(pages=[page(records, total=12)], details=details)

    result = adapter.fetch_scan(barclays, client)

    assert result.status is ScanStatus.SUCCESS
    assert len(client.gets) == 12
    assert result.records_parsed == 12
    assert all(job.location_normalized.qualifies for job in result.jobs)


# pagination edge cases ----------------------------------------------------


def test_missing_total_makes_the_scan_partial_but_keeps_the_roles(
    adapter, barclays, fake_workday_http
) -> None:
    client = fake_workday_http(pages=[page([posting("JR1", "Graduate Software Engineer")])])

    result = adapter.fetch_scan(barclays, client)

    assert result.status is ScanStatus.PARTIAL
    assert result.records_parsed == 1
    assert "no usable total" in (result.error_summary or "")
    assert len(client.posts) == 1


def test_short_page_before_the_total_is_partial(adapter, barclays, fake_workday_http) -> None:
    client = fake_workday_http(
        pages=[page([posting("JR1", "Graduate Software Engineer")], total=5)]
    )

    result = adapter.fetch_scan(barclays, client)

    assert result.status is ScanStatus.PARTIAL
    assert "before the reported total (1 of 5)" in (result.error_summary or "")


def test_page_limit_stops_runaway_pagination(barclays, fake_workday_http) -> None:
    adapter = WorkdayAdapter(page_size=1, max_pages=2)
    client = fake_workday_http(
        pages=[
            page([posting("JR1", "Graduate Software Engineer")], total=10),
            page([posting("JR2", "Graduate Data Analyst")], total=10),
            page([posting("JR3", "Graduate Risk Analyst")], total=10),
        ]
    )

    result = adapter.fetch_scan(barclays, client)

    assert result.status is ScanStatus.PARTIAL
    assert result.records_parsed == 2
    assert "stopped after 2 page(s)" in (result.error_summary or "")
    assert len(client.posts) == 2


def test_valid_empty_response_is_empty_success(adapter, barclays, fake_workday_http) -> None:
    client = fake_workday_http(pages=[page([], total=0)])

    result = adapter.fetch_scan(barclays, client)

    assert result.status is ScanStatus.SUCCESS_EMPTY
    assert result.complete is True


# malformed responses ------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (["not", "an", "object"], "listing response was not an object"),
        ({"unexpected": True}, "no jobPostings array"),
        ({"total": 3, "jobPostings": "nope"}, "no jobPostings array"),
    ],
)
def test_malformed_listing_fails_the_scan(
    adapter, barclays, fake_workday_http, response, expected
) -> None:
    client = fake_workday_http(pages=[response])

    result = adapter.fetch_scan(barclays, client)

    assert result.status is ScanStatus.FAILED
    assert result.complete is False
    assert expected in (result.error_summary or "")


def test_non_object_postings_are_reported(adapter, barclays, fake_workday_http) -> None:
    client = fake_workday_http(
        pages=[page([posting("JR1", "Graduate Software Engineer"), "junk"], total=2)]
    )

    result = adapter.fetch_scan(barclays, client)

    assert result.status is ScanStatus.PARTIAL
    assert result.records_parsed == 1
    assert "1 posting(s) were not objects" in (result.error_summary or "")


def test_records_without_identity_title_or_path_are_skipped(
    adapter, barclays, fake_workday_http
) -> None:
    records = [
        {"title": "No identity"},
        {"bulletFields": ["JR9"]},
        {"bulletFields": ["JR10"], "title": "No path"},
    ]
    client = fake_workday_http(pages=[page(records, total=3)])

    result = adapter.fetch_scan(barclays, client)

    assert result.status is ScanStatus.PARTIAL
    assert result.records_seen == 3
    assert result.records_parsed == 0
    assert [skip.reason for skip in result.skipped] == [
        "missing_source_job_id",
        "missing_title",
        "missing_url",
    ]


def test_duplicate_requisition_ids_are_skipped(adapter, barclays, fake_workday_http) -> None:
    records = [
        posting("JR1", "Graduate Software Engineer"),
        posting("JR1", "Graduate Software Engineer (republished)"),
    ]
    client = fake_workday_http(pages=[page(records, total=2)])

    result = adapter.fetch_scan(barclays, client)

    assert result.status is ScanStatus.PARTIAL
    assert result.records_parsed == 1
    assert [skip.reason for skip in result.skipped] == ["duplicate_source_job_id"]


def test_missing_options_fail_the_scan_clearly(fake_workday_http) -> None:
    misconfigured = SourceConfig(id="broken", employer="Broken", adapter="workday", options={})

    result = WorkdayAdapter().fetch_scan(misconfigured, fake_workday_http())

    assert result.status is ScanStatus.FAILED
    assert "missing required options: host, tenant, site" in (result.error_summary or "")


# transport failures -------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retryable_transport_failure_becomes_a_failed_scan(
    barclays, fake_workday_http, status
) -> None:
    client = fake_workday_http()
    client.post_error = HttpError("request failed after retries", status=status, retryable=True)

    result = WorkdayAdapter().fetch_scan(barclays, client)

    assert result.status is ScanStatus.FAILED
    assert result.complete is False
    assert "request failed after retries" in (result.error_summary or "")


# scanner integration ------------------------------------------------------


def run_workday_scan(storage: Storage, config: SourceConfig, client):
    return run_scan(storage, WorkdayAdapter(page_size=10), config, client, rules=DEFAULT_RULES)


def test_partial_workday_scan_cannot_close_or_alert(store, barclays, fake_workday_http) -> None:
    baseline = fake_workday_http(
        pages=[
            page(
                [
                    posting("JR1", "Graduate Software Engineer"),
                    posting("JR2", "Graduate Data Engineer"),
                ],
                total=2,
            )
        ]
    )
    assert run_workday_scan(store, barclays, baseline).is_baseline is True

    truncated = fake_workday_http(
        pages=[
            page([posting("JR1", "Graduate Software Engineer")], total=2),
            page([], total=2),
        ]
    )
    outcome = run_workday_scan(store, barclays, truncated)

    assert outcome.status is ScanStatus.PARTIAL
    assert outcome.closed_jobs == 0
    missing = store.get_job("barclays", "JR2")
    assert missing.status is JobStatus.OPEN
    assert missing.consecutive_misses == 0


def test_unresolved_placeholder_detail_cannot_close_or_alert(
    store, barclays, fake_workday_http
) -> None:
    baseline = fake_workday_http(
        pages=[
            page(
                [
                    posting("JR1", "Graduate Software Engineer"),
                    posting("JR2", "Graduate Data Engineer"),
                ],
                total=2,
            )
        ]
    )
    assert run_workday_scan(store, barclays, baseline).is_baseline is True

    # JR2 disappears and a new placeholder role arrives, but its detail payload has no
    # usable location, so the scan must not be complete.
    later = fake_workday_http(
        pages=[
            page(
                [
                    posting("JR1", "Graduate Software Engineer"),
                    posting("JR3", "Graduate Data Engineer", locationsText="2 Locations"),
                ],
                total=2,
            )
        ],
        details={"/job/London/JR3": {"jobPostingInfo": {"location": ""}}},
    )
    outcome = run_workday_scan(store, barclays, later)

    assert outcome.status is ScanStatus.PARTIAL
    assert outcome.complete is False
    assert outcome.closed_jobs == 0
    assert outcome.candidates == ()
    assert outcome.notifications_queued == 0
    assert "detail lookup for JR3 had no usable location" in (outcome.error_summary or "")

    missing = store.get_job("barclays", "JR2")
    assert missing.status is JobStatus.OPEN
    assert missing.consecutive_misses == 0
    unresolved = store.get_job("barclays", "JR3")
    assert unresolved.first_complete_scan_id is None


def test_complete_workday_scan_alerts_for_new_matching_roles(
    store, barclays, fake_workday_http
) -> None:
    baseline = fake_workday_http(
        pages=[page([posting("JR1", "Graduate Software Engineer")], total=1)]
    )
    run_workday_scan(store, barclays, baseline)

    later = fake_workday_http(
        pages=[
            page(
                [
                    posting("JR1", "Graduate Software Engineer"),
                    posting("JR2", "Graduate Data Engineer"),
                ],
                total=2,
            )
        ]
    )
    outcome = run_workday_scan(store, barclays, later)

    assert outcome.status is ScanStatus.SUCCESS
    assert [candidate.title for candidate in outcome.candidates] == ["Graduate Data Engineer"]
    stored = store.get_job("barclays", "JR2")
    assert stored.first_complete_scan_id == outcome.scan_id
    assert stored.match is not None
    assert stored.match.decision.value == "match"


def test_workday_dedupe_survives_a_restart(tmp_path, barclays, fake_workday_http) -> None:
    db_path = tmp_path / "workday.db"

    def client():
        return fake_workday_http(
            pages=[page([posting("JR1", "Graduate Software Engineer")], total=1)]
        )

    with Storage.open(db_path) as storage:
        run_workday_scan(storage, barclays, client())
    with Storage.open(db_path) as storage:
        outcome = run_workday_scan(storage, barclays, client())

    assert outcome.is_baseline is False
    assert outcome.candidates == ()
    assert outcome.new_jobs == 0


def test_uk_employer_context_makes_an_ambiguous_town_alert_eligible(
    store, lloyds, fake_workday_http
) -> None:
    """A UK-only board: 'Manchester' qualifies instead of going to review (D-065)."""

    def run(options, requisition: str):
        body = page(
            [posting(requisition, "Graduate Software Engineer", locationsText="Manchester")],
            total=1,
        )
        config = SourceConfig(
            id="lloyds_graduate",
            employer="Lloyds Banking Group",
            adapter="workday",
            options=options,
        )
        return run_workday_scan(
            store, config, fake_workday_http(pages=[json.loads(json.dumps(body))])
        )

    conservative = run(dict(lloyds.options), "JR1")  # first scan baselines the board
    uk_context = run({**lloyds.options, "uk_employer": True}, "JR2")  # a new role later

    assert conservative.needs_review == 1
    assert conservative.candidates == ()
    assert uk_context.needs_review == 0
    assert len(uk_context.candidates) == 1
    assert "location:uk_city:manchester" in uk_context.candidates[0].decision.reasons
    assert "early_career:graduate" in uk_context.candidates[0].decision.reasons
