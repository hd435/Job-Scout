from __future__ import annotations

from datetime import date

import pytest

from jobscout.config import SourceConfig
from jobscout.http import HttpError
from jobscout.models import ScanStatus
from jobscout.sources.lever import LeverAdapter


@pytest.fixture
def adapter() -> LeverAdapter:
    return LeverAdapter()


@pytest.fixture
def config() -> SourceConfig:
    return SourceConfig(
        id="palantir",
        employer="Palantir Technologies",
        adapter="lever",
        options={"site": "palantir"},
    )


def test_full_fixture_maps_every_record(adapter, config, fixture_payload, fake_http):
    client = fake_http(fixture_payload)

    result = adapter.fetch_scan(config, client)

    assert result.status is ScanStatus.SUCCESS
    assert result.complete is True
    assert result.records_seen == 22
    assert result.records_parsed == 22
    assert result.skipped == ()
    assert result.source_reported_total == 22
    assert client.requests == ["https://api.lever.co/v0/postings/palantir?mode=json"]


def test_record_mapping_covers_all_normalized_fields(adapter, config, fixture_payload, fake_http):
    result = adapter.fetch_scan(config, fake_http(fixture_payload))
    job = next(j for j in result.jobs if j.source_job_id.endswith("0001"))

    assert job.title == "Forward Deployed Software Engineer - New Grad"
    assert job.employer == "Palantir Technologies"
    assert job.url.startswith("https://jobs.lever.co/palantir/")
    assert job.location_raw == "London, United Kingdom"
    assert job.location_normalized.qualifies is True
    assert job.location_normalized.reason == "london"
    assert job.workplace_type == "hybrid"
    assert job.posted_at == date(2025, 9, 1)
    assert job.closing_at is None
    assert job.salary_text == "£85,000 - £95,000 per year"
    assert job.description and "visa sponsorship" in job.description.casefold()
    assert len(job.content_hash) == 64


def test_single_bound_salary_is_formatted(adapter, config, fixture_payload, fake_http):
    result = adapter.fetch_scan(config, fake_http(fixture_payload))
    job = next(j for j in result.jobs if j.source_job_id.endswith("0018"))

    assert job.salary_text == "£55,000 per year"


def test_missing_optional_fields_stay_none(adapter, config, fixture_payload, fake_http):
    result = adapter.fetch_scan(config, fake_http(fixture_payload))
    no_salary = next(j for j in result.jobs if j.source_job_id.endswith("0002"))
    bad_timestamp = next(j for j in result.jobs if j.source_job_id.endswith("0016"))

    assert no_salary.salary_text is None
    assert no_salary.closing_at is None
    assert bad_timestamp.posted_at is None


def test_multi_location_is_joined(adapter, config, fixture_payload, fake_http):
    result = adapter.fetch_scan(config, fake_http(fixture_payload))
    multi = next(j for j in result.jobs if j.source_job_id.endswith("0006"))

    assert multi.location_raw == "London, United Kingdom; New York, United States"
    assert multi.location_normalized.qualifies is True


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        ({"text": "Software Engineer", "hostedUrl": "https://x/1"}, "missing_source_job_id"),
        ({"id": "a1", "hostedUrl": "https://x/1"}, "missing_title"),
        ({"id": "a2", "text": "Software Engineer"}, "missing_url"),
        ("not-a-record", "record_not_object"),
    ],
)
def test_unusable_records_are_skipped_with_a_reason(adapter, config, fake_http, record, reason):
    result = adapter.fetch_scan(config, fake_http([record]))

    assert result.status is ScanStatus.PARTIAL
    assert result.complete is False
    assert result.records_seen == 1
    assert result.records_parsed == 0
    assert [skip.reason for skip in result.skipped] == [reason]


def test_duplicate_ids_are_skipped_deterministically(adapter, config, fake_http):
    payload = [
        {"id": "dup", "text": "Graduate Software Engineer", "hostedUrl": "https://x/1"},
        {"id": "dup", "text": "Senior Software Engineer", "hostedUrl": "https://x/2"},
    ]

    result = adapter.fetch_scan(config, fake_http(payload))

    assert result.status is ScanStatus.PARTIAL
    assert result.records_parsed == 1
    assert result.jobs[0].url == "https://x/1"
    assert [skip.reason for skip in result.skipped] == ["duplicate_source_job_id"]


def test_empty_payload_is_a_valid_empty_success(adapter, config, fake_http):
    result = adapter.fetch_scan(config, fake_http([]))

    assert result.status is ScanStatus.SUCCESS_EMPTY
    assert result.complete is True
    assert result.records_seen == 0
    assert result.records_parsed == 0


def test_non_list_payload_fails(adapter, config, fake_http):
    result = adapter.fetch_scan(config, fake_http({"error": "unexpected"}))

    assert result.status is ScanStatus.FAILED
    assert result.complete is False
    assert "unexpected payload type: dict" in (result.error_summary or "")


@pytest.mark.parametrize("retryable", [True, False])
def test_http_errors_produce_a_failed_scan(adapter, config, fake_http, retryable):
    error = HttpError("request failed", status=503 if retryable else 404, retryable=retryable)

    result = adapter.fetch_scan(config, fake_http(None, error=error))

    assert result.status is ScanStatus.FAILED
    assert result.complete is False
    assert result.records_parsed == 0
    assert "request failed" in (result.error_summary or "")
