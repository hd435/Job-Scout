"""`jobscout sources list`: every configured source with its adapter and state."""

from __future__ import annotations

from pathlib import Path

from jobscout.cli import main

FIXTURES = Path(__file__).parent / "fixtures"
PALANTIR_FIXTURE = FIXTURES / "palantir_jobs.json"


def run_cli(*args: str) -> int:
    return main(list(args))


def write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                "[jobscout]",
                f'database = "{(tmp_path / "sources.db").as_posix()}"',
                "",
                "[[sources]]",
                'id = "palantir"',
                'employer = "Palantir Technologies"',
                'adapter = "lever"',
                "[sources.options]",
                'site = "palantir"',
                "",
                "[[sources]]",
                'id = "wise"',
                'employer = "Wise"',
                'adapter = "sitemap_html"',
                "enabled = false",
                "[sources.options]",
                'sitemap_urls = ["https://wise.jobs/sitemap.xml"]',
                "job_url_pattern = '^https://wise.jobs/job/[a-z0-9-]+-jid-(?P<id>\\d+)/?$'",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_list_before_any_scan_reports_unknown_state(tmp_path: Path, capsys) -> None:
    config = write_config(tmp_path)

    exit_code = run_cli("sources", "list", "--config", str(config))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "Sources: 2 configured, 1 enabled" in out
    assert "not created yet" in out
    assert "enabled, not yet scanned" in out
    assert "disabled" in out


def test_list_shows_the_adapter_and_the_scan_state(tmp_path: Path, capsys) -> None:
    config = write_config(tmp_path)
    run_cli(
        "scan",
        "--source",
        "palantir",
        "--dry-run",
        "--config",
        str(config),
        "--fixture",
        str(PALANTIR_FIXTURE),
    )
    capsys.readouterr()

    exit_code = run_cli("sources", "list", "--config", str(config))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "lever" in out and "sitemap_html" in out
    assert "success/complete" in out
    assert "(+" in out and "review)" in out


def test_list_counts_matching_and_review_roles_after_a_baseline(tmp_path: Path, capsys) -> None:
    config = write_config(tmp_path)
    run_cli(
        "scan",
        "--source",
        "palantir",
        "--dry-run",
        "--config",
        str(config),
        "--fixture",
        str(PALANTIR_FIXTURE),
    )
    capsys.readouterr()

    run_cli("sources", "list", "--config", str(config))
    out = capsys.readouterr().out

    line = next(row for row in out.splitlines() if row.startswith("palantir"))
    assert "success/complete" in line
    assert "(+" in line and "review)" in line


def test_a_duplicate_employer_warning_reaches_the_operator(tmp_path: Path, capsys) -> None:
    path = tmp_path / "dupes.toml"
    path.write_text(
        "\n".join(
            [
                "[[sources]]",
                'id = "monzo"',
                'employer = "Monzo"',
                'adapter = "greenhouse"',
                "[sources.options]",
                'board = "monzo"',
                "",
                "[[sources]]",
                'id = "monzo_two"',
                'employer = "Monzo"',
                'adapter = "greenhouse"',
                "[sources.options]",
                'board = "monzo-two"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    run_cli("sources", "list", "--config", str(path))
    captured = capsys.readouterr()

    assert "warning: sources 'monzo' and 'monzo_two' both monitor" in captured.err


def test_the_payload_of_a_scan_is_not_needed_to_list_sources(tmp_path: Path, capsys) -> None:
    """`sources list` is read-only: it must not make a network request or scan anything."""
    config = write_config(tmp_path)

    run_cli("sources", "list", "--config", str(config))

    out = capsys.readouterr().out
    assert "enabled, not yet scanned" in out
    assert not (tmp_path / "sources.db").exists()
