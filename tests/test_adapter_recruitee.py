"""Recruitee adapter tests. The fixture mirrors the widget payload observed 2026-11-13."""

from __future__ import annotations

from datetime import date

from jobscout.config import SourceConfig
from jobscout.http import HttpError
from jobscout.models import ScanStatus
from jobscout.sources.recruitee import RecruiteeAdapter

ENDPOINT = "https://acme.recruitee.com/api/offers/"


def _source(**options) -> SourceConfig:
    return SourceConfig(
        id="acme",
        employer="Acme",
        adapter="recruitee",
        options={"account": "acme", **options},
        access_note="test fixture",
    )


def _offer(**overrides):
    offer = {
        "id": 2804240,
        "guid": "woghk",
        "title": "Graduate Civil Engineer",
        "careers_url": "https://acme.recruitee.com/o/graduate-civil-engineer",
        "careers_apply_url": "https://acme.recruitee.com/o/graduate-civil-engineer/c/new",
        "location": "London, England, United Kingdom",
        "locations": [{"name": "London", "state": "England", "country": "United Kingdom"}],
        "city": "London",
        "country": "United Kingdom",
        "country_code": "GB",
        "description": "<p>Graduate programme, no experience required.</p>",
        "requirements": "<ul><li>Degree in civil engineering</li></ul>",
        "published_at": "2026-11-10 09:30:00 UTC",
        "created_at": "2026-11-09 14:00:29 UTC",
        "remote": False,
        "hybrid": True,
        "employment_type_code": "fulltime_permanent",
        "experience_code": "entry_level",
    }
    offer.update(overrides)
    return offer


def test_maps_a_full_board_in_one_request(fake_http) -> None:
    client = fake_http({"offers": [_offer()]})

    result = RecruiteeAdapter().fetch_scan(_source(), client)

    assert result.status is ScanStatus.SUCCESS
    assert result.complete is True
    assert client.requests == [ENDPOINT]
    assert result.records_seen == 1
    assert result.records_parsed == 1


def test_the_guid_is_the_stable_source_job_id(fake_http) -> None:
    result = RecruiteeAdapter().fetch_scan(_source(), fake_http({"offers": [_offer()]}))
    job = result.jobs[0]

    assert job.source_job_id == "woghk"
    assert job.url == "https://acme.recruitee.com/o/graduate-civil-engineer"


def test_an_offer_without_a_guid_falls_back_to_the_numeric_id(fake_http) -> None:
    result = RecruiteeAdapter().fetch_scan(_source(), fake_http({"offers": [_offer(guid=None)]}))

    assert result.jobs[0].source_job_id == "2804240"


def test_maps_the_fields_the_api_provides(fake_http) -> None:
    result = RecruiteeAdapter().fetch_scan(_source(), fake_http({"offers": [_offer()]}))
    job = result.jobs[0]

    assert job.employer == "Acme"
    assert job.title == "Graduate Civil Engineer"
    assert job.location_raw.startswith("London, England, United Kingdom")
    assert job.location_normalized.qualifies is True
    assert "Graduate programme" in job.description
    assert "civil engineering" in job.description, "requirements are part of the description"
    assert "<" not in job.description, "HTML is stripped"
    assert job.posted_at == date(2026, 11, 10)


def test_remote_and_hybrid_become_the_workplace_type(fake_http) -> None:
    remote = RecruiteeAdapter().fetch_scan(_source(), fake_http({"offers": [_offer(remote=True)]}))
    hybrid = RecruiteeAdapter().fetch_scan(
        _source(), fake_http({"offers": [_offer(remote=False, hybrid=True)]})
    )
    onsite = RecruiteeAdapter().fetch_scan(
        _source(), fake_http({"offers": [_offer(remote=False, hybrid=False)]})
    )

    assert remote.jobs[0].workplace_type == "remote"
    assert hybrid.jobs[0].workplace_type == "hybrid"
    assert onsite.jobs[0].workplace_type is None


def test_a_multi_site_offer_keeps_every_location(fake_http) -> None:
    offer = _offer(
        locations=[
            {"name": "Manchester", "state": "England", "country": "United Kingdom"},
            {"name": "Leeds", "state": "England", "country": "United Kingdom"},
        ]
    )

    result = RecruiteeAdapter().fetch_scan(_source(), fake_http({"offers": [offer]}))

    assert "Manchester" in result.jobs[0].location_raw
    assert "Leeds" in result.jobs[0].location_raw


def test_an_offer_without_a_title_is_skipped(fake_http) -> None:
    result = RecruiteeAdapter().fetch_scan(_source(), fake_http({"offers": [_offer(title=None)]}))

    assert result.status is ScanStatus.PARTIAL
    assert [skip.reason for skip in result.skipped] == ["missing_title"]


def test_the_same_guid_twice_is_a_duplicate(fake_http) -> None:
    result = RecruiteeAdapter().fetch_scan(_source(), fake_http({"offers": [_offer(), _offer()]}))

    assert result.status is ScanStatus.PARTIAL
    assert [skip.reason for skip in result.skipped] == ["duplicate_source_job_id"]
    assert result.records_parsed == 1


def test_an_empty_account_is_success_empty_not_a_failure(fake_http) -> None:
    result = RecruiteeAdapter().fetch_scan(_source(), fake_http({"offers": []}))

    assert result.status is ScanStatus.SUCCESS_EMPTY
    assert result.complete is True


def test_a_transport_error_fails_the_scan_without_raising(fake_http) -> None:
    client = fake_http(None, error=HttpError("boom", status=502))

    result = RecruiteeAdapter().fetch_scan(_source(), client)

    assert result.status is ScanStatus.FAILED
    assert result.complete is False


def test_a_payload_without_an_offers_array_is_a_failure(fake_http) -> None:
    result = RecruiteeAdapter().fetch_scan(_source(), fake_http({"unexpected": True}))

    assert result.status is ScanStatus.FAILED
    assert "offers array" in (result.error_summary or "")
