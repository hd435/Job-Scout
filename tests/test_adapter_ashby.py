"""Ashby adapter tests. Fixtures mirror the real board payload observed 2026-10-25."""

from __future__ import annotations

from datetime import date

from jobscout.config import SourceConfig
from jobscout.http import HttpError
from jobscout.models import ScanStatus
from jobscout.sources.ashby import AshbyAdapter

BOARD_ENDPOINT = "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true"


def _source(**options) -> SourceConfig:
    return SourceConfig(
        id="acme",
        employer="Acme",
        adapter="ashby",
        options={"board": "acme", **options},
        access_note="test fixture",
    )


def _record(**overrides):
    record = {
        "id": "3f6c1a58-0000-4000-8000-000000000001",
        "title": "Graduate Data Engineer",
        "location": "London",
        "secondaryLocations": [{"location": "Remote (UK)"}],
        "workplaceType": "Hybrid",
        "employmentType": "FullTime",
        "department": "Data",
        "team": "Data Platform",
        "isListed": True,
        "publishedAt": "2026-10-02T09:15:00.000+00:00",
        "jobUrl": "https://jobs.ashbyhq.com/acme/3f6c1a58-0000-4000-8000-000000000001",
        "descriptionPlain": "Graduate data engineering programme. No experience required.",
        "descriptionHtml": "<p>Graduate data engineering programme.</p>",
        "compensation": {"compensationTierSummary": "£45K – £55K"},
    }
    record.update(overrides)
    return record


def test_maps_a_full_board_in_one_request(fake_http) -> None:
    client = fake_http({"jobs": [_record()]})

    result = AshbyAdapter().fetch_scan(_source(), client)

    assert result.status is ScanStatus.SUCCESS
    assert result.complete is True
    assert client.requests == [BOARD_ENDPOINT]
    assert result.records_seen == 1
    assert result.records_parsed == 1


def test_maps_the_fields_the_board_provides(fake_http) -> None:
    result = AshbyAdapter().fetch_scan(_source(), fake_http({"jobs": [_record()]}))
    job = result.jobs[0]

    assert job.employer == "Acme"
    assert job.title == "Graduate Data Engineer"
    assert job.location_raw == "London; Remote (UK)"
    assert job.location_normalized.qualifies is True
    assert job.url.endswith("3f6c1a58-0000-4000-8000-000000000001")
    assert job.description == "Graduate data engineering programme. No experience required."
    assert job.posted_at == date(2026, 10, 2)
    assert job.salary_text == "£45K – £55K"
    assert job.workplace_type == "Hybrid"
    assert job.source_revision == "2026-10-02T09:15:00.000+00:00"


def test_unlisted_records_are_not_counted_as_extraction_failures(fake_http) -> None:
    client = fake_http({"jobs": [_record(), _record(id="unlisted", isListed=False)]})

    result = AshbyAdapter().fetch_scan(_source(), client)

    assert result.status is ScanStatus.SUCCESS
    assert result.records_seen == 1
    assert result.skipped == ()


def test_html_description_is_used_when_plain_text_is_missing(fake_http) -> None:
    record = _record(descriptionPlain=None, descriptionHtml="<p>Placement year role.</p>")

    result = AshbyAdapter().fetch_scan(_source(), fake_http({"jobs": [record]}))

    assert result.jobs[0].description == "Placement year role."


def test_a_record_without_a_url_is_skipped_and_the_scan_is_partial(fake_http) -> None:
    client = fake_http({"jobs": [_record(), _record(id="no-url", jobUrl=None)]})

    result = AshbyAdapter().fetch_scan(_source(), client)

    assert result.status is ScanStatus.PARTIAL
    assert result.records_parsed == 1
    assert [skip.reason for skip in result.skipped] == ["missing_url"]


def test_foreign_only_location_does_not_qualify(fake_http) -> None:
    record = _record(location="Berlin", secondaryLocations=[])

    result = AshbyAdapter().fetch_scan(_source(), fake_http({"jobs": [record]}))

    assert result.jobs[0].location_normalized.qualifies is False


def test_uk_employer_option_relaxes_an_ambiguous_town_name(fake_http) -> None:
    record = _record(location="Manchester", secondaryLocations=[])

    result = AshbyAdapter().fetch_scan(_source(uk_employer=True), fake_http({"jobs": [record]}))

    assert result.jobs[0].location_normalized.qualifies is True


def test_an_empty_board_is_success_empty_not_a_failure(fake_http) -> None:
    result = AshbyAdapter().fetch_scan(_source(), fake_http({"jobs": []}))

    assert result.status is ScanStatus.SUCCESS_EMPTY
    assert result.complete is True


def test_a_transport_error_fails_the_scan_without_raising(fake_http) -> None:
    client = fake_http(None, error=HttpError("boom", status=503))

    result = AshbyAdapter().fetch_scan(_source(), client)

    assert result.status is ScanStatus.FAILED
    assert result.complete is False
    assert result.records_parsed == 0


def test_a_payload_without_a_jobs_array_is_a_failure(fake_http) -> None:
    result = AshbyAdapter().fetch_scan(_source(), fake_http({"unexpected": True}))

    assert result.status is ScanStatus.FAILED
    assert "jobs array" in (result.error_summary or "")


def test_the_board_option_falls_back_to_the_source_id(fake_http) -> None:
    source = SourceConfig(
        id="acme", employer="Acme", adapter="ashby", options={}, access_note="test fixture"
    )
    client = fake_http({"jobs": [_record()]})

    AshbyAdapter().fetch_scan(source, client)

    assert client.requests == [BOARD_ENDPOINT]
