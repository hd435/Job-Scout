"""Bounded live-validation check for one source (M5.1, extended for Monzo in M5.4.1).

The check makes **one** read-only request, compares the live payload's shape with the
recorded fixture, and runs the real adapter over the live payload so parsing is proven
rather than assumed. It never opens the production database and never touches Telegram.

Two payload families are covered: a bare JSON list (Palantir/Lever) and an enveloped board
response whose roles live under `jobs` (Monzo/Greenhouse). The differences are expressed as
a `ValidationProfile`, so the check itself stays source-agnostic.

Design constraints (docs/roadmap.md M5.1):

- exactly one HTTP request per run, no retries inside the check;
- a failure writes no artifact;
- anything written stays under `data/validation/` (gitignored) and is capped and
  trimmed, never a full mirror of the source;
- the command refuses to run without an explicit `--confirm-live` acknowledgement.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import SourceConfig
from .http import HttpClient, HttpError
from .models import ScanResult, ScanStatus
from .sources import SourceAdapter, get_adapter
from .sources.sitemap_html import SitemapHtmlAdapter
from .sources.workday import WorkdayAdapter

DEFAULT_SAMPLE_RECORDS = 5
DESCRIPTION_LIMIT = 200
MAX_DEPTH = 3
MAX_DICT_ITEMS = 20
MAX_LIST_ITEMS = 10
MAX_RECORD_FIELDS = 40
MAX_KEY_LENGTH = 64
REQUEST_BUDGET = 1

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

# Greenhouse (Monzo) returns an envelope: roles sit in a `jobs` array beside `meta`. Only
# fields the adapter actually reads are watched, so a warning means real drift rather than
# a field we never modelled. `id` is intentionally not type-watched: the adapter coerces
# strings and integers alike, so its type is not a parsing risk.
MONZO_WATCHED_FIELDS: dict[str, type] = {
    "title": str,
    "absolute_url": str,
    "location": dict,
    "offices": list,
    "content": str,
    "updated_at": str,
    "first_published": str,
    "metadata": list,
}
MONZO_REQUIRED_FIELDS = ("id", "title", "absolute_url")


@dataclass(frozen=True)
class ValidationProfile:
    """What the check knows about one source's payload before it sees live data."""

    watched_fields: Mapping[str, type]
    required_fields: tuple[str, ...]
    records_key: str | None = None
    request_budget: int = 1
    require_complete: bool = True
    adapter_options: Mapping[str, Any] = field(default_factory=dict)
    payload_kind: str = "json"  # "json" compares a payload; "html" judges the scan alone

    def records(self, payload: Any) -> Any:
        """The role records inside a payload; `None` when an envelope lacks them."""
        if self.records_key is None:
            return payload
        if isinstance(payload, dict):
            return payload.get(self.records_key)
        return None

    def missing_records_note(self, payload: Any) -> str:
        """Explain an unusable envelope in the source's own terms."""
        if self.records_key is None:
            return f"live payload is not a JSON list (found {type(payload).__name__})"
        return f"live payload has no '{self.records_key}' array"


PALANTIR_PROFILE = ValidationProfile(watched_fields=WATCHED_FIELDS, required_fields=REQUIRED_FIELDS)
MONZO_PROFILE = ValidationProfile(
    watched_fields=MONZO_WATCHED_FIELDS,
    required_fields=MONZO_REQUIRED_FIELDS,
    records_key="jobs",
)

# Workday's listing is a POST returning `{total, jobPostings[]}`, paginated in pages of
# `page_size`. Validation checks the listing contract, not location resolution, so detail
# lookups are switched off: the live board already showed more multi-site postings than a
# small budget can resolve, and placeholder resolution is covered by offline tests. With
# lookups off the scan is `partial` by design, so `require_complete` is off and the
# incompleteness is reported as a warning instead of a payload failure.
WORKDAY_WATCHED_FIELDS: dict[str, type] = {
    "title": str,
    "externalPath": str,
    "locationsText": str,
    "postedOn": str,
    "bulletFields": list,
}
WORKDAY_REQUIRED_FIELDS = ("title", "externalPath")
LLOYDS_PROFILE = ValidationProfile(
    watched_fields=WORKDAY_WATCHED_FIELDS,
    required_fields=WORKDAY_REQUIRED_FIELDS,
    records_key="jobPostings",
    request_budget=6,
    require_complete=False,
    adapter_options={"page_size": 20, "max_detail_lookups": 0},
)

VALIDATION_PROFILES: dict[str, ValidationProfile] = {
    "palantir": PALANTIR_PROFILE,
    "monzo": MONZO_PROFILE,
    "lloyds_graduate": LLOYDS_PROFILE,
}

# A sitemap source has no JSON payload to compare: its contract is "the board was found,
# URLs matched, and pages parsed". The budget covers two sitemap documents and three detail
# pages, with the delay removed because the check must stay quick and small.
SITEMAP_VALIDATION_BUDGET = 5
SITEMAP_PROFILE = ValidationProfile(
    watched_fields={},
    required_fields=(),
    request_budget=SITEMAP_VALIDATION_BUDGET,
    require_complete=False,
    adapter_options={
        "max_sitemap_documents": 2,
        "max_detail_fetches": 3,
        "request_delay": 0.0,
    },
    payload_kind="html",
)

# Budgets inferred from the adapter when a source has no named profile, so any configured
# source can be checked before it is enabled.
ADAPTER_PROFILES: dict[str, ValidationProfile] = {
    "lever": PALANTIR_PROFILE,
    "greenhouse": MONZO_PROFILE,
    "workday": LLOYDS_PROFILE,
    "sitemap_html": SITEMAP_PROFILE,
}


def profile_for(source: SourceConfig) -> ValidationProfile | None:
    """The check's expectations for a source: named profile first, then adapter default."""
    return VALIDATION_PROFILES.get(source.id) or ADAPTER_PROFILES.get(source.adapter)


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


class RequestBudgetExceeded(RuntimeError):
    """Raised before a second request could reach the network."""


class RecordingClient:
    """Wraps an HTTP client, enforcing a request budget before delegating.

    The budget is checked *before* the inner client is called, so a source can never receive
    a request the check did not intend to make. It counts POSTs as well as GETs, because a
    Workday listing query is a POST. The exception is deliberately not an `HttpError`:
    adapters convert those into a failed scan, which would hide the violation.
    """

    def __init__(self, client: HttpClient, *, max_requests: int = REQUEST_BUDGET) -> None:
        self._client = client
        self._max_requests = max_requests
        self.requests: list[str] = []
        self.payload: Any = None

    @property
    def max_requests(self) -> int:
        return self._max_requests

    def get_json(self, url: str) -> Any:
        self._check(url)
        payload = self._client.get_json(url)
        self._remember(payload)
        return payload

    def get_text(self, url: str) -> str:
        """Sitemap and HTML sources read text, and that counts against the budget too."""
        self._check(url)
        return self._client.get_text(url)  # type: ignore[attr-defined]

    def post_json(self, url: str, payload: dict[str, Any], *, retry: bool = False) -> Any:
        self._check(url)
        response = self._client.post_json(url, payload, retry=retry)
        self._remember(response)
        return response

    def _check(self, url: str) -> None:
        if len(self.requests) >= self._max_requests:
            raise RequestBudgetExceeded(
                f"request budget of {self._max_requests} exceeded; refused a request to {url}"
            )
        self.requests.append(url)

    def _remember(self, payload: Any) -> None:
        # The first response is the one the shape contract describes: a listing page for
        # paginated sources, the only response for single-request ones.
        if self.payload is None:
            self.payload = payload


def compare_payloads(
    live: Any, fixture: Any, *, profile: ValidationProfile = PALANTIR_PROFILE
) -> PayloadComparison:
    """Compare the live payload's records with the recorded fixture. See `ValidationProfile`.

    The fixture is a *recorded baseline for shape*: it tells us which keys the source used
    to return and how many records were sampled. It is deliberately not treated as a type
    oracle, because a committed fixture is a synthetic trimming that can include records
    with missing or malformed values. The type contract is therefore enforced on the
    **live** payload only, where uniform types are expected.
    """
    failures: list[str] = []
    warnings: list[str] = []
    optional_fields = frozenset(profile.watched_fields) - frozenset(profile.required_fields)

    if not isinstance(fixture, list):
        failures.append(f"fixture is not a JSON list (found {type(fixture).__name__})")
    if not isinstance(live, list):
        failures.append(profile.missing_records_note(live))
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
            if key in profile.required_fields:
                failures.append(f"required field missing from live payload: {key}")
            else:
                warnings.append(f"field no longer present: {key}")

    for name, expected_type in profile.watched_fields.items():
        if name in profile.required_fields and all(
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
        elif name in optional_fields and not observed:
            # Distinguish "the source stopped sending it" from "it is present but null",
            # because the two mean different things to whoever reads the report.
            if any(_has_key(record, name) for record in live_records):
                warnings.append(f"optional field is null in every live record: {name}")
            else:
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
        items = list(record.items())
        trimmed: dict[str, Any] = {}
        for key, value in items[:MAX_RECORD_FIELDS]:
            out_key = _bounded_key(key)
            if key == "lists" and isinstance(value, list):
                # Section bodies carry the full description HTML; only their count matters.
                trimmed[out_key] = {"__omitted__": len(value)}
            else:
                trimmed[out_key] = _bounded(value, depth=MAX_DEPTH)
        if len(items) > MAX_RECORD_FIELDS:
            trimmed["__omitted__"] = len(items) - MAX_RECORD_FIELDS
        sample.append(trimmed)
    return {
        "source_id": source_id,
        "captured_at": captured_at,
        "total_records": len(records),
        "sample_truncated_to": max_records,
        "records": sample,
    }


def _bounded(value: Any, *, depth: int) -> Any:
    """Copy a value with hard limits on string length, container size, and nesting."""
    if isinstance(value, str):
        if len(value) > DESCRIPTION_LIMIT:
            return value[:DESCRIPTION_LIMIT] + "..."
        return value
    if isinstance(value, dict):
        if depth <= 0:
            return {"__omitted__": len(value)}
        bounded = {
            _bounded_key(key): _bounded(item, depth=depth - 1)
            for key, item in list(value.items())[:MAX_DICT_ITEMS]
        }
        if len(value) > MAX_DICT_ITEMS:
            bounded["__omitted__"] = len(value) - MAX_DICT_ITEMS
        return bounded
    if isinstance(value, list):
        if depth <= 0 or len(value) > MAX_LIST_ITEMS:
            return {"__omitted__": len(value)}
        return [_bounded(item, depth=depth - 1) for item in value]
    return value


def _bounded_key(key: Any) -> str:
    """Keys are copied too, so an oversized field name cannot inflate the artifact."""
    text = key if isinstance(key, str) else str(key)
    if len(text) > MAX_KEY_LENGTH:
        return text[:MAX_KEY_LENGTH] + "..."
    return text


def run_live_check(
    source: SourceConfig,
    *,
    fixture_payload: Any,
    client: HttpClient,
    sample_dir: Path | None = None,
    allowed_root: Path | None = None,
    max_records: int = DEFAULT_SAMPLE_RECORDS,
    now: str | None = None,
    allow_missing_fixture: bool = False,
) -> ValidationOutcome:
    """Perform exactly one read-only request and judge the result.

    `client` is injected so tests can prove the whole path offline; the caller decides
    the retry policy of the underlying client (the CLI builds it with no retries).
    `allowed_root` confines artifacts to one directory (the gitignored `data/` tree in the
    CLI); the containment check runs before any request is made.

    `allow_missing_fixture` is first contact with a source that has no recorded payload yet:
    the adapter still parses the live response, but the shape comparison is skipped and the
    report says so rather than implying a contract was checked.
    """
    captured_at = now or _now_iso()
    profile = profile_for(source)
    if profile is None:
        return ValidationOutcome(
            ok=False,
            source_id=source.id,
            error=f"no validation profile for source '{source.id}'",
            lines=(f"No validation profile for source '{source.id}'",),
        )
    if fixture_payload is None and profile.payload_kind == "json" and not allow_missing_fixture:
        return ValidationOutcome(
            ok=False,
            source_id=source.id,
            error=(
                f"source '{source.id}' has no recorded fixture, so its payload shape cannot"
                " be compared; pass --fixture <path> with a recorded response"
            ),
            lines=(
                f"No recorded fixture for source '{source.id}'."
                " Pass --fixture <path> to compare shapes, or use a source whose fixture"
                " is bundled.",
            ),
        )
    if sample_dir is not None and allowed_root is not None:
        _ensure_within(sample_dir, allowed_root)

    recording = RecordingClient(client, max_requests=profile.request_budget)
    adapter = _build_adapter(source, profile)

    try:
        scan = adapter.fetch_scan(source, recording, known={})
    except RequestBudgetExceeded as exc:
        return ValidationOutcome(
            ok=False,
            source_id=source.id,
            request_url=recording.requests[0] if recording.requests else None,
            error=str(exc),
            lines=(f"Request budget violated: {exc}",),
        )
    except HttpError as exc:
        return ValidationOutcome(
            ok=False,
            source_id=source.id,
            request_url=recording.requests[0] if recording.requests else None,
            error=f"request failed: {exc.summary()}",
            lines=(f"Request failed: {exc.summary()}",),
        )

    if not 1 <= len(recording.requests) <= profile.request_budget:
        return ValidationOutcome(
            ok=False,
            source_id=source.id,
            request_url=recording.requests[0] if recording.requests else None,
            scan=scan,
            error=f"expected 1 to {profile.request_budget} request(s), the adapter made"
            f" {len(recording.requests)}",
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
            lines=(f"Request: {recording.requests[0]}", f"Request failed: {message}"),
        )

    complete_ok = bool(scan.complete) or not profile.require_complete
    parse_ok = scan.status is not ScanStatus.FAILED and not scan.skipped and complete_ok
    if profile.payload_kind == "html":
        # Nothing to compare against a fixture: the contract is that the board was found
        # and pages parsed, which `scan` already reports.
        comparison = None
        live_records = None
        ok = parse_ok and scan.records_seen > 0
    else:
        live_records = profile.records(recording.payload)
        if fixture_payload is None:
            comparison = None
            ok = parse_ok and bool(live_records)
        else:
            comparison = compare_payloads(
                live_records, profile.records(fixture_payload), profile=profile
            )
            ok = comparison.ok and parse_ok

    lines = _report_lines(
        recording.requests,
        comparison,
        scan,
        ok,
        require_complete=profile.require_complete,
        payload_kind=profile.payload_kind,
        live_records=live_records if isinstance(live_records, list) else None,
    )
    sample_path = report_path = None
    if sample_dir is not None:
        # A failed request leaves no artifact; a shape or parse failure keeps the evidence.
        sample_path, report_path = _write_artifacts(
            sample_dir,
            source_id=source.id,
            payload=live_records,
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


def _build_adapter(source: SourceConfig, profile: ValidationProfile) -> SourceAdapter:
    """Build the real adapter, optionally with a smaller bound than the production default."""
    if not profile.adapter_options:
        return get_adapter(source.adapter)
    if source.adapter == "workday":
        return WorkdayAdapter(**profile.adapter_options)
    if source.adapter == "sitemap_html":
        return SitemapHtmlAdapter(**profile.adapter_options)
    raise ValueError(f"no bounded adapter builder for '{source.adapter}'")  # pragma: no cover


def _report_lines(
    requests: list[str],
    comparison: PayloadComparison | None,
    scan: ScanResult,
    ok: bool,
    *,
    require_complete: bool,
    payload_kind: str = "json",
    live_records: list[Any] | None = None,
) -> tuple[str, ...]:
    lines = [
        f"Request: {requests[0]}" if len(requests) == 1 else f"Requests: {len(requests)}",
    ]
    if len(requests) > 1:
        lines.extend(f"  {url}" for url in requests)
    if comparison is not None:
        lines.append(
            f"Records: live={comparison.live_records} fixture={comparison.fixture_records}"
        )
    elif live_records is not None:
        lines.append(f"Records: live={len(live_records)} (no recorded fixture to compare)")
    else:
        lines.append(
            f"Records: live={scan.records_seen} (no JSON payload to compare for this adapter)"
        )
    lines.append(
        f"Adapter: status={scan.status} complete={'yes' if scan.complete else 'no'}"
        f" seen={scan.records_seen} parsed={scan.records_parsed} skipped={scan.skipped_count}"
    )
    if scan.error_summary:
        lines.append(f"Adapter note: {scan.error_summary}")
    if comparison is None:
        if not scan.records_seen:
            lines.append("FAIL  the adapter found no roles on the live board")
    else:
        for failure in comparison.failures:
            lines.append(f"FAIL  {failure}")
        for warning in comparison.warnings:
            lines.append(f"WARN  {warning}")
    if scan.status is not ScanStatus.FAILED and not scan.complete and not require_complete:
        lines.append(
            "WARN  scan was partial; incompleteness does not fail this check, but a"
            " production scan would refuse to baseline or alert on it"
        )
    if comparison is None and payload_kind == "html":
        lines.append(
            "Shape: not compared (this adapter has no JSON payload; the scan result is"
            " the evidence)"
        )
    elif comparison is None:
        lines.append(
            "Shape: not compared (no recorded fixture yet: this is a first-contact check of"
            " the live response, not a contract check)"
        )
    elif not comparison.failures and not comparison.warnings:
        lines.append("Shape: identical to the recorded fixture")
    elif comparison.shape_ok:
        lines.append("Shape: compatible, with the warnings above")
    lines.append("Result: PASS" if ok else "Result: FAIL")
    return tuple(lines)


def ensure_artifacts_location(directory: Path, root: Path) -> Path:
    """Public seam: validate an artifact directory before any work happens."""
    _ensure_within(directory, root)
    return directory


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
    sample_path = _free_path(directory / f"{source_id}-live-{stamp}.json")
    report_path = _free_path(directory / f"{source_id}-validation-{stamp}.txt")
    sample = build_sample(
        payload, source_id=source_id, captured_at=captured_at, max_records=max_records
    )
    sample_path.write_text(json.dumps(sample, indent=2, ensure_ascii=False), encoding="utf-8")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sample_path, report_path


def _free_path(path: Path) -> Path:
    """Never overwrite an earlier artifact: append a counter instead."""
    if not path.exists():
        return path
    for index in range(2, 100):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise ValueError(f"too many validation artifacts in {path.parent}")  # pragma: no cover


def _ensure_within(directory: Path, root: Path) -> None:
    """Refuse to write artifacts outside the intended (gitignored) directory."""
    resolved_dir = directory.expanduser().resolve()
    resolved_root = root.expanduser().resolve()
    if resolved_dir != resolved_root and resolved_root not in resolved_dir.parents:
        raise ValueError(
            f"validation artifacts must stay inside {resolved_root}; got {resolved_dir}"
        )


def _resolve(record: Any, dotted: str) -> Any:
    current: Any = record
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _has_key(record: Any, dotted: str) -> bool:
    """True when the final key exists even if its value is null."""
    current: Any = record
    *parents, leaf = dotted.split(".")
    for part in parents:
        if not isinstance(current, dict):
            return False
        current = current.get(part)
    return isinstance(current, dict) and leaf in current


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "ADAPTER_PROFILES",
    "DEFAULT_SAMPLE_RECORDS",
    "MONZO_PROFILE",
    "PALANTIR_PROFILE",
    "SITEMAP_PROFILE",
    "PayloadComparison",
    "RecordingClient",
    "RequestBudgetExceeded",
    "VALIDATION_PROFILES",
    "ValidationOutcome",
    "ValidationProfile",
    "build_sample",
    "compare_payloads",
    "profile_for",
    "ensure_artifacts_location",
    "run_live_check",
]
