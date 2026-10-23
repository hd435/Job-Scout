"""CLI coverage for `jobscout schedule`. Offline: the HTTP client is replaced."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import jobscout.cli as cli

FIXTURES = Path(__file__).parent / "fixtures"
PALANTIR_FIXTURE = FIXTURES / "palantir_jobs.json"


class FakeBoardClient:
    """Serves the recorded Palantir payload for any GET; no network access."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.payload: Any = json.loads(PALANTIR_FIXTURE.read_text(encoding="utf-8"))

    def get_json(self, url: str) -> Any:
        self.requests.append(url)
        return self.payload


@pytest.fixture
def board(monkeypatch) -> FakeBoardClient:
    client = FakeBoardClient()
    monkeypatch.setattr(cli, "UrllibHttpClient", lambda **_kwargs: client)
    return client


@pytest.fixture
def config(tmp_path: Path) -> Path:
    """A single-source schedule: this test must not fan out over all six sources."""
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                "[jobscout]",
                f'database = "{(tmp_path / "schedule.db").as_posix()}"',
                "",
                "[[sources]]",
                'id = "palantir"',
                'employer = "Palantir Technologies"',
                'adapter = "lever"',
                "enabled = true",
                "cadence_minutes = 30",
                "",
                "[sources.options]",
                'site = "palantir"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_schedule_once_runs_the_due_source_and_reports_next_run(
    config: Path, board, capsys
) -> None:
    exit_code = cli.main(["schedule", "--once", "--config", str(config)])
    out = capsys.readouterr().out

    assert exit_code == cli.EXIT_OK
    assert "Telegram is not configured" in out
    assert "palantir: success" in out
    assert "alerts=0" in out  # baseline: no individual alerts
    assert "next run:" in out
    assert len(board.requests) == 1


def test_second_pass_has_nothing_due(config: Path, board, capsys) -> None:
    cli.main(["schedule", "--once", "--config", str(config)])
    capsys.readouterr()

    cli.main(["schedule", "--once", "--config", str(config)])
    out = capsys.readouterr().out

    assert "No source is due." in out
    assert len(board.requests) == 1


def test_bounded_loop_runs_the_requested_number_of_passes(
    config: Path, board, capsys, monkeypatch
) -> None:
    naps: list[float] = []
    monkeypatch.setattr(cli.time, "sleep", naps.append)

    exit_code = cli.main(["schedule", "--max-iterations", "2", "--config", str(config)])
    out = capsys.readouterr().out

    assert exit_code == cli.EXIT_OK
    assert out.count("palantir: success") == 1
    assert "No source is due." in out
    assert naps == [60.0, 60.0]  # waits are capped, never a full cadence
    assert len(board.requests) == 1


def test_status_reports_the_next_run(config: Path, board, capsys) -> None:
    cli.main(["schedule", "--once", "--config", str(config)])
    capsys.readouterr()

    cli.main(["status", "--source", "palantir", "--config", str(config)])
    out = capsys.readouterr().out

    assert "Next run:" in out
    assert "Failure notice" not in out
