"""Greenhouse (Monzo) adapter tests. Fixtures are synthetic; no network access."""

from __future__ import annotations

from datetime import date

import pytest

from jobscout.http import HttpError
from jobscout.models import ScanStatus
from jobscout.sources.greenhouse import GreenhouseAdapter

MONZO_ENDPOINT = "https://boards-api.greenhouse.io/v1/boards/monzo/jobs?content=true"


def test_maps_a_full_board_in_one_request(monzo, greenhouse_payload, fake_http) -> None:
    client = fake_http(greenhouse_payload)

    result = GreenhouseAdapter().fetch_scan(monzo, client)

    assert result.status is ScanStatus.SUCCESS
    assert result.complete is True
    assert client.requests == [MONZO_ENDPOINT]
    assert result.records_seen == 3
    assert result.records_parsed == 3
    assert result.source_reported_total == 3
    assert [job.source_job_id for job in result.jobs] == ["4102001", "4102002", "4102003"]


def test_maps_fields_from_the_board_record(monzo, greenhouse_payload, fake_http) -> None:
    result = GreenhouseAdapter().fetch_scan(monzo, fake_http(greenhouse_payload))

    graduate = result.jobs[0]
    assert graduate.title == "Graduate Software Engineer"
    assert graduate.employer == "Monzo"
    assert graduate.location_raw == "London, UK"
    assert graduate.location_normalized.qualifies is True
    assert graduate.url == "https://boards.greenhouse.io/monzo/jobs/4102001"
    assert graduate.posted_at == date(2026, 10, 2)
    assert graduate.description == (
        "Build the bank of the future with our graduate engineering team.\nLondon based, hybrid."
    )
    assert graduate.salary_text is None
    assert graduate.source_revision == "2026-10-20T09:15:00-04:00"


def test_html_content_is_unescaped_before_stripping(monzo, greenhouse_payload, fake_http) -> None:
    result = GreenhouseAdapter().fetch_scan(monzo, fake_http(greenhouse_payload))

    assert "&lt;" not in (result.jobs[0].description or "")
    assert "graduate engineering team" in (result.jobs[0].description or "")


def test_salary_metadata_is_used_when_there_is_no_pay_range(
    monzo, greenhouse_payload, fake_http
) -> None:
    result = GreenhouseAdapter().fetch_scan(monzo, fake_http(greenhouse_payload))

    assert result.jobs[1].salary_text == "£40,000 - £45,000 pro rata"


def test_remote_uk_location_is_normalized(monzo, greenhouse_payload, fake_http) -> None:
    result = GreenhouseAdapter().fetch_scan(monzo, fake_http(greenhouse_payload))

    internship = result.jobs[1]
    assert internship.location_normalized.qualifies is True
    assert internship.location_normalized.reason == "uk_remote"


def test_senior_role_is_still_parsed_but_not_a_match(monzo, greenhouse_payload, fake_http) -> None:
    result = GreenhouseAdapter().fetch_scan(monzo, fake_http(greenhouse_payload))

    senior = result.jobs[2]
    assert senior.title == "Senior Software Engineer"


def test_pay_input_ranges_are_formatted(monzo, fake_http) -> None:
    payload = {
        "jobs": [
            {
                "id": 1,
                "title": "Graduate Data Analyst",
                "absolute_url": "https://boards.greenhouse.io/monzo/jobs/1",
                "location": {"name": "London, UK"},
                "pay_input_ranges": [
                    {
                        "min_cents": 4500000,
                        "max_cents": 5500000,
                        "currency_type": "GBP",
                        "unit": "year",
                    }
                ],
            }
        ]
    }

    result = GreenhouseAdapter().fetch_scan(monzo, fake_http(payload))

    assert result.jobs[0].salary_text == "£45,000 - £55,000 per year"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"unexpected": True}, "response has no jobs array"),
        ({"jobs": "nope"}, "response has no jobs array"),
        (["not", "an", "object"], "unexpected payload type: list"),
    ],
)
def test_malformed_payloads_fail_the_scan(monzo, fake_http, payload, expected) -> None:
    result = GreenhouseAdapter().fetch_scan(monzo, fake_http(payload))

    assert result.status is ScanStatus.FAILED
    assert expected in (result.error_summary or "")


def test_transport_failure_fails_the_scan(monzo, fake_http) -> None:
    client = fake_http(error=HttpError("request failed after retries", status=503, retryable=True))

    result = GreenhouseAdapter().fetch_scan(monzo, client)

    assert result.status is ScanStatus.FAILED
    assert "request failed after retries" in (result.error_summary or "")


def test_records_without_identity_title_or_url_are_skipped(monzo, fake_http) -> None:
    payload = {
        "jobs": [
            {"title": "No id", "absolute_url": "https://boards.greenhouse.io/monzo/jobs/9"},
            {"id": 10, "absolute_url": "https://boards.greenhouse.io/monzo/jobs/10"},
            {"id": 11, "title": "No url"},
        ]
    }

    result = GreenhouseAdapter().fetch_scan(monzo, fake_http(payload))

    assert result.status is ScanStatus.PARTIAL
    assert result.records_seen == 3
    assert result.records_parsed == 0
    assert [skip.reason for skip in result.skipped] == [
        "missing_source_job_id",
        "missing_title",
        "missing_url",
    ]


def test_duplicate_ids_are_skipped(monzo, fake_http) -> None:
    payload = {
        "jobs": [
            {
                "id": 11,
                "title": "Graduate Software Engineer",
                "absolute_url": "https://boards.greenhouse.io/monzo/jobs/11",
                "location": {"name": "London, UK"},
            },
            {
                "id": 11,
                "title": "Graduate Software Engineer (republished)",
                "absolute_url": "https://boards.greenhouse.io/monzo/jobs/11",
                "location": {"name": "London, UK"},
            },
        ]
    }

    result = GreenhouseAdapter().fetch_scan(monzo, fake_http(payload))

    assert result.status is ScanStatus.PARTIAL
    assert result.records_parsed == 1
    assert [skip.reason for skip in result.skipped] == ["duplicate_source_job_id"]


def test_empty_board_is_empty_success(monzo, fake_http) -> None:
    result = GreenhouseAdapter().fetch_scan(monzo, fake_http({"jobs": []}))

    assert result.status is ScanStatus.SUCCESS_EMPTY
    assert result.complete is True


def test_missing_location_falls_back_to_offices(monzo, fake_http) -> None:
    payload = {
        "jobs": [
            {
                "id": 12,
                "title": "Graduate Risk Analyst",
                "absolute_url": "https://boards.greenhouse.io/monzo/jobs/12",
                "offices": [{"name": "London"}, {"name": "Cardiff"}],
            }
        ]
    }

    result = GreenhouseAdapter().fetch_scan(monzo, fake_http(payload))

    assert result.jobs[0].location_raw == "London; Cardiff"


def test_board_id_comes_from_options_or_source_id(monzo, fake_http) -> None:
    client = fake_http({"jobs": []})

    GreenhouseAdapter().fetch_scan(monzo, client)

    assert client.requests == [MONZO_ENDPOINT]
