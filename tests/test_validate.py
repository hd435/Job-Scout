"""M5.1 bounded live-validation tests. Every test is offline; no socket is opened."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import jobscout.cli as cli
import jobscout.validate as validate
from jobscout.config import SourceConfig
from jobscout.http import HttpError
from jobscout.models import ScanResult, ScanStatus
from jobscout.validate import compare_payloads, run_live_check

FIXTURES = Path(__file__).parent / "fixtures"
PALANTIR_FIXTURE = FIXTURES / "palantir_jobs.json"
MONZO_FIXTURE = FIXTURES / "monzo_validation.json"
LLOYDS_FIXTURE = FIXTURES / "workday_lloyds_graduate.json"
CAPTURED_AT = "2026-10-23T09:00:00Z"


@pytest.fixture
def palantir_payload() -> list[dict]:
    return json.loads(PALANTIR_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def monzo_payload() -> dict[str, Any]:
    return json.loads(MONZO_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def monzo() -> SourceConfig:
    return SourceConfig(
        id="monzo",
        employer="Monzo",
        adapter="greenhouse",
        options={"board": "monzo"},
    )


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
    assert len(sample["records"][0]["descriptionPlain"]) == validate.DESCRIPTION_LIMIT + 3
    assert sample["records"][0]["lists"] == {"__omitted__": 1}


def test_sample_bounds_nested_content() -> None:
    deep = {"level1": {"level2": {"level3": {"level4": "x" * 1000}}}}
    wide = {"values": list(range(50)), "texts": ["y" * 500 for _ in range(3)]}
    payload = [dict(record("abc-1"), nested=deep, wide=wide, many={f"k{i}": i for i in range(40)})]

    sample = validate.build_sample(
        payload, source_id="palantir", captured_at=CAPTURED_AT, max_records=1
    )
    bounded_record = sample["records"][0]

    assert bounded_record["nested"]["level1"]["level2"]["level3"] == {"__omitted__": 1}
    assert bounded_record["wide"]["values"] == {"__omitted__": 50}
    assert all(
        len(text) <= validate.DESCRIPTION_LIMIT + 3 for text in bounded_record["wide"]["texts"]
    )
    assert bounded_record["many"]["__omitted__"] == 40 - validate.MAX_DICT_ITEMS
    assert len(json.dumps(sample, ensure_ascii=False)) < 4000


def test_sample_bounds_top_level_fields_and_key_lengths() -> None:
    oversized_key = "k" * (validate.MAX_KEY_LENGTH + 50)
    big_record: dict[str, Any] = {oversized_key: "value"}
    big_record.update({f"field{index}": index for index in range(60)})
    payload = [big_record, dict(record("abc-1"), nested_key_holder={oversized_key: 1})]

    sample = validate.build_sample(
        payload, source_id="palantir", captured_at=CAPTURED_AT, max_records=2
    )
    first, second = sample["records"]

    assert len(first) == validate.MAX_RECORD_FIELDS + 1
    assert first["__omitted__"] == len(big_record) - validate.MAX_RECORD_FIELDS
    assert all(len(key) <= validate.MAX_KEY_LENGTH + 3 for key in first)
    assert any(key.endswith("...") for key in first)
    assert all(len(key) <= validate.MAX_KEY_LENGTH + 3 for key in second["nested_key_holder"])


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


def test_second_request_is_blocked_before_it_reaches_the_network(
    monkeypatch, palantir, palantir_payload
) -> None:
    """The budget is enforced at the client boundary, not after the fact."""

    class CountingClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_json(self, url: str) -> Any:
            self.calls.append(url)
            return palantir_payload

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
    client = CountingClient()

    outcome = run_live_check(palantir, fixture_payload=palantir_payload, client=client)

    assert outcome.ok is False
    assert client.calls == ["https://example.test/one"]
    assert outcome.error is not None and "request budget of 1 exceeded" in outcome.error


def test_contains_artifacts_inside_the_allowed_root_before_requesting(
    tmp_path: Path, palantir, palantir_payload
) -> None:
    class NeverCalled:
        def get_json(self, url: str) -> Any:  # pragma: no cover - must not run
            raise AssertionError("no request may be made when the artifact path is rejected")

    with pytest.raises(ValueError, match="must stay inside"):
        run_live_check(
            palantir,
            fixture_payload=palantir_payload,
            client=NeverCalled(),
            sample_dir=tmp_path / "elsewhere",
            allowed_root=tmp_path / "data",
        )


def test_traversal_out_of_the_allowed_root_is_rejected(
    tmp_path: Path, palantir, palantir_payload
) -> None:
    root = tmp_path / "data"
    root.mkdir()

    with pytest.raises(ValueError, match="must stay inside"):
        run_live_check(
            palantir,
            fixture_payload=palantir_payload,
            client=object(),  # type: ignore[arg-type]
            sample_dir=root / ".." / ".." / "escape",
            allowed_root=root,
        )


def test_artifacts_do_not_overwrite_an_earlier_run(
    tmp_path: Path, palantir, palantir_payload, clean_live_payload, fake_http
) -> None:
    sample_dir = tmp_path / "validation"

    first = run_live_check(
        palantir,
        fixture_payload=palantir_payload,
        client=fake_http(clean_live_payload),
        sample_dir=sample_dir,
        now=CAPTURED_AT,
    )
    second = run_live_check(
        palantir,
        fixture_payload=palantir_payload,
        client=fake_http(clean_live_payload),
        sample_dir=sample_dir,
        now=CAPTURED_AT,
    )

    assert first.sample_path != second.sample_path
    assert first.sample_path.exists() and second.sample_path.exists()
    assert len(list(sample_dir.glob("*.json"))) == 2


# CLI wiring ---------------------------------------------------------------


def test_cli_refuses_without_confirm_live(monkeypatch, capsys) -> None:
    def explode(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("a live client must not be constructed without --confirm-live")

    monkeypatch.setattr(cli, "UrllibHttpClient", explode)

    assert cli.main(["validate"]) == cli.EXIT_FAILED
    assert "--confirm-live" in capsys.readouterr().err


def test_cli_rejects_sources_outside_the_validation_scope(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "UrllibHttpClient", lambda **_kwargs: pytest.fail("no client"))

    assert cli.main(["validate", "--confirm-live", "--source", "wise"]) == cli.EXIT_FAILED
    assert "validation is supported for" in capsys.readouterr().err


def test_cli_runs_the_check_without_opening_a_database(
    tmp_path: Path, monkeypatch, capsys, palantir_payload, clean_live_payload, fake_http
) -> None:
    monkeypatch.setattr(cli, "UrllibHttpClient", lambda **_kwargs: fake_http(clean_live_payload))
    monkeypatch.setattr(cli, "VALIDATION_ARTIFACT_ROOT", tmp_path)

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
    monkeypatch.setattr(cli, "VALIDATION_ARTIFACT_ROOT", tmp_path)

    exit_code = cli.main(
        ["validate", "--confirm-live", "--sample-out", str(tmp_path / "validation")]
    )

    assert exit_code == cli.EXIT_DEGRADED
    assert "Result: FAIL" in capsys.readouterr().out


def test_cli_rejects_an_artifact_directory_outside_data(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The artifact path is checked before any request is made."""
    monkeypatch.setattr(cli, "UrllibHttpClient", lambda **_kwargs: pytest.fail("no client"))
    monkeypatch.setattr(cli, "VALIDATION_ARTIFACT_ROOT", tmp_path / "data")

    exit_code = cli.main(
        ["validate", "--confirm-live", "--sample-out", str(tmp_path / "elsewhere")]
    )
    err = capsys.readouterr().err

    assert exit_code == cli.EXIT_FAILED
    assert "must stay inside" in err
    assert not (tmp_path / "elsewhere").exists()


def test_cli_rejects_a_missing_fixture(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "UrllibHttpClient", lambda **_kwargs: pytest.fail("no client"))

    exit_code = cli.main(["validate", "--confirm-live", "--fixture", str(tmp_path / "absent.json")])

    assert exit_code == cli.EXIT_FAILED
    assert "cannot read fixture" in capsys.readouterr().err


def test_cli_rejects_a_non_positive_max_records(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "UrllibHttpClient", lambda **_kwargs: pytest.fail("no client"))

    assert cli.main(["validate", "--confirm-live", "--max-records", "0"]) == cli.EXIT_FAILED
    assert "--max-records" in capsys.readouterr().err


# Monzo (Greenhouse envelope) ----------------------------------------------


def test_the_monzo_profile_reads_the_jobs_array(monzo, monzo_payload) -> None:
    profile = validate.VALIDATION_PROFILES["monzo"]

    assert profile.records(monzo_payload) == monzo_payload["jobs"]
    assert profile.records({"meta": {"total": 0}}) is None
    assert profile.missing_records_note({"meta": {"total": 0}}) == (
        "live payload has no 'jobs' array"
    )
    assert "monzo" in validate.VALIDATION_PROFILES
    assert cli.DEFAULT_VALIDATION_FIXTURES["monzo"] == MONZO_FIXTURE


def test_monzo_validation_passes_on_an_enveloped_payload(
    tmp_path: Path, monzo, monzo_payload, fake_http
) -> None:
    client = fake_http(monzo_payload)

    outcome = run_live_check(
        monzo,
        fixture_payload=monzo_payload,
        client=client,
        sample_dir=tmp_path / "validation",
        max_records=2,
        now=CAPTURED_AT,
    )

    assert outcome.ok is True
    assert client.requests == ["https://boards-api.greenhouse.io/v1/boards/monzo/jobs?content=true"]
    assert outcome.scan is not None
    assert outcome.scan.records_parsed == len(monzo_payload["jobs"])
    assert outcome.scan.skipped_count == 0
    sample = json.loads(outcome.sample_path.read_text(encoding="utf-8"))
    # The sample holds roles, not the envelope, and stays capped.
    assert sample["total_records"] == len(monzo_payload["jobs"])
    assert "jobs" not in sample
    assert len(sample["records"]) == 2
    assert sample["records"][0]["title"] == monzo_payload["jobs"][0]["title"]
    assert outcome.sample_path.name.startswith("monzo-live-")
    assert "Result: PASS" in outcome.report_path.read_text(encoding="utf-8")


def test_monzo_payload_without_a_jobs_array_is_rejected_by_the_adapter(
    tmp_path: Path, monzo, monzo_payload, fake_http
) -> None:
    client = fake_http({"meta": {"total": 0}})

    outcome = run_live_check(
        monzo,
        fixture_payload=monzo_payload,
        client=client,
        sample_dir=tmp_path / "validation",
        now=CAPTURED_AT,
    )

    assert outcome.ok is False
    # The adapter's own contract catches the envelope first, so the check never has to
    # guess: the reported error names the missing array.
    assert "no jobs array" in (outcome.error or "")
    assert outcome.comparison is None
    # A failed scan writes no artifact (M5.1 rule), so nothing is left behind.
    assert outcome.report_path is None
    assert not (tmp_path / "validation").exists()


def test_monzo_record_that_cannot_be_mapped_fails_validation(
    tmp_path: Path, monzo, monzo_payload, fake_http
) -> None:
    broken = json.loads(json.dumps(monzo_payload))
    broken["jobs"][1].pop("absolute_url")
    client = fake_http(broken)

    outcome = run_live_check(
        monzo,
        fixture_payload=monzo_payload,
        client=client,
        sample_dir=tmp_path / "validation",
        now=CAPTURED_AT,
    )

    assert outcome.ok is False
    assert outcome.scan is not None and outcome.scan.skipped_count == 1
    assert "skipped=1" in outcome.report_path.read_text(encoding="utf-8")


def test_monzo_type_drift_is_a_failure_not_a_warning(
    tmp_path: Path, monzo, monzo_payload, fake_http
) -> None:
    drifted = json.loads(json.dumps(monzo_payload))
    for job in drifted["jobs"]:
        job["updated_at"] = 1761000000
    client = fake_http(drifted)

    outcome = run_live_check(
        monzo,
        fixture_payload=monzo_payload,
        client=client,
        sample_dir=tmp_path / "validation",
        now=CAPTURED_AT,
    )

    assert outcome.ok is False
    assert any("updated_at" in item for item in outcome.comparison.failures)


def test_monzo_extra_live_field_is_only_a_warning(
    tmp_path: Path, monzo, monzo_payload, fake_http
) -> None:
    enriched = json.loads(json.dumps(monzo_payload))
    for job in enriched["jobs"]:
        job["pay_input_ranges"] = [{"min_cents": 4000000, "max_cents": 4500000}]
    client = fake_http(enriched)

    outcome = run_live_check(
        monzo,
        fixture_payload=monzo_payload,
        client=client,
        sample_dir=tmp_path / "validation",
        now=CAPTURED_AT,
    )

    assert outcome.ok is True
    assert any("pay_input_ranges" in item for item in outcome.comparison.warnings)


def test_a_null_field_is_reported_differently_from_a_missing_one() -> None:
    """Observed live behaviour: Greenhouse sends `metadata` present but null."""
    fixture = [{"id": 1, "title": "Role", "absolute_url": "u", "metadata": None}]
    present_but_null = [{"id": 1, "title": "Role", "absolute_url": "u", "metadata": None}]
    missing_entirely = [{"id": 1, "title": "Role", "absolute_url": "u"}]

    null_comparison = compare_payloads(present_but_null, fixture, profile=validate.MONZO_PROFILE)
    missing_comparison = compare_payloads(missing_entirely, fixture, profile=validate.MONZO_PROFILE)

    assert "optional field is null in every live record: metadata" in null_comparison.warnings
    assert "optional field absent from every live record: metadata" in missing_comparison.warnings


def test_the_monzo_fixture_matches_the_observed_live_shape(monzo_payload) -> None:
    """The baseline is synthetic but must not invent a host or field shape we know is wrong."""
    for job in monzo_payload["jobs"]:
        assert job["absolute_url"].startswith("https://job-boards.greenhouse.io/monzo/jobs/")
        assert "metadata" in job


def test_cli_validates_monzo_with_its_own_fixture(
    tmp_path: Path, monkeypatch, capsys, monzo_payload, fake_http
) -> None:
    monkeypatch.setattr(cli, "UrllibHttpClient", lambda **_kwargs: fake_http(monzo_payload))
    monkeypatch.setattr(cli, "VALIDATION_ARTIFACT_ROOT", tmp_path)
    sample_dir = tmp_path / "validation"

    exit_code = cli.main(
        [
            "validate",
            "--confirm-live",
            "--source",
            "monzo",
            "--sample-out",
            str(sample_dir),
            "--max-records",
            "2",
        ]
    )
    out = capsys.readouterr().out

    assert exit_code == cli.EXIT_OK
    assert "Live validation: monzo (Monzo)" in out
    assert "Result: PASS" in out
    assert "Database: not opened" in out
    assert len(list(sample_dir.glob("monzo-live-*.json"))) == 1
    assert not (tmp_path / "jobscout.db").exists()


# Lloyds Graduate (Workday listing POST plus bounded detail lookups) ------


@pytest.fixture
def lloyds() -> SourceConfig:
    return SourceConfig(
        id="lloyds_graduate",
        employer="Lloyds Banking Group",
        adapter="workday",
        options={
            "host": "lbg.wd3.myworkdayjobs.com",
            "tenant": "lbg",
            "site": "Graduate_careers",
            "locale": "en-US",
        },
    )


@pytest.fixture
def lloyds_payload() -> dict[str, Any]:
    data = json.loads(LLOYDS_FIXTURE.read_text(encoding="utf-8"))
    data["total"] = len(data["jobPostings"])
    return data


def test_the_lloyds_profile_allows_a_bounded_workday_conversation() -> None:
    profile = validate.VALIDATION_PROFILES["lloyds_graduate"]

    assert profile.records_key == "jobPostings"
    assert profile.request_budget > 1, "a Workday listing is a POST plus bounded detail GETs"
    assert profile.require_complete is False, "the lookup cap makes partial scans expected"
    assert profile.adapter_options["max_detail_lookups"] < 10
    assert cli.DEFAULT_VALIDATION_FIXTURES["lloyds_graduate"] == LLOYDS_FIXTURE


def test_lloyds_validation_posts_once_and_passes(
    tmp_path: Path, lloyds, lloyds_payload, fake_workday_http
) -> None:
    client = fake_workday_http(pages=[lloyds_payload])

    outcome = run_live_check(
        lloyds,
        fixture_payload=lloyds_payload,
        client=client,
        sample_dir=tmp_path / "validation",
        max_records=2,
        now=CAPTURED_AT,
    )

    assert outcome.ok is True
    assert len(client.posts) == 1, "one listing query, no pagination needed"
    assert client.gets == [], "no placeholder locations, so no detail lookups"
    assert len(outcome.lines) > 0
    assert outcome.scan is not None and outcome.scan.records_parsed == 2
    sample = json.loads(outcome.sample_path.read_text(encoding="utf-8"))
    assert sample["records"][0]["title"] == lloyds_payload["jobPostings"][0]["title"]
    report = outcome.report_path.read_text(encoding="utf-8")
    assert "Result: PASS" in report
    assert "Request:" in report


def test_a_placeholder_location_costs_one_detail_lookup(
    tmp_path: Path, lloyds, lloyds_payload, fake_workday_http
) -> None:
    payload = json.loads(json.dumps(lloyds_payload))
    payload["jobPostings"][0]["locationsText"] = "2 Locations"
    detail = {
        "jobPostingInfo": {
            "title": payload["jobPostings"][0]["title"],
            "location": "London, United Kingdom",
            "jobDescription": "<p>Graduate scheme.</p>",
            "externalUrl": "https://lbg.wd3.myworkdayjobs.com/en-US/Graduate_careers/job/LBG200",
        }
    }
    client = fake_workday_http(
        pages=[payload], details={payload["jobPostings"][0]["externalPath"]: detail}
    )

    outcome = run_live_check(
        lloyds,
        fixture_payload=lloyds_payload,
        client=client,
        sample_dir=tmp_path / "validation",
        now=CAPTURED_AT,
    )

    assert outcome.ok is True
    assert len(client.posts) == 1
    assert len(client.gets) == 1, "exactly one detail lookup for the one placeholder"
    assert sum(1 for line in outcome.lines if line.startswith("Requests:")) == 1


def test_an_extra_detail_lookup_beyond_the_budget_is_refused(
    tmp_path: Path, lloyds, lloyds_payload, fake_workday_http
) -> None:
    """The budget is enforced before the call, so the source never sees request N+1."""
    payload = json.loads(json.dumps(lloyds_payload))
    for posting in payload["jobPostings"]:
        posting["locationsText"] = "2 Locations"
    detail = {
        "jobPostingInfo": {
            "title": "Graduate scheme",
            "location": "London, United Kingdom",
            "jobDescription": "<p>Graduate scheme.</p>",
        }
    }
    client = fake_workday_http(
        pages=[payload],
        details={posting["externalPath"]: detail for posting in payload["jobPostings"]},
    )
    tight = validate.ValidationProfile(
        watched_fields=validate.WORKDAY_WATCHED_FIELDS,
        required_fields=validate.WORKDAY_REQUIRED_FIELDS,
        records_key="jobPostings",
        request_budget=2,  # one listing plus a single detail lookup
        require_complete=False,
        adapter_options={"page_size": 20, "max_detail_lookups": 3},
    )

    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(validate.VALIDATION_PROFILES, "lloyds_graduate", tight)
        outcome = run_live_check(
            lloyds,
            fixture_payload=lloyds_payload,
            client=client,
            sample_dir=tmp_path / "validation",
            now=CAPTURED_AT,
        )

    assert outcome.ok is False
    assert "budget of 2" in (outcome.error or "")
    assert len(client.posts) == 1
    assert len(client.gets) == 1, "the second detail lookup never reached the network"
    assert outcome.report_path is None


def test_a_partial_workday_scan_is_a_warning_not_a_failure(
    tmp_path: Path, lloyds, lloyds_payload, fake_workday_http
) -> None:
    payload = json.loads(json.dumps(lloyds_payload))
    for posting in payload["jobPostings"]:
        posting["locationsText"] = "Multiple Locations"
    detail = {
        "jobPostingInfo": {
            "title": "Data Science & AI Graduate Scheme",
            "location": "London, United Kingdom",
            "jobDescription": "<p>Graduate scheme.</p>",
        }
    }
    # Supply a detail for the first lookup only; the second returns the same, but the cap of
    # one lookup below is what makes the scan partial.
    client = fake_workday_http(
        pages=[payload], details={payload["jobPostings"][0]["externalPath"]: detail}
    )
    profile = validate.ValidationProfile(
        watched_fields=validate.WORKDAY_WATCHED_FIELDS,
        required_fields=validate.WORKDAY_REQUIRED_FIELDS,
        records_key="jobPostings",
        request_budget=4,
        require_complete=False,
        adapter_options={"page_size": 20, "max_detail_lookups": 1},
    )

    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(validate.VALIDATION_PROFILES, "lloyds_graduate", profile)
        outcome = run_live_check(
            lloyds,
            fixture_payload=lloyds_payload,
            client=client,
            sample_dir=tmp_path / "validation",
            now=CAPTURED_AT,
        )

    assert outcome.ok is True, "a capped lookup is our bound, not a payload defect"
    assert outcome.scan is not None and not outcome.scan.complete
    assert any("scan was partial" in line for line in outcome.lines)


def test_workday_field_drift_fails(
    tmp_path: Path, lloyds, lloyds_payload, fake_workday_http
) -> None:
    drifted = json.loads(json.dumps(lloyds_payload))
    for posting in drifted["jobPostings"]:
        posting["externalPath"] = 12345
    client = fake_workday_http(pages=[drifted])

    outcome = run_live_check(
        lloyds,
        fixture_payload=lloyds_payload,
        client=client,
        sample_dir=tmp_path / "validation",
        now=CAPTURED_AT,
    )

    assert outcome.ok is False
    assert any("externalPath" in item for item in outcome.comparison.failures)
    assert outcome.report_path is not None


def test_cli_validates_lloyds_graduate_with_its_own_fixture(
    tmp_path: Path, monkeypatch, capsys, lloyds_payload, fake_workday_http
) -> None:
    monkeypatch.setattr(
        cli, "UrllibHttpClient", lambda **_kwargs: fake_workday_http(pages=[lloyds_payload])
    )
    monkeypatch.setattr(cli, "VALIDATION_ARTIFACT_ROOT", tmp_path)
    sample_dir = tmp_path / "validation"

    exit_code = cli.main(
        [
            "validate",
            "--confirm-live",
            "--source",
            "lloyds_graduate",
            "--sample-out",
            str(sample_dir),
        ]
    )
    out = capsys.readouterr().out

    assert exit_code == cli.EXIT_OK
    assert "Live validation: lloyds_graduate (Lloyds Banking Group)" in out
    assert "Result: PASS" in out
    assert "Database: not opened" in out
    assert len(list(sample_dir.glob("lloyds_graduate-live-*.json"))) == 1
