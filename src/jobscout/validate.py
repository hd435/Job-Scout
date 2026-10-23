"""Bounded live-validation check for a single list-shaped source (M5.1).

The check makes **one** read-only request, compares the live payload's shape with the
recorded fixture, and runs the real adapter over the live payload so parsing is proven
rather than assumed. It never opens the production database and never touches Telegram.

Design constraints (docs/roadmap.md M5.1):

- exactly one HTTP request per run, no retries inside the check;
- a failure writes no artifact;
- anything written stays under `data/validation/` (gitignored) and is capped and
  trimmed, never a full mirror of the source;
- the command refuses to run without an explicit `--confirm-live` acknowledgement.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import SourceConfig
from .http import HttpClient, HttpError
from .models import ScanResult, ScanStatus
from .sources import get_adapter

DEFAULT_SAMPLE_RECORDS = 5
DESCRIPTION_LIMIT = 200

# Fields the adapter reads, with the type each must have in **live** data. The committed
# fixture is synthetic and intentionally contains missing and malformed values, so the
# fixture is used as a key-shape baseline only; the type contract applies to live records.
WATCHED_FIELDS: dict[str, type] = {
    "id": str,
    "text": str,
    "hostedUrl": str,
    "categories": dict,
    "categories.location": str,
    "categories.allLocations": list,
    "createdAt": int,
    "descriptionPlain": str,
    "workplaceType": str,
    "salaryRange": dict,
}
REQUIRED_FIELDS = ("id", "text", "hostedUrl", "categories")

# Fields whose absence is normal (they are optional in the data model) are reported as
# drift only when the fixture had them; a *type* change is always a failure.
_OPTIONAL_FIELDS = frozenset(WATCHED_FIELDS) - frozenset(REQUIRED_FIELDS)


@dataclass(frozen=True)
class PayloadComparison:
    """Structural comparison between a live payload and the recorded fixture."""

    failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    live_records: int = 0
    fixture_records: int = 0

    @property
    def shape_ok(self) -> bool:
        return not self.failures

    @property
    def ok(self) -> bool:
        return self.shape_ok


@dataclass(frozen=True)
class ValidationOutcome:
    """Everything the check learned, plus whether it passed."""

    ok: bool
    source_id: str
    request_url: str | None = None
    comparison: PayloadComparison | None = None
    scan: ScanResult | None = None
    error: str | None = None
    sample_path: Path | None = None
    report_path: Path | None = None
    lines: tuple[str, ...] = field(default_factory=tuple)


class RecordingClient:
    """Wraps a JSON client, counting requests and keeping the one payload returned."""

    def __init__(self, client: HttpClient) -> None:
        self._client = client
        self.requests: list[str] = []
        self.payload: Any = None

    def get_json(self, url: str) -> Any:
        self.requests.append(url)
        payload = self._client.get_json(url)
        self.payload = payload
        return payload


def compare_payloads(live: Any, fixture: Any) -> PayloadComparison:
    """Compare the live payload with the recorded fixture.

    The fixture is a *recorded baseline for shape*: it tells us which keys the source used
    to return and how many records were sampled. It is deliberately not treated as a type
    oracle, because the committed Palantir fixture is a synthetic trimming that includes
    records with no `createdAt` and one with a non-date string. The type contract below is
    therefore enforced on the **live** payload only, where uniform types are expected.
    """
    failures: list[str] = []
    warnings: list[str] = []

    if not isinstance(fixture, list):
        failures.append(f"fixture is not a JSON list (found {type(fixture).__name__})")
    if not isinstance(live, list):
        failures.append(f"live payload is not a JSON list (found {type(live).__name__})")
    if failures:
        return PayloadComparison(failures=tuple(failures))

    live_records = [record for record in live if isinstance(record, dict)]
    fixture_records = [record for record in fixture if isinstance(record, dict)]
    if not live_records:
        failures.append("live payload contains no job records")
    if len(live_records) != len(live):
        failures.append(f"{len(live) - len(live_records)} live record(s) are not objects")
    if live_records and fixture_records:
        live_keys = set().union(*(record.keys() for record in live_records))
        fixture_keys = set().union(*(record.keys() for record in fixture_records))
        for key in sorted(live_keys - fixture_keys):
            warnings.append(f"new field in live payload: {key}")
        for key in sorted(fixture_keys - live_keys):
            if key in REQUIRED_FIELDS:
                failures.append(f"required field missing from live payload: {key}")
            else:
                warnings.append(f"field no longer present: {key}")

    for name, expected_type in WATCHED_FIELDS.items():
        if name in REQUIRED_FIELDS and all(
            _resolve(record, name) is None for record in live_records
        ):
            failures.append(f"required field has no value in any live record: {name}")
            continue
        observed = {
            type(value).__name__
            for record in live_records
            for value in (_resolve(record, name),)
            if value is not None
        }
        wrong = {type_name for type_name in observed if type_name != expected_type.__name__}
        if wrong:
            failures.append(
                f"field {name} has unexpected type(s) {sorted(wrong)};"
                f" expected {expected_type.__name__}"
            )
        elif name in _OPTIONAL_FIELDS and not observed:
            warnings.append(f"optional field absent from every live record: {name}")

    return PayloadComparison(
        failures=tuple(failures),
        warnings=tuple(warnings),
        live_records=len(live_records),
        fixture_records=len(fixture_records),
    )


def build_sample(
    payload: Any,
    *,
    source_id: str,
    captured_at: str,
    max_records: int = DEFAULT_SAMPLE_RECORDS,
) -> dict[str, Any]:
    """A trimmed, bounded sample: enough to inspect field names, not a content mirror."""
    records = payload if isinstance(payload, list) else []
    sample: list[Any] = []
    for record in records[:max_records]:
        if not isinstance(record, dict):
            sample.append({"__type__": type(record).__name__})
            continue
        trimmed: dict[str, Any] = {}
        for key, value in record.items():
            if key == "lists" and isinstance(value, list):
                trimmed[key] = {"__omitted__": len(value)}
            elif isinstance(value, str):
                trimmed[key] = value[:DESCRIPTION_LIMIT]
            else:
                trimmed[key] = value
        sample.append(trimmed)
    return {
        "source_id": source_id,
        "captured_at": captured_at,
        "total_records": len(records),
        "sample_truncated_to": max_records,
        "records": sample,
    }


def run_live_check(
    source: SourceConfig,
    *,
    fixture_payload: Any,
    client: HttpClient,
    sample_dir: Path | None = None,
    max_records: int = DEFAULT_SAMPLE_RECORDS,
    now: str | None = None,
) -> ValidationOutcome:
    """Perform exactly one read-only request and judge the result.

    `client` is injected so tests can prove the whole path offline; the caller decides
    the retry policy of the underlying client (the CLI builds it with no retries).
    """
    captured_at = now or _now_iso()
    recording = RecordingClient(client)
    adapter = get_adapter(source.adapter)

    try:
        scan = adapter.fetch_scan(source, recording, known={})
    except HttpError as exc:
        return ValidationOutcome(
            ok=False,
            source_id=source.id,
            request_url=recording.requests[0] if recording.requests else None,
            error=f"request failed: {exc.summary()}",
            lines=(f"Request failed: {exc.summary()}",),
        )

    if len(recording.requests) != 1:
        return ValidationOutcome(
            ok=False,
            source_id=source.id,
            request_url=recording.requests[0] if recording.requests else None,
            scan=scan,
            error=f"expected exactly 1 request, the adapter made {len(recording.requests)}",
            lines=(f"Request budget violated: {len(recording.requests)} request(s)",),
        )

    if scan.status is ScanStatus.FAILED:
        message = scan.error_summary or "adapter reported a failed scan"
        return ValidationOutcome(
            ok=False,
            source_id=source.id,
            request_url=recording.requests[0],
            scan=scan,
            error=message,
            lines=(f"Request: GET {recording.requests[0]}", f"Request failed: {message}"),
        )

    comparison = compare_payloads(recording.payload, fixture_payload)
    parse_ok = scan.status in {ScanStatus.SUCCESS, ScanStatus.SUCCESS_EMPTY} and not scan.skipped
    ok = comparison.ok and parse_ok

    lines = _report_lines(recording.requests[0], comparison, scan, ok)
    sample_path = report_path = None
    if sample_dir is not None:
        # A failed request leaves no artifact; a shape or parse failure keeps the evidence.
        sample_path, report_path = _write_artifacts(
            sample_dir,
            source_id=source.id,
            payload=recording.payload,
            captured_at=captured_at,
            max_records=max_records,
            lines=lines,
        )

    return ValidationOutcome(
        ok=ok,
        source_id=source.id,
        request_url=recording.requests[0],
        comparison=comparison,
        scan=scan,
        sample_path=sample_path,
        report_path=report_path,
        lines=lines,
    )


def _report_lines(
    url: str, comparison: PayloadComparison, scan: ScanResult, ok: bool
) -> tuple[str, ...]:
    lines = [
        f"Request: GET {url}",
        f"Records: live={comparison.live_records} fixture={comparison.fixture_records}",
        f"Adapter: status={scan.status} complete={'yes' if scan.complete else 'no'}"
        f" seen={scan.records_seen} parsed={scan.records_parsed} skipped={scan.skipped_count}",
    ]
    if scan.error_summary:
        lines.append(f"Adapter note: {scan.error_summary}")
    for failure in comparison.failures:
        lines.append(f"FAIL  {failure}")
    for warning in comparison.warnings:
        lines.append(f"WARN  {warning}")
    if not comparison.failures and not comparison.warnings:
        lines.append("Shape: identical to the recorded fixture")
    elif comparison.shape_ok:
        lines.append("Shape: compatible, with the warnings above")
    lines.append("Result: PASS" if ok else "Result: FAIL")
    return tuple(lines)


def _write_artifacts(
    directory: Path,
    *,
    source_id: str,
    payload: Any,
    captured_at: str,
    max_records: int,
    lines: tuple[str, ...],
) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = captured_at.replace(":", "").replace("-", "")
    sample_path = directory / f"{source_id}-live-{stamp}.json"
    report_path = directory / f"{source_id}-validation-{stamp}.txt"
    sample = build_sample(
        payload, source_id=source_id, captured_at=captured_at, max_records=max_records
    )
    sample_path.write_text(json.dumps(sample, indent=2, ensure_ascii=False), encoding="utf-8")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sample_path, report_path


def _resolve(record: Any, dotted: str) -> Any:
    current: Any = record
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "DEFAULT_SAMPLE_RECORDS",
    "PayloadComparison",
    "RecordingClient",
    "ValidationOutcome",
    "build_sample",
    "compare_payloads",
    "run_live_check",
]
