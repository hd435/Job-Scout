"""Workable adapter tests. Fixtures mirror the widget payload observed 2026-10-25."""

from __future__ import annotations

from datetime import date

from jobscout.config import SourceConfig
from jobscout.http import HttpError
from jobscout.models import ScanStatus
from jobscout.sources.workable import WorkableAdapter

ENDPOINT = "https://apply.workable.com/api/v1/widget/accounts/acme?details=true"


def _source(**options) -> SourceConfig:
    return SourceConfig(
        id="acme",
        employer="Acme",
        adapter="workable",
        options={"account": "acme", **options},
        access_note="test fixture",
    )


def _record(**overrides):
    record = {
        "title": "Graduate Business Analyst",
        "shortcode": "8E20F2A1BD",
        "code": "abc123",
        "url": "https://apply.workable.com/acme/j/8E20F2A1BD",
        "shortlink": "https://apply.workable.com/j/8E20F2A1BD",
        "city": "London",
        "state": "England",
        "country": "United Kingdom",
        "department": "Delivery",
        "employment_type": "Full-time",
        "telecommuting": False,
        "published_on": "2026-10-19",
        "created_at": "2026-10-18",
        "description": "<p>Graduate programme, no experience required.</p>",
        "locations": [{"city": "Manchester", "region": "England", "country": "UK"}],
    }
    record.update(overrides)
    return record


def test_maps_a_full_board_in_one_request(fake_http) -> None:
    client = fake_http({"jobs": [_record()]})

    result = WorkableAdapter().fetch_scan(_source(), client)

    assert result.status is ScanStatus.SUCCESS
    assert result.complete is True
    assert client.requests == [ENDPOINT]
    assert result.records_seen == 1
    assert result.records_parsed == 1


def test_shortcode_is_the_stable_source_job_id(fake_http) -> None:
    result = WorkableAdapter().fetch_scan(_source(), fake_http({"jobs": [_record()]}))
    job = result.jobs[0]

    assert job.source_job_id == "8E20F2A1BD"
    assert job.url == "https://apply.workable.com/acme/j/8E20F2A1BD"


def test_maps_the_fields_the_widget_provides(fake_http) -> None:
    result = WorkableAdapter().fetch_scan(_source(), fake_http({"jobs": [_record()]}))
    job = result.jobs[0]

    assert job.employer == "Acme"
    assert job.title == "Graduate Business Analyst"
    assert job.location_raw == "London; England; United Kingdom; Manchester, England, UK"
    assert job.location_normalized.qualifies is True
    assert job.description == "Graduate programme, no experience required."
    assert job.posted_at == date(2026, 10, 19)
    assert job.source_revision == "2026-10-19"


def test_remote_flag_becomes_the_workplace_type(fake_http) -> None:
    record = _record(telecommuting=True, city="", state="", country="United Kingdom")

    result = WorkableAdapter().fetch_scan(_source(), fake_http({"jobs": [record]}))

    assert result.jobs[0].workplace_type == "remote"


def test_a_record_without_a_shortcode_is_skipped(fake_http) -> None:
    client = fake_http({"jobs": [_record(), _record(shortcode=None, code=None)]})

    result = WorkableAdapter().fetch_scan(_source(), client)

    assert result.status is ScanStatus.PARTIAL
    assert [skip.reason for skip in result.skipped] == ["missing_source_job_id"]


def test_shortlink_is_used_when_there_is_no_url(fake_http) -> None:
    record = _record(url=None)

    result = WorkableAdapter().fetch_scan(_source(), fake_http({"jobs": [record]}))

    assert result.jobs[0].url == "https://apply.workable.com/j/8E20F2A1BD"


def test_an_empty_account_is_success_empty_not_a_failure(fake_http) -> None:
    result = WorkableAdapter().fetch_scan(_source(), fake_http({"jobs": []}))

    assert result.status is ScanStatus.SUCCESS_EMPTY
    assert result.complete is True


def test_a_transport_error_fails_the_scan_without_raising(fake_http) -> None:
    client = fake_http(None, error=HttpError("boom", status=502))

    result = WorkableAdapter().fetch_scan(_source(), client)

    assert result.status is ScanStatus.FAILED
    assert result.complete is False


def test_a_payload_without_a_jobs_array_is_a_failure(fake_http) -> None:
    result = WorkableAdapter().fetch_scan(_source(), fake_http({"unexpected": True}))

    assert result.status is ScanStatus.FAILED
    assert "jobs array" in (result.error_summary or "")


def test_a_multi_location_post_is_one_job_not_a_duplicate(fake_http) -> None:
    """Workable publishes one row per location under the same shortcode.

    Treating those rows as duplicates made every such board permanently `partial`, and a partial
    scan never baselines, alerts or becomes digest-eligible.
    """
    london = _record()
    manchester = _record(
        city="Manchester",
        state="England",
        country="United Kingdom",
        locations=[],
    )
    birmingham = _record(
        city="Birmingham",
        state="England",
        country="United Kingdom",
        locations=None,
        url="https://apply.workable.com/acme/j/8E20F2A1BD",
    )

    result = WorkableAdapter().fetch_scan(
        _source(), fake_http({"jobs": [london, manchester, birmingham]})
    )

    assert result.status is ScanStatus.SUCCESS, "a multi-location board is still complete"
    assert result.records_seen == 3
    assert result.records_parsed == 1
    assert result.skipped == ()
    job = result.jobs[0]
    assert job.source_job_id == "8E20F2A1BD"
    assert "Manchester, England, United Kingdom" in job.location_raw
    assert "Birmingham, England, United Kingdom" in job.location_raw
    assert job.location_normalized.qualifies is True


def test_two_different_posts_with_the_same_city_stay_two_jobs(fake_http) -> None:
    first = _record()
    second = _record(shortcode="AAAAAAAAAA", url="https://apply.workable.com/acme/j/AAAAAAAAAA")

    result = WorkableAdapter().fetch_scan(_source(), fake_http({"jobs": [first, second]}))

    assert result.status is ScanStatus.SUCCESS
    assert result.records_parsed == 2
    assert {job.source_job_id for job in result.jobs} == {"8E20F2A1BD", "AAAAAAAAAA"}
