"""Sitemap + HTML adapter tests for Wise and Arm. Fixtures are synthetic; no network."""

from __future__ import annotations

from datetime import date

import pytest

from jobscout.http import HttpError
from jobscout.matching import DEFAULT_RULES
from jobscout.models import JobStatus, ScanStatus
from jobscout.scanner import run_scan
from jobscout.sources.sitemap_html import SitemapHtmlAdapter

WISE_INDEX = "https://wise.jobs/sitemap.xml"
WISE_VACANCIES = "https://wise.jobs/vacanciessitemap.xml"
WISE_PAGES = "https://wise.jobs/pagessitemap.xml"
WISE_JOB_1 = "https://wise.jobs/jobs/graduate-data-scientist-london-jid-4821"
WISE_JOB_2 = "https://wise.jobs/jobs/graduate-software-engineer-london-jid-4822"
WISE_JOB_3 = "https://wise.jobs/jobs/senior-product-manager-london-jid-4823"

ARM_SITEMAP = "https://careers.arm.com/sitemap.xml"
ARM_JOB_1 = "https://careers.arm.com/job/cambridge/data-analytics-intern/88121/"
ARM_JOB_2 = "https://careers.arm.com/job/manchester/graduate-software-engineer/88122/"


def wise_adapter(**kwargs) -> SitemapHtmlAdapter:
    kwargs.setdefault("sleep", lambda _seconds: None)
    kwargs.setdefault("request_delay", 0.0)
    return SitemapHtmlAdapter(profile="wise", **kwargs)


def arm_adapter(**kwargs) -> SitemapHtmlAdapter:
    kwargs.setdefault("sleep", lambda _seconds: None)
    kwargs.setdefault("request_delay", 0.0)
    return SitemapHtmlAdapter(profile="arm", **kwargs)


def wise_documents(html_payloads: dict[str, str], **overrides) -> dict[str, object]:
    documents: dict[str, object] = {
        WISE_INDEX: html_payloads["wise_sitemap_index.xml"],
        WISE_VACANCIES: html_payloads["wise_vacancies_sitemap.xml"],
        WISE_JOB_1: html_payloads["wise_job_page.html"],
        WISE_JOB_2: html_payloads["wise_job_page.html"],
        WISE_JOB_3: html_payloads["wise_job_page.html"],
    }
    documents.update(overrides)
    return documents


# discovery ---------------------------------------------------------------


def test_follows_the_index_to_the_vacancy_sitemap(wise, html_payloads, fake_html_http) -> None:
    client = fake_html_http(wise_documents(html_payloads))

    result = wise_adapter().fetch_scan(wise, client)

    assert result.status is ScanStatus.SUCCESS
    assert result.complete is True
    assert client.gets == [WISE_INDEX, WISE_VACANCIES, WISE_JOB_1, WISE_JOB_2, WISE_JOB_3]
    assert WISE_PAGES not in client.gets
    assert result.records_seen == 3
    assert result.records_parsed == 3
    assert [job.source_job_id for job in result.jobs] == ["4821", "4822", "4823"]


def test_arm_ignores_non_job_urls_and_never_requests_the_search_path(
    arm, html_payloads, fake_html_http
) -> None:
    client = fake_html_http(
        {
            ARM_SITEMAP: html_payloads["arm_sitemap.xml"],
            ARM_JOB_1: html_payloads["arm_job_page.html"],
            ARM_JOB_2: html_payloads["arm_job_page_no_ld.html"],
        }
    )

    result = arm_adapter().fetch_scan(arm, client)

    assert result.status is ScanStatus.SUCCESS
    assert [job.source_job_id for job in result.jobs] == ["88121", "88122"]
    assert not any("search-jobs" in url for url in client.gets)
    assert result.records_seen == 2


def test_no_matching_job_urls_makes_the_scan_partial(wise, fake_html_http) -> None:
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://wise.jobs/about</loc></url>"
        "</urlset>"
    )
    client = fake_html_http({WISE_INDEX: sitemap})

    result = wise_adapter().fetch_scan(wise, client)

    assert result.status is ScanStatus.PARTIAL
    assert "matched the wise job URL pattern" in (result.error_summary or "")


def test_empty_urlset_is_empty_success(wise, fake_html_http) -> None:
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'
    )

    result = wise_adapter().fetch_scan(wise, fake_html_http({WISE_INDEX: sitemap}))

    assert result.status is ScanStatus.SUCCESS_EMPTY
    assert result.complete is True


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        ("not xml at all", "is not valid XML"),
        ('<html xmlns="http://www.w3.org/1999/xhtml"></html>', "unexpected sitemap root element"),
    ],
)
def test_malformed_sitemaps_fail_the_scan(wise, fake_html_http, markup, expected) -> None:
    result = wise_adapter().fetch_scan(wise, fake_html_http({WISE_INDEX: markup}))

    assert result.status is ScanStatus.FAILED
    assert expected in (result.error_summary or "")


def test_sitemap_request_failure_fails_the_scan(wise, fake_html_http) -> None:
    client = fake_html_http(
        {WISE_INDEX: HttpError("request failed after retries", status=503, retryable=True)}
    )

    result = wise_adapter().fetch_scan(wise, client)

    assert result.status is ScanStatus.FAILED
    assert "request failed after retries" in (result.error_summary or "")


def test_sitemap_document_cap_marks_the_scan_partial(wise, html_payloads, fake_html_http) -> None:
    client = fake_html_http(wise_documents(html_payloads))

    result = wise_adapter(max_sitemap_documents=1).fetch_scan(wise, client)

    assert result.status is ScanStatus.PARTIAL
    assert "sitemap documents capped at 1" in (result.error_summary or "")
    assert client.gets == [WISE_INDEX]


def test_duplicate_job_ids_are_skipped(wise, fake_html_http) -> None:
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{WISE_JOB_1}</loc><lastmod>2026-10-21</lastmod></url>"
        f"<url><loc>{WISE_JOB_1}</loc><lastmod>2026-10-22</lastmod></url>"
        "</urlset>"
    )
    client = fake_html_http(
        {WISE_INDEX: sitemap, WISE_JOB_1: "<html><h1>Graduate Analyst</h1></html>"}
    )

    result = wise_adapter().fetch_scan(wise, client)

    assert result.status is ScanStatus.PARTIAL
    assert [skip.reason for skip in result.skipped] == ["duplicate_source_job_id"]
    assert result.records_seen == 1


def test_unknown_profile_fails_clearly(wise, fake_html_http) -> None:
    misconfigured = type(wise)(
        id="mystery",
        employer="Mystery",
        adapter="sitemap_html",
        options={"profile": "mystery"},
    )

    result = SitemapHtmlAdapter(profile=None).fetch_scan(misconfigured, fake_html_http())

    assert result.status is ScanStatus.FAILED
    assert "unknown sitemap profile 'mystery'" in (result.error_summary or "")


# field mapping -----------------------------------------------------------


def test_maps_json_ld_job_posting_fields(wise, html_payloads, fake_html_http) -> None:
    client = fake_html_http(wise_documents(html_payloads))

    result = wise_adapter().fetch_scan(wise, client)

    job = result.jobs[0]
    assert job.title == "Graduate Data Scientist"
    assert job.employer == "Wise"
    assert job.url == WISE_JOB_1
    assert job.location_raw == "London, United Kingdom"
    assert job.location_normalized.qualifies is True
    assert job.posted_at == date(2026, 10, 21)
    assert job.closing_at == date(2026, 11, 30)
    assert job.salary_text == "£58,000 - £58,000 per year"
    assert job.description == "Join the graduate data science programme."
    assert job.source_revision == "2026-10-21"


def test_multiple_job_locations_are_joined(arm, html_payloads, fake_html_http) -> None:
    client = fake_html_http(
        {
            ARM_SITEMAP: html_payloads["arm_sitemap.xml"],
            ARM_JOB_1: html_payloads["arm_job_page.html"],
            ARM_JOB_2: html_payloads["arm_job_page_no_ld.html"],
        }
    )

    result = arm_adapter().fetch_scan(arm, client)

    intern = result.jobs[0]
    assert intern.location_raw == "Cambridge, United Kingdom; Manchester, United Kingdom"
    assert intern.closing_at == date(2026, 11, 15)


def test_page_without_json_ld_falls_back_to_the_heading(arm, html_payloads, fake_html_http) -> None:
    client = fake_html_http(
        {
            ARM_SITEMAP: html_payloads["arm_sitemap.xml"],
            ARM_JOB_1: html_payloads["arm_job_page.html"],
            ARM_JOB_2: html_payloads["arm_job_page_no_ld.html"],
        }
    )

    result = arm_adapter().fetch_scan(arm, client)

    fallback = result.jobs[1]
    assert fallback.title == "Graduate Software Engineer"
    assert fallback.posted_at == date(2026, 10, 18)
    assert fallback.location_raw == ""
    assert fallback.location_normalized.ambiguous is True
    assert result.status is ScanStatus.SUCCESS
    assert "had no JobPosting JSON-LD" in (result.error_summary or "")


def test_page_without_title_or_heading_is_skipped(wise, fake_html_http) -> None:
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{WISE_JOB_1}</loc><lastmod>2026-10-21</lastmod></url>"
        "</urlset>"
    )
    client = fake_html_http({WISE_INDEX: sitemap, WISE_JOB_1: "<html><body></body></html>"})

    result = wise_adapter().fetch_scan(wise, client)

    assert result.status is ScanStatus.PARTIAL
    assert [skip.reason for skip in result.skipped] == ["missing_title"]


def test_detail_request_failure_makes_the_scan_partial(wise, fake_html_http) -> None:
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{WISE_JOB_1}</loc><lastmod>2026-10-21</lastmod></url>"
        "</urlset>"
    )
    client = fake_html_http(
        {
            WISE_INDEX: sitemap,
            WISE_JOB_1: HttpError("request failed after retries", status=429, retryable=True),
        }
    )

    result = wise_adapter().fetch_scan(wise, client)

    assert result.status is ScanStatus.PARTIAL
    assert [skip.reason for skip in result.skipped] == ["detail_request_failed"]
    assert result.jobs == ()


# incremental refresh -----------------------------------------------------


def test_unchanged_revision_skips_the_detail_fetch(wise, html_payloads, fake_html_http) -> None:
    client = fake_html_http(wise_documents(html_payloads))

    result = wise_adapter().fetch_scan(wise, client, known={"4821": "2026-10-21"})

    assert WISE_JOB_1 not in client.gets
    assert result.status is ScanStatus.SUCCESS
    assert result.observed_ids == ("4821",)
    assert [job.source_job_id for job in result.jobs] == ["4822", "4823"]
    assert result.records_seen == 3
    assert result.records_parsed == 3


def test_changed_revision_refetches_the_page(wise, html_payloads, fake_html_http) -> None:
    client = fake_html_http(wise_documents(html_payloads))

    result = wise_adapter().fetch_scan(wise, client, known={"4821": "2026-10-01"})

    assert WISE_JOB_1 in client.gets
    assert result.observed_ids == ()


def test_stored_role_without_a_revision_is_refetched(wise, html_payloads, fake_html_http) -> None:
    client = fake_html_http(wise_documents(html_payloads))

    result = wise_adapter().fetch_scan(wise, client, known={"4821": None})

    assert WISE_JOB_1 in client.gets
    assert result.observed_ids == ()


def test_entry_without_lastmod_is_always_fetched(wise, fake_html_http) -> None:
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{WISE_JOB_1}</loc></url>"
        "</urlset>"
    )
    client = fake_html_http(
        {WISE_INDEX: sitemap, WISE_JOB_1: "<html><h1>Graduate Analyst</h1></html>"}
    )

    result = wise_adapter().fetch_scan(wise, client, known={"4821": "2026-10-21"})

    assert WISE_JOB_1 in client.gets
    assert result.observed_ids == ()


def test_detail_fetch_cap_is_reported_and_bounded(wise, html_payloads, fake_html_http) -> None:
    client = fake_html_http(wise_documents(html_payloads))

    result = wise_adapter(max_detail_fetches=1).fetch_scan(wise, client)

    assert result.status is ScanStatus.PARTIAL
    assert "detail fetches capped at 1" in (result.error_summary or "")
    assert len(result.jobs) == 1
    assert set(result.observed_ids) == {"4822", "4823"}
    assert client.gets == [WISE_INDEX, WISE_VACANCIES, WISE_JOB_1]


def test_detail_fetch_cap_and_delay_can_come_from_source_options(
    wise, html_payloads, fake_html_http
) -> None:
    delays: list[float] = []
    configured = type(wise)(
        id="wise",
        employer="Wise",
        adapter="sitemap_html",
        options={"profile": "wise", "max_detail_fetches": 2, "request_delay_seconds": 0.25},
    )
    client = fake_html_http(wise_documents(html_payloads))

    result = SitemapHtmlAdapter(profile="wise", request_delay=0.0, sleep=delays.append).fetch_scan(
        configured, client
    )

    assert delays == [0.25]
    assert result.status is ScanStatus.PARTIAL
    assert "detail fetches capped at 2" in (result.error_summary or "")
    assert len(result.jobs) == 2


def test_detail_requests_are_spaced_by_the_configured_delay(
    wise, html_payloads, fake_html_http
) -> None:
    delays: list[float] = []
    adapter = SitemapHtmlAdapter(profile="wise", request_delay=0.5, sleep=delays.append)

    adapter.fetch_scan(wise, fake_html_http(wise_documents(html_payloads)))

    assert delays == [0.5, 0.5]


# scanner integration -----------------------------------------------------


def test_unchanged_roles_survive_a_second_scan_without_refetch(
    store, wise, html_payloads, fake_html_http
) -> None:
    first_client = fake_html_http(wise_documents(html_payloads))
    first = run_scan(store, wise_adapter(), wise, first_client, rules=DEFAULT_RULES)

    second_client = fake_html_http(wise_documents(html_payloads))
    second = run_scan(store, wise_adapter(), wise, second_client, rules=DEFAULT_RULES)

    assert first.is_baseline is True
    assert second.is_baseline is False
    assert second_client.gets == [WISE_INDEX, WISE_VACANCIES]
    assert second.observed_only_jobs == 3
    assert second.closed_jobs == 0
    assert second.candidates == ()
    assert store.get_job("wise", "4821").status is JobStatus.OPEN
    assert store.get_job("wise", "4821").job.title == "Graduate Data Scientist"


def test_a_new_role_alerts_once_and_a_changed_role_updates_quietly(
    store, wise, html_payloads, fake_html_http
) -> None:
    run_scan(
        store,
        wise_adapter(),
        wise,
        fake_html_http(wise_documents(html_payloads)),
        rules=DEFAULT_RULES,
    )

    updated_sitemap = html_payloads["wise_vacancies_sitemap.xml"].replace(
        "</urlset>",
        "  <url>\n"
        "    <loc>https://wise.jobs/jobs/graduate-product-analyst-london-jid-4824</loc>\n"
        "    <lastmod>2026-10-22</lastmod>\n"
        "  </url>\n"
        "</urlset>",
    )
    new_job_url = "https://wise.jobs/jobs/graduate-product-analyst-london-jid-4824"
    documents = wise_documents(
        html_payloads,
        **{WISE_VACANCIES: updated_sitemap, new_job_url: html_payloads["wise_job_page.html"]},
    )
    client = fake_html_http(documents)

    outcome = run_scan(store, wise_adapter(), wise, client, rules=DEFAULT_RULES)

    assert client.gets == [WISE_INDEX, WISE_VACANCIES, new_job_url]
    assert outcome.observed_only_jobs == 3
    assert [candidate.title for candidate in outcome.candidates] == ["Graduate Data Scientist"]
    assert outcome.closed_jobs == 0


def test_a_removed_role_closes_only_after_two_complete_scans(
    store, wise, html_payloads, fake_html_http
) -> None:
    run_scan(
        store,
        wise_adapter(),
        wise,
        fake_html_http(wise_documents(html_payloads)),
        rules=DEFAULT_RULES,
    )

    trimmed = html_payloads["wise_vacancies_sitemap.xml"].replace(
        f"  <url>\n    <loc>{WISE_JOB_3}</loc>\n    <lastmod>2026-09-30</lastmod>\n  </url>\n",
        "",
    )
    assert WISE_JOB_3 not in trimmed

    first = run_scan(
        store,
        wise_adapter(),
        wise,
        fake_html_http(wise_documents(html_payloads, **{WISE_VACANCIES: trimmed})),
        rules=DEFAULT_RULES,
    )

    assert first.closed_jobs == 0
    assert store.get_job("wise", "4823").consecutive_misses == 1
    assert store.get_job("wise", "4823").status is JobStatus.OPEN

    second = run_scan(
        store,
        wise_adapter(),
        wise,
        fake_html_http(wise_documents(html_payloads, **{WISE_VACANCIES: trimmed})),
        rules=DEFAULT_RULES,
    )

    assert second.closed_jobs == 1
    assert store.get_job("wise", "4823").status is JobStatus.CLOSED
