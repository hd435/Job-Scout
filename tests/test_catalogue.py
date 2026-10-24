"""The source catalogue: employers as files, and the precedence that makes it predictable."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobscout.config import (
    DEFAULT_SOURCES,
    ConfigError,
    bundled_catalogue,
    load_config,
)
from jobscout.source_options import missing_options, unknown_options

RESEARCHED = {"palantir", "barclays", "lloyds_graduate", "monzo", "wise", "arm"}


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def write_catalogue(tmp_path: Path, files: dict[str, str], *, name: str = "sources") -> Path:
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    for filename, content in files.items():
        (directory / filename).write_text(content, encoding="utf-8")
    return directory


GREENHOUSE_SOURCE = """
id = "example"
employer = "Example Ltd"
adapter = "greenhouse"
cadence_minutes = 45

[options]
board = "example"
uk_employer = true
"""

SITEMAP_SOURCE = """
id = "example_sitemap"
employer = "Example Sitemap Ltd"
adapter = "sitemap_html"

[options]
sitemap_urls = ["https://example.test/sitemap-jobs.xml"]
job_url_pattern = '^https://example\\.test/jobs/[a-z0-9-]+-(?P<id>\\d+)$'
"""


# the bundled catalogue -----------------------------------------------------


def test_the_researched_employers_are_catalogue_files_not_python() -> None:
    catalogue = bundled_catalogue()

    assert {source.id for source in catalogue} == RESEARCHED
    adapters = {source.id: source.adapter for source in catalogue}
    assert adapters == {
        "palantir": "lever",
        "barclays": "workday",
        "lloyds_graduate": "workday",
        "monzo": "greenhouse",
        "wise": "sitemap_html",
        "arm": "sitemap_html",
    }
    for source in catalogue:
        assert source.enabled is True, "a fresh clone scans the researched set"
        assert unknown_options(source.adapter, source.options) == ()
        assert missing_options(source.adapter, source.options) == ()


def test_the_module_level_default_is_the_bundled_catalogue() -> None:
    assert [source.id for source in DEFAULT_SOURCES] == [
        source.id for source in bundled_catalogue()
    ]


def test_every_catalogue_file_describes_exactly_one_employer(tmp_path: Path) -> None:
    write_catalogue(
        tmp_path,
        {"two.toml": '[[sources]]\nid = "a"\nemployer = "A"\nadapter = "lever"\n'},
    )
    config = write_config(tmp_path, '[jobscout]\ncatalogue = "sources"\n')

    with pytest.raises(ConfigError, match="must describe one employer"):
        load_config(config)


# adding an employer --------------------------------------------------------


def test_an_employer_is_added_by_writing_one_file(tmp_path: Path) -> None:
    write_catalogue(tmp_path, {"example.toml": GREENHOUSE_SOURCE, "sitemap.toml": SITEMAP_SOURCE})
    config = write_config(tmp_path, '[jobscout]\ncatalogue = "sources"\n')

    app = load_config(config)

    assert [source.id for source in app.sources] == ["example", "example_sitemap"]
    assert app.sources[0].cadence_minutes == 45
    assert app.sources[0].options == {"board": "example", "uk_employer": True}
    assert app.warnings == ()


def test_catalogue_files_are_read_in_filename_order(tmp_path: Path) -> None:
    write_catalogue(
        tmp_path,
        {
            "b_second.toml": GREENHOUSE_SOURCE.replace('"example"', '"second"'),
            "a_first.toml": GREENHOUSE_SOURCE.replace('"example"', '"first"'),
        },
    )
    config = write_config(tmp_path, '[jobscout]\ncatalogue = "sources"\n')

    assert [source.id for source in load_config(config).sources] == ["first", "second"]


def test_a_missing_catalogue_directory_fails_loudly(tmp_path: Path) -> None:
    config = write_config(tmp_path, '[jobscout]\ncatalogue = "absent"\n')

    with pytest.raises(ConfigError, match="catalogue directory not found"):
        load_config(config)


def test_a_bad_option_in_a_catalogue_file_names_the_file_and_key(tmp_path: Path) -> None:
    write_catalogue(
        tmp_path,
        {"example.toml": GREENHOUSE_SOURCE.replace("board =", "boardd =")},
    )
    config = write_config(tmp_path, '[jobscout]\ncatalogue = "sources"\n')

    with pytest.raises(ConfigError) as exc:
        load_config(config)

    assert "example.toml" in str(exc.value)
    assert "boardd" in str(exc.value)


def test_two_files_claiming_one_id_are_rejected(tmp_path: Path) -> None:
    write_catalogue(
        tmp_path,
        {
            "one.toml": GREENHOUSE_SOURCE,
            "two.toml": GREENHOUSE_SOURCE.replace("Example Ltd", "Example Two"),
        },
    )
    config = write_config(tmp_path, '[jobscout]\ncatalogue = "sources"\n')

    with pytest.raises(ConfigError, match="appears in both one.toml and two.toml"):
        load_config(config)


def test_a_catalogue_file_without_an_id_is_rejected(tmp_path: Path) -> None:
    write_catalogue(tmp_path, {"anon.toml": 'employer = "A"\nadapter = "lever"\n'})
    config = write_config(tmp_path, '[jobscout]\ncatalogue = "sources"\n')

    with pytest.raises(ConfigError, match="has no `id`"):
        load_config(config)


# precedence and narrowing --------------------------------------------------


def test_a_configured_catalogue_replaces_the_bundled_one(tmp_path: Path) -> None:
    write_catalogue(tmp_path, {"example.toml": GREENHOUSE_SOURCE})
    config = write_config(tmp_path, '[jobscout]\ncatalogue = "sources"\n')

    app = load_config(config)

    assert [source.id for source in app.sources] == ["example"], (
        "an operator catalogue is its own list, not an addition to the shipped six"
    )


def test_an_explicit_sources_list_still_replaces_the_catalogue(tmp_path: Path) -> None:
    write_catalogue(tmp_path, {"example.toml": GREENHOUSE_SOURCE})
    config = write_config(
        tmp_path,
        """
[jobscout]
catalogue = "sources"

[[sources]]
id = "only"
employer = "Only Ltd"
adapter = "lever"
[sources.options]
site = "only"
""",
    )

    app = load_config(config)

    assert [source.id for source in app.sources] == ["only"], (
        "`[[sources]]` keeps its pre-v0.6 meaning: it is the whole list"
    )


def test_disable_turns_catalogue_sources_off_without_editing_files(tmp_path: Path) -> None:
    write_catalogue(tmp_path, {"example.toml": GREENHOUSE_SOURCE, "sitemap.toml": SITEMAP_SOURCE})
    config = write_config(tmp_path, '[jobscout]\ncatalogue = "sources"\ndisable = ["example"]\n')

    app = load_config(config)

    assert [source.id for source in app.sources] == ["example", "example_sitemap"]
    assert [source.id for source in app.enabled_sources()] == ["example_sitemap"]


def test_disable_of_an_unknown_id_is_rejected(tmp_path: Path) -> None:
    write_catalogue(tmp_path, {"example.toml": GREENHOUSE_SOURCE})
    config = write_config(tmp_path, '[jobscout]\ncatalogue = "sources"\ndisable = ["nope"]\n')

    with pytest.raises(ConfigError, match="disable names unknown source"):
        load_config(config)


def test_disable_must_be_an_array_of_ids(tmp_path: Path) -> None:
    config = write_config(tmp_path, '[jobscout]\ndisable = "wise"\n')

    with pytest.raises(ConfigError, match="disable must be an array"):
        load_config(config)


def test_a_disabled_catalogue_source_is_not_a_duplicate_warning(tmp_path: Path) -> None:
    write_catalogue(
        tmp_path,
        {
            "one.toml": GREENHOUSE_SOURCE,
            "two.toml": GREENHOUSE_SOURCE.replace('id = "example"', 'id = "example_two"'),
        },
    )
    config = write_config(
        tmp_path, '[jobscout]\ncatalogue = "sources"\ndisable = ["example_two"]\n'
    )

    assert load_config(config).warnings == ()
