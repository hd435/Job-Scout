"""CLI coverage for the M4 sitemap scan path and the unchanged-role line."""

from __future__ import annotations

from pathlib import Path

import pytest

import jobscout.cli as cli
from jobscout.config import SourceConfig, load_config

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


def write_config(tmp_path: Path, *, source_id: str, profile: str) -> Path:
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
                f'profile = "{profile}"',
                "request_delay_seconds = 0",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config


def test_scan_reports_unchanged_roles_without_refetching_them(
    tmp_path: Path, wise_http: StaticHtmlClient, capsys
) -> None:
    config = write_config(tmp_path, source_id="wise", profile="wise")
    command = ["scan", "--source", "wise", "--dry-run", "--config", str(config)]

    assert cli.main(command) == 0
    capsys.readouterr()
    wise_http.gets.clear()

    assert cli.main(command) == 0
    out = capsys.readouterr().out

    assert "Unchanged roles: 3 observed without re-reading details" in out
    assert wise_http.gets == [WISE_INDEX, WISE_VACANCIES]


def test_scan_of_an_unknown_sitemap_profile_fails_loudly(
    tmp_path: Path, wise_http: StaticHtmlClient, capsys
) -> None:
    config = write_config(tmp_path, source_id="mystery", profile="mystery")

    exit_code = cli.main(["scan", "--source", "mystery", "--dry-run", "--config", str(config)])
    out = capsys.readouterr().out

    assert exit_code == 2
    assert "unknown sitemap profile 'mystery'" in out
    assert wise_http.gets == []


def test_sitemap_source_options_are_configurable(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, source_id="wise", profile="wise"))

    source: SourceConfig = config.sources[0]
    assert source.options["profile"] == "wise"
    assert source.options["request_delay_seconds"] == 0
