"""M5.1 bounded live-validation tests. Every test is offline; no socket is opened."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import jobscout.cli as cli
import jobscout.validate as validate
from jobscout.config import SourceConfig
from jobscout.http import HttpError
from jobscout.models import ScanResult, ScanStatus
from jobscout.validate import compare_payloads, run_live_check

FIXTURES = Path(__file__).parent / "fixtures"
PALANTIR_FIXTURE = FIXTURES / "palantir_jobs.json"
CAPTURED_AT = "2026-10-23T09:00:00Z"


@pytest.fixture
def palantir_payload() -> list[dict]:
    return json.loads(PALANTIR_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def palantir() -> SourceConfig:
    return SourceConfig(
        id="palantir",
        employer="Palantir Technologies",
        adapter="lever",
        options={"site": "palantir"},
    )


@pytest.fixture
def clean_live_payload(palantir_payload) -> list[dict]:
    """A realistic live shape: the fixture's records that carry a valid epoch timestamp."""
    return [item for item in palantir_payload if isinstance(item.get("createdAt"), int)]


def record(job_id: str = "abc-1") -> dict:
    return {
        "id": job_id,
        "text": "Graduate Software Engineer",
        "hostedUrl": f"https://jobs.lever.co/palantir/{job_id}",
        "categories": {"location": "London, United Kingdom", "allLocations": ["London"]},
        "createdAt": 1761000000000,
        "descriptionPlain": "A role description.",
        "workplaceType": "hybrid",
        "salaryRange": {
            "min": 50000,
            "max": 60000,
            "currency": "GBP",
            "interval": "per-year-salary",
        },
    }


# compare_payloads ---------------------------------------------------------


def test_uniform_payload_has_no_drift() -> None:
    payload = [record("abc-1"), record("abc-2")]

    comparison = compare_payloads(payload, payload)

    assert comparison.ok is True
    assert comparison.failures == ()
    assert comparison.warnings == ()
    assert comparison.live_records == comparison.fixture_records == 2


def test_committed_fixture_self_comparison_surfaces_its_known_quirk(palantir_payload) -> None:
    """The recorded Palantir fixture is synthetic: one record carries a non-date string."""
    comparison = compare_payloads(palantir_payload, palantir_payload)

    assert comparison.ok is False
    assert [item for item in comparison.failures if "createdAt" in item]
    assert comparison.live_records == comparison.fixture_records == len(palantir_payload)


def test_new_field_is_reported_without_failing() -> None:
    fixture = [record("abc-1"), record("abc-2")]
    live = [dict(record_value, brandNewField="x") for record_value in fixture]

    comparison = compare_payloads(live, fixture)

    assert comparison.ok is True
    assert any("new field in live payload: brandNewField" in item for item in comparison.warnings)


def test_missing_required_field_fails() -> None:
    fixture = [record()]
    live = [record()]
    live[0].pop("hostedUrl")

    comparison = compare_payloads(live, fixture)

    assert comparison.ok is False
    assert any("required field missing" in item for item in comparison.failures)


def test_missing_optional_field_is_a_warning() -> None:
    fixture = [record("abc-1")]
    live = [record("abc-1")]
    live[0].pop("workplaceType")

    comparison = compare_payloads(live, fixture)

    assert comparison.ok is True
    assert any("no longer present: workplaceType" in item for item in comparison.warnings)


def test_type_change_fails() -> None:
    fixture = [record()]
    live = [record()]
    live[0]["createdAt"] = "1761000000000"

    comparison = compare_payloads(live, fixture)

    assert comparison.ok is False
    assert any("unexpected type" in item and "createdAt" in item for item in comparison.failures)


def test_nested_field_type_change_fails() -> None:
    fixture = [record()]
    live = [record()]
    live[0]["categories"]["location"] = 42

    comparison = compare_payloads(live, fixture)

    assert comparison.ok is False
    assert any("categories.location" in item for item in comparison.failures)


@pytest.mark.parametrize(
    ("live", "expected"),
    [
        ({"not": "a list"}, "live payload is not a JSON list"),
        ([], "live payload contains no job records"),
        (["junk"], "live record(s) are not objects"),
    ],
)
def test_malformed_live_payloads_fail(live, expected) -> None:
    comparison = compare_payloads(live, [record()])

    assert comparison.ok is False
    assert any(expected in item for item in comparison.failures)


def test_non_list_fixture_is_reported() -> None:
    comparison = compare_payloads([record()], {"jobs": []})

    assert comparison.ok is False
    assert any("fixture is not a JSON list" in item for item in comparison.failures)


# build_sample -------------------------------------------------------------


def test_sample_is_capped_and_trimmed() -> None:
    payload = [
        dict(record(f"id-{index}"), descriptionPlain="x" * 500, lists=[{"text": "section"}])
        for index in range(8)
    ]

    sample = validate.build_sample(
        payload, source_id="palantir", captured_at=CAPTURED_AT, max_records=2
    )

    assert sample["total_records"] == 8
    assert sample["sample_truncated_to"] == 2
    assert len(sample["records"]) == 2
    assert len(sample["records"][0]["descriptionPlain"]) == validate.DESCRIPTION_LIMIT
    assert sample["records"][0]["lists"] == {"__omitted__": 1}


# run_live_check -----------------------------------------------------------


def test_successful_check_records_one_request_and_writes_artifacts(
    tmp_path: Path, palantir, palantir_payload, clean_live_payload, fake_http
) -> None:
    client = fake_http(clean_live_payload)

    outcome = run_live_check(
        palantir,
        fixture_payload=palantir_payload,
        client=client,
        sample_dir=tmp_path / "validation",
        max_records=3,
        now=CAPTURED_AT,
    )

    assert outcome.ok is True
    assert client.requests == ["https://api.lever.co/v0/postings/palantir?mode=json"]
    assert outcome.scan is not None and outcome.scan.records_parsed == len(clean_live_payload)
    assert outcome.sample_path is not None and outcome.sample_path.exists()
    sample = json.loads(outcome.sample_path.read_text(encoding="utf-8"))
    assert len(sample["records"]) == 3
    report = outcome.report_path.read_text(encoding="utf-8")
    assert "Result: PASS" in report
    assert "skipped=0" in report


def test_request_failure_writes_no_artifact(
    tmp_path: Path, palantir, palantir_payload, fake_http
) -> None:
    client = fake_http(error=HttpError("request failed", status=429, retryable=True))
    sample_dir = tmp_path / "validation"

    outcome = run_live_check(
        palantir,
        fixture_payload=palantir_payload,
        client=client,
        sample_dir=sample_dir,
        now=CAPTURED_AT,
    )

    assert outcome.ok is False
    assert outcome.error is not None and "request failed" in outcome.error
    assert not sample_dir.exists()


def test_shape_drift_fails_but_keeps_the_evidence(
    tmp_path: Path, palantir, palantir_payload, fake_http
) -> None:
    drifted = [dict(palantir_payload[0])]
    drifted[0]["createdAt"] = "not-an-epoch"
    client = fake_http(drifted)

    outcome = run_live_check(
        palantir,
        fixture_payload=palantir_payload,
        client=client,
        sample_dir=tmp_path / "validation",
        now=CAPTURED_AT,
    )

    assert outcome.ok is False
    assert any("createdAt" in item for item in outcome.comparison.failures)
    assert outcome.report_path is not None and outcome.report_path.exists()
    assert "Result: FAIL" in outcome.report_path.read_text(encoding="utf-8")


def test_more_than_one_request_is_a_budget_violation(
    monkeypatch, palantir, palantir_payload, fake_http
) -> None:
    class ChattyAdapter:
        adapter_id = "chatty"

        def fetch_scan(self, config, http_client, *, known=None):
            http_client.get_json("https://example.test/one")
            http_client.get_json("https://example.test/two")
            return ScanResult(
                source_id=config.id,
                started_at=CAPTURED_AT,
                finished_at=CAPTURED_AT,
                status=ScanStatus.SUCCESS,
                records_seen=1,
                records_parsed=1,
            )

    monkeypatch.setattr(validate, "get_adapter", lambda _adapter_id: ChattyAdapter())

    outcome = run_live_check(palantir, fixture_payload=palantir_payload, client=fake_http([]))

    assert outcome.ok is False
    assert outcome.error is not None and "expected exactly 1 request" in outcome.error


# CLI wiring ---------------------------------------------------------------


def test_cli_refuses_without_confirm_live(monkeypatch, capsys) -> None:
    def explode(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("a live client must not be constructed without --confirm-live")

    monkeypatch.setattr(cli, "UrllibHttpClient", explode)

    assert cli.main(["validate"]) == cli.EXIT_FAILED
    assert "--confirm-live" in capsys.readouterr().err


def test_cli_rejects_sources_outside_the_m5_1_scope(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "UrllibHttpClient", lambda **_kwargs: pytest.fail("no client"))

    assert cli.main(["validate", "--confirm-live", "--source", "wise"]) == cli.EXIT_FAILED
    assert "M5.1 scope" in capsys.readouterr().err


def test_cli_runs_the_check_without_opening_a_database(
    tmp_path: Path, monkeypatch, capsys, palantir_payload, clean_live_payload, fake_http
) -> None:
    monkeypatch.setattr(cli, "UrllibHttpClient", lambda **_kwargs: fake_http(clean_live_payload))

    def explode(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("validate must not open the database")

    monkeypatch.setattr(cli.Storage, "open", classmethod(explode))
    sample_dir = tmp_path / "validation"

    exit_code = cli.main(
        ["validate", "--confirm-live", "--sample-out", str(sample_dir), "--max-records", "2"]
    )
    out = capsys.readouterr().out

    assert exit_code == cli.EXIT_OK
    assert "Result: PASS" in out
    assert "Database: not opened" in out
    assert len(list(sample_dir.glob("*.json"))) == 1
    assert not (tmp_path / "jobscout.db").exists()


def test_cli_returns_degraded_when_the_shape_drifted(
    tmp_path: Path, monkeypatch, capsys, palantir_payload, fake_http
) -> None:
    drifted = [dict(palantir_payload[0])]
    drifted[0].pop("hostedUrl")
    monkeypatch.setattr(cli, "UrllibHttpClient", lambda **_kwargs: fake_http(drifted))

    exit_code = cli.main(
        ["validate", "--confirm-live", "--sample-out", str(tmp_path / "validation")]
    )

    assert exit_code == cli.EXIT_DEGRADED
    assert "Result: FAIL" in capsys.readouterr().out


def test_cli_rejects_a_missing_fixture(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "UrllibHttpClient", lambda **_kwargs: pytest.fail("no client"))

    exit_code = cli.main(["validate", "--confirm-live", "--fixture", str(tmp_path / "absent.json")])

    assert exit_code == cli.EXIT_FAILED
    assert "cannot read fixture" in capsys.readouterr().err


def test_cli_rejects_a_non_positive_max_records(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "UrllibHttpClient", lambda **_kwargs: pytest.fail("no client"))

    assert cli.main(["validate", "--confirm-live", "--max-records", "0"]) == cli.EXIT_FAILED
    assert "--max-records" in capsys.readouterr().err
