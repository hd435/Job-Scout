"""`jobscout config show` and `jobscout config validate`."""

from __future__ import annotations

from pathlib import Path

from jobscout.cli import main
from jobscout.matching import DEFAULT_EMPLOYMENT_TYPES, RULES_VERSION


def run_cli(*args: str) -> int:
    return main(list(args))


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_show_reports_the_built_in_defaults(capsys) -> None:
    exit_code = run_cli("config", "show")
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "Config: none (built-in defaults)" in out
    assert f"Rules: {RULES_VERSION} (built-in defaults)" in out
    assert "Preference fingerprint: " in out
    assert "Role families: data, software, quant, finance, consulting, business_analysis" in out
    assert f"Employment types: {', '.join(DEFAULT_EMPLOYMENT_TYPES)}" in out
    assert "Locations: mode = uk" in out
    assert "Title exclusions: none" in out


def test_show_reports_sources_and_disabled_state(tmp_path: Path, capsys) -> None:
    config = write_config(
        tmp_path,
        """
[[sources]]
id = "monzo"
employer = "Monzo"
adapter = "greenhouse"
enabled = true
[sources.options]
board = "monzo"

[[sources]]
id = "wise"
employer = "Wise"
adapter = "sitemap_html"
enabled = false
[sources.options]
sitemap_urls = ["https://example.test/sitemap.xml"]
job_url_pattern = '^https://example.test/job/(?P<id>\\d+)$'
""",
    )

    exit_code = run_cli("config", "show", "--config", str(config))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "Sources: 2 configured, 1 enabled" in out
    assert "monzo (Monzo) via greenhouse" in out
    assert "wise (Wise) via sitemap_html" in out
    assert "[disabled]" in out
    assert "board = 'monzo'" in out


def test_show_marks_a_pinned_city_the_list_does_not_know(tmp_path: Path, capsys) -> None:
    config = write_config(
        tmp_path,
        """
[preferences.locations]
mode = "pinned"
cities = ["London", "Bournemouth"]
""",
    )

    run_cli("config", "show", "--config", str(config))
    out = capsys.readouterr().out

    assert "cities: London, Bournemouth" in out
    assert "resolved through the city list: London" in out
    assert "text match only" in out and "bournemouth" in out
    assert "UK remote included: yes" in out


def test_show_describes_a_preference_only_difference(tmp_path: Path, capsys) -> None:
    config = write_config(
        tmp_path,
        """
[preferences.locations]
mode = "pinned"
cities = ["London"]
""",
    )

    run_cli("config", "show", "--config", str(config))
    out = capsys.readouterr().out

    assert "(preferences differ from the defaults)" in out
    assert "Locations: mode = pinned" in out


def test_validate_accepts_a_sound_configuration(tmp_path: Path, capsys) -> None:
    config = write_config(
        tmp_path,
        """
[preferences]
role_families = ["data", "software"]

[preferences.locations]
mode = "pinned"
cities = ["London"]
""",
    )

    exit_code = run_cli("config", "validate", "--config", str(config))
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Result: OK - the configuration loaded" in captured.out


def test_validate_reports_an_invalid_value_by_name(tmp_path: Path, capsys) -> None:
    config = write_config(
        tmp_path, '[preferences]\nemployment_types = ["graduate_scheme", "pizza"]\n'
    )

    exit_code = run_cli("config", "validate", "--config", str(config))
    err = capsys.readouterr().err

    assert exit_code == 2
    assert "invalid: invalid [preferences] configuration" in err
    assert "'pizza'" in err


def test_validate_reports_a_missing_configuration_file(tmp_path: Path, capsys) -> None:
    exit_code = run_cli("config", "validate", "--config", str(tmp_path / "absent.toml"))
    err = capsys.readouterr().err

    assert exit_code == 2
    assert "configuration file not found" in err


def test_validate_warns_about_a_duplicate_employer(tmp_path: Path, capsys) -> None:
    config = write_config(
        tmp_path,
        """
[[sources]]
id = "monzo_a"
employer = "Monzo"
adapter = "greenhouse"
[sources.options]
board = "monzo"

[[sources]]
id = "monzo_b"
employer = "Monzo"
adapter = "greenhouse"
[sources.options]
board = "monzo-careers"
""",
    )

    exit_code = run_cli("config", "validate", "--config", str(config))
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "both monitor 'Monzo'" in captured.out + captured.err
    assert "OK with 1 warning(s)" in captured.out + captured.err


def test_validate_warns_about_an_unknown_adapter(tmp_path: Path, capsys) -> None:
    config = write_config(
        tmp_path,
        """
[[sources]]
id = "mystery"
employer = "Mystery Ltd"
adapter = "carrier_pigeon"
""",
    )

    exit_code = run_cli("config", "validate", "--config", str(config))
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "carrier_pigeon" in captured.out + captured.err
    assert "OK with 1 warning(s)" in captured.out + captured.err
