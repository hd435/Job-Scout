"""CLI coverage for the M4 sitemap scan path and the unchanged-role line."""

from __future__ import annotations

from pathlib import Path

import pytest

import jobscout.cli as cli
from jobscout.config import ConfigError, SourceConfig, load_config

FIXTURES = Path(__file__).parent / "fixtures"
WISE_INDEX = "https://wise.jobs/sitemap.xml"
WISE_VACANCIES = "https://wise.jobs/vacanciessitemap.xml"


class StaticHtmlClient:
    """Serves the sitemap and page bodies used elsewhere, without network access."""

    def __init__(self) -> None:
        self.gets: list[str] = []

    def get_text(self, url: str) -> str:
        self.gets.append(url)
        documents = {
            WISE_INDEX: (FIXTURES / "wise_sitemap_index.xml").read_text(encoding="utf-8"),
            WISE_VACANCIES: (FIXTURES / "wise_vacancies_sitemap.xml").read_text(encoding="utf-8"),
        }
        if url in documents:
            return documents[url]
        return (FIXTURES / "wise_job_page.html").read_text(encoding="utf-8")


@pytest.fixture
def wise_http(monkeypatch) -> StaticHtmlClient:
    client = StaticHtmlClient()
    monkeypatch.setattr(cli, "UrllibHttpClient", lambda: client)
    return client


# The same shape the shipped Wise default uses, so the fixture documents the real contract.
WISE_OPTIONS = """
sitemap_urls = ["https://wise.jobs/sitemap.xml"]
sitemap_child_pattern = "vacanc"
job_url_pattern = "^https://wise\\\\.jobs/jobs/[A-Za-z0-9\\\\-]+?-jid-(?P<id>\\\\d+)/?$"
request_delay_seconds = 0
"""


def write_config(tmp_path: Path, *, source_id: str, options: str = WISE_OPTIONS) -> Path:
    database = (tmp_path / "cli.db").as_posix()
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join(
            [
                "[jobscout]",
                f'database = "{database}"',
                "",
                "[[sources]]",
                f'id = "{source_id}"',
                'employer = "Synthetic"',
                'adapter = "sitemap_html"',
                "enabled = true",
                "",
                "[sources.options]",
                options.strip(),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config


def test_scan_reports_unchanged_roles_without_refetching_them(
    tmp_path: Path, wise_http: StaticHtmlClient, capsys
) -> None:
    config = write_config(tmp_path, source_id="wise")
    command = ["scan", "--source", "wise", "--dry-run", "--config", str(config)]

    assert cli.main(command) == 0
    capsys.readouterr()
    wise_http.gets.clear()

    assert cli.main(command) == 0
    out = capsys.readouterr().out

    assert "Unchanged roles: 3 observed without re-reading details" in out
    assert wise_http.gets == [WISE_INDEX, WISE_VACANCIES]


def test_a_url_pattern_that_matches_nothing_fails_loudly(
    tmp_path: Path, wise_http: StaticHtmlClient, capsys
) -> None:
    options = WISE_OPTIONS.replace("-jid-", "-reference-")
    config = write_config(tmp_path, source_id="mystery", options=options)

    exit_code = cli.main(["scan", "--source", "mystery", "--dry-run", "--config", str(config)])
    out = capsys.readouterr().out

    assert exit_code == 1, "a scan that finds nothing is partial (degraded), not a crash"
    assert "matched options.job_url_pattern" in out
    assert wise_http.gets == [WISE_INDEX, WISE_VACANCIES]


def test_sitemap_source_options_are_configurable(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, source_id="wise"))

    source: SourceConfig = config.sources[0]
    assert source.options["sitemap_urls"] == ["https://wise.jobs/sitemap.xml"]
    assert source.options["request_delay_seconds"] == 0


def test_an_option_the_adapter_does_not_read_is_rejected_at_load(tmp_path: Path) -> None:
    options = WISE_OPTIONS + 'jobb_url_pattern = "typo"\n'
    config = write_config(tmp_path, source_id="wise", options=options)

    with pytest.raises(ConfigError, match="unknown option"):
        load_config(config)


def test_a_missing_required_option_is_rejected_at_load(tmp_path: Path) -> None:
    config = write_config(
        tmp_path, source_id="wise", options='sitemap_urls = ["https://x.test/s.xml"]'
    )

    with pytest.raises(ConfigError, match="needs job_url_pattern"):
        load_config(config)


# a brand-new employer, added with configuration only ----------------------

EXAMPLE_SITEMAP = "https://careers.example.test/sitemap-jobs.xml"
EXAMPLE_JOB = "https://careers.example.test/jobs/graduate-data-analyst-7012"
EXAMPLE_PAGE = """<!doctype html><html><head><title>Graduate Data Analyst</title>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "JobPosting",
 "title": "Graduate Data Analyst",
 "datePosted": "2026-10-20",
 "jobLocation": {"@type": "Place",
   "address": {"addressLocality": "London", "addressCountry": "GB"}}}
</script></head><body><h1>Graduate Data Analyst</h1></body></html>"""
EXAMPLE_SITEMAP_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{EXAMPLE_JOB}</loc><lastmod>2026-10-20</lastmod></url>
</urlset>"""


class ExampleClient:
    """A careers site the code has never seen: one urlset, one JSON-LD job page."""

    def __init__(self) -> None:
        self.gets: list[str] = []

    def get_text(self, url: str) -> str:
        self.gets.append(url)
        if url == EXAMPLE_SITEMAP:
            return EXAMPLE_SITEMAP_XML
        if url == EXAMPLE_JOB:
            return EXAMPLE_PAGE
        raise AssertionError(f"unexpected request to {url}")


def test_a_new_employer_is_added_by_configuration_alone(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """V05-2: an unknown JSON-LD careers site, with no Python change."""
    client = ExampleClient()
    monkeypatch.setattr(cli, "UrllibHttpClient", lambda: client)
    options = (
        f'sitemap_urls = ["{EXAMPLE_SITEMAP}"]'
        "\n"
        "job_url_pattern = '^https://careers\\.example\\.test/jobs/[a-z0-9-]+-(?P<id>\\d+)$'"
    )
    config = write_config(tmp_path, source_id="example", options=options)

    exit_code = cli.main(["scan", "--source", "example", "--dry-run", "--config", str(config)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "records: seen=1 parsed=1 skipped=0" in out
    assert "Baseline established: 1 role(s)" in out
    assert client.gets == [EXAMPLE_SITEMAP, EXAMPLE_JOB]
