"""SmartRecruiters adapter tests. The fixture mirrors the postings payload observed 2026-11-14."""

from __future__ import annotations

from datetime import date

from jobscout.config import SourceConfig
from jobscout.http import HttpError
from jobscout.models import ScanStatus
from jobscout.sources.smartrecruiters import SmartRecruitersAdapter

FIRST_PAGE = "https://api.smartrecruiters.com/v1/companies/Acme/postings?limit=100&offset=0"


def _source(**options) -> SourceConfig:
    return SourceConfig(
        id="acme",
        employer="Acme Group",
        adapter="smartrecruiters",
        options={"company": "Acme", **options},
        access_note="test fixture",
    )


def _posting(**overrides):
    posting = {
        "id": "744000161514639",
        "name": "Graduate - Medical Underwriting - Cardiff - 2027",
        "company": {"identifier": "Acme", "name": "Acme Group"},
        "releasedDate": "2026-11-13T15:24:03.217Z",
        "location": {
            "city": "Cardiff",
            "region": "Wales",
            "country": "gb",
            "remote": False,
            "hybrid": True,
            "fullLocation": "Cardiff, Wales, United Kingdom",
        },
    }
    posting.update(overrides)
    return posting


def test_maps_a_board_in_one_request(fake_http) -> None:
    client = fake_http({"totalFound": 1, "content": [_posting()]})

    result = SmartRecruitersAdapter().fetch_scan(_source(), client)

    assert result.status is ScanStatus.SUCCESS
    assert result.complete is True
    assert client.requests == [FIRST_PAGE]
    job = result.jobs[0]
    assert job.title == "Graduate - Medical Underwriting - Cardiff - 2027"
    assert job.location_raw == "Cardiff, Wales, United Kingdom"
    assert job.workplace_type == "hybrid"
    assert job.posted_at == date(2026, 11, 13)
    # The employer's own tenant page, so a user applies to the employer rather than a redirect.
    assert job.url == "https://jobs.smartrecruiters.com/Acme/744000161514639"


def test_pages_until_the_reported_total(fake_http) -> None:
    class Paged:
        """Returns one scripted response per request, so paging can be tested."""

        def __init__(self, *responses):
            self.responses = list(responses)
            self.requests: list[str] = []

        def get_json(self, url: str):
            self.requests.append(url)
            value = self.responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

    client = Paged(
        {"totalFound": 101, "content": [_posting(id=str(i)) for i in range(100)]},
        {"totalFound": 101, "content": [_posting(id="999")]},
    )

    result = SmartRecruitersAdapter().fetch_scan(_source(), client)

    assert result.records_parsed == 101
    assert result.complete is True
    assert len(client.requests) == 2
    assert client.requests[1].endswith("offset=100")


def test_a_failure_after_the_first_page_is_partial_not_success() -> None:
    """A partial board must never baseline silently: that is how roles get missed."""

    class Paged:
        def __init__(self, *responses):
            self.responses = list(responses)
            self.requests: list[str] = []

        def get_json(self, url: str):
            self.requests.append(url)
            value = self.responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

    client = Paged(
        {"totalFound": 200, "content": [_posting(id=str(i)) for i in range(100)]},
        HttpError("request failed", status=500, retryable=True),
    )

    result = SmartRecruitersAdapter().fetch_scan(_source(), client)

    assert result.status is ScanStatus.PARTIAL
    assert result.complete is False
    assert result.records_parsed == 100


def test_a_first_page_failure_is_a_failed_scan(fake_http) -> None:
    client = fake_http(HttpError("request rejected", status=404))

    result = SmartRecruitersAdapter().fetch_scan(_source(), client)

    assert result.status is ScanStatus.FAILED


def test_a_posting_without_a_title_is_skipped_not_guessed(fake_http) -> None:
    client = fake_http({"totalFound": 2, "content": [_posting(name=""), _posting(id="2")]})

    result = SmartRecruitersAdapter().fetch_scan(_source(), client)

    assert result.records_parsed == 1
    assert result.status is ScanStatus.PARTIAL
    assert result.skipped[0].reason == "missing_title"


def test_an_empty_board_is_success_empty(fake_http) -> None:
    client = fake_http({"totalFound": 0, "content": []})

    result = SmartRecruitersAdapter().fetch_scan(_source(), client)

    assert result.status is ScanStatus.SUCCESS_EMPTY
