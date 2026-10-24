"""Operational visibility and coverage measurement: what an operator needs at 100 sources."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jobscout.cli as cli
from jobscout.cli import main
from jobscout.config import AppConfig, SourceConfig
from jobscout.models import ScanResult, ScanStatus
from jobscout.report import build_trial_report, format_trial_report
from jobscout.scheduler import Scheduler
from jobscout.sources.base import SourceAdapter
from jobscout.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures"
PALANTIR_FIXTURE = FIXTURES / "palantir_jobs.json"


PALANTIR_SOURCE = SourceConfig(
    id="palantir",
    employer="Palantir Technologies",
    adapter="lever",
    options={"site": "palantir"},
)


class FailureResult:
    """An HTTP client whose every request fails, so the scan records one failure."""

    def __init__(self, message: str) -> None:
        self.message = message

    def get_json(self, url: str):
        from jobscout.http import HttpError

        raise HttpError(self.message, status=503, retryable=True)


class FixedAdapter(SourceAdapter):
    """A healthy adapter that reports an empty, complete scan."""

    adapter_id = "lever"

    def fetch_scan(self, config, http_client, *, known=None) -> ScanResult:
        return ScanResult(
            source_id=config.id,
            started_at="2026-10-24T10:00:00Z",
            finished_at="2026-10-24T10:00:01Z",
            status=ScanStatus.SUCCESS_EMPTY,
            records_seen=0,
            records_parsed=0,
        )


class StepClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 24, 9, 0, 0, tzinfo=UTC)

    def __call__(self) -> str:
        current = self._now
        self._now = current + timedelta(minutes=31)
        return current.strftime("%Y-%m-%dT%H:%M:%SZ")


def run_cli(*args: str) -> int:
    return main(list(args))


def write_config(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                "[jobscout]",
                f'database = "{(tmp_path / "ops.db").as_posix()}"',
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
                "[sources.options]",
                'sitemap_urls = ["https://wise.jobs/sitemap.xml"]',
                "job_url_pattern = '^https://wise.jobs/job/[a-z0-9-]+-jid-(?P<id>\\d+)/?$'",
                extra,
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_status_brief_prints_one_line_per_source(tmp_path: Path, capsys) -> None:
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

    exit_code = run_cli("status", "--brief", "--config", str(config))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "id" in out and "next run" in out and "matches" in out
    rows = [line for line in out.splitlines() if line.startswith(("palantir", "wise"))]
    assert len(rows) == 2
    assert "success/complete" in rows[0] or "success/complete" in rows[1]


def test_the_detailed_status_block_is_still_available(tmp_path: Path, capsys) -> None:
    config = write_config(tmp_path)

    run_cli("status", "--source", "palantir", "--config", str(config))
    out = capsys.readouterr().out

    assert "adapter=lever" in out
    assert "No scans recorded yet." in out


def test_sources_list_reports_never_validated(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "VALIDATION_ARTIFACT_ROOT", tmp_path / "data")
    config = write_config(tmp_path)

    run_cli("sources", "list", "--config", str(config))
    out = capsys.readouterr().out

    assert "validated" in out
    assert "never" in out
    assert "Validate a source before enabling it" in out


def test_sources_list_shows_a_validation_date(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "VALIDATION_ARTIFACT_ROOT", tmp_path / "data")
    validation = tmp_path / "data" / "validation"
    validation.mkdir(parents=True)
    (validation / "palantir-latest.json").write_text(
        json.dumps(
            {
                "source_id": "palantir",
                "adapter": "lever",
                "captured_at": "2026-10-24T09:48:00Z",
                "ok": True,
                "records_seen": 309,
            }
        ),
        encoding="utf-8",
    )
    config = write_config(tmp_path)

    run_cli("sources", "list", "--config", str(config))
    out = capsys.readouterr().out

    row = next(line for line in out.splitlines() if line.startswith("palantir"))
    assert "10-24 09:48 ok" in row
    assert "Validated: 1 ok" in out


def test_a_failed_validation_is_visible(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "VALIDATION_ARTIFACT_ROOT", tmp_path / "data")
    validation = tmp_path / "data" / "validation"
    validation.mkdir(parents=True)
    (validation / "wise-latest.json").write_text(
        json.dumps({"source_id": "wise", "captured_at": "2026-10-24T09:00:00Z", "ok": False}),
        encoding="utf-8",
    )
    config = write_config(tmp_path)

    run_cli("sources", "list", "--config", str(config))
    out = capsys.readouterr().out

    row = next(line for line in out.splitlines() if line.startswith("wise"))
    assert "FAILED" in row


def test_the_report_states_what_each_source_contributes(store, scan, record) -> None:
    scan(
        [
            record("a", "Graduate Software Engineer"),
            record("b", "Data Analyst"),
            record("c", "Senior Software Engineer"),
        ]
    )

    report = build_trial_report(store, now="2026-10-22T12:00:00Z", days=1)
    palantir = next(source for source in report.sources if source.source_id == "palantir")

    assert palantir.open_roles == 3
    assert palantir.matches == 1
    assert palantir.reviews == 1
    text = format_trial_report(report)
    assert "coverage 3 role(s) stored, 1 matching, 1 in review" in text
    assert "Coverage in total: 3 role(s), 1 matching" in text


def test_the_report_names_sources_that_contribute_nothing(store, scan, record) -> None:
    scan([record("a", "Senior Software Engineer")])

    report = build_trial_report(store, now="2026-10-22T12:00:00Z", days=1)
    text = format_trial_report(report)

    assert "no current matches" in text
    assert "palantir" in text.split("no current matches")[1]


def test_coverage_counts_are_per_source(store, scan, record) -> None:
    scan([record("a", "Graduate Software Engineer")], config=_other_source(store))

    report = build_trial_report(store, now="2026-10-22T12:00:00Z", days=1)
    other = next(source for source in report.sources if source.source_id == "other")

    assert other.matches == 1
    assert other.open_roles == 1


def _other_source(store: Storage):
    from jobscout.config import SourceConfig

    source = SourceConfig(
        id="other", employer="Other Ltd", adapter="lever", options={"site": "other"}
    )
    store.ensure_source(source)
    return source


def test_brief_status_shows_only_errors_that_are_current(tmp_path: Path, capsys) -> None:
    """A recovered failure is history: at a hundred sources the column must mean "now"."""
    config = write_config(tmp_path)
    db = tmp_path / "ops.db"
    failing = FailureResult("board unreachable")

    with Storage.open(db) as storage:
        clock = StepClock()
        scheduler = Scheduler(
            storage,
            AppConfig(database_path=db, sources=(PALANTIR_SOURCE,)),
            http_client=failing,
            clock=clock,
            sleep=lambda _s: None,
        )
        scheduler.run_once()
        assert storage.get_source_state("palantir").consecutive_failures == 1

        # A complete scan ends the failure episode; the brief view must stop shouting.
        scheduler._adapter_factory = lambda _adapter: FixedAdapter()  # noqa: SLF001 - test seam
        scheduler.run_once()

    lines = _brief_lines(config)
    row = next(line for line in lines if line.startswith("palantir"))
    assert "board unreachable" not in row
    assert row.split()[2] == "0", "no consecutive failures once recovered"
    assert capsys.readouterr().err == ""


def _brief_lines(config: Path) -> list[str]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        run_cli("status", "--brief", "--config", str(config))
    return buffer.getvalue().splitlines()
