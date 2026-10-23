"""Greenhouse job-board adapter (used for Monzo in M4).

Endpoint: `https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true`

One request returns every published role with its fields, so there is no pagination and
no detail refresh to schedule. The response shape, the `content` HTML-escaped body, and
the absence of structured salary for this board were observed during source research
(`docs/sources.md`); the pay-range fallback below is an unvalidated assumption.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from ..config import SourceConfig
from ..http import HttpClient, HttpError
from ..matching import normalize_text
from ..models import Job, ScanResult, ScanStatus, SkipReason
from .base import build_job, uk_employer
from .htmltext import strip_html

GREENHOUSE_ENDPOINT = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"

_CURRENCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€"}
_PAY_INTERVALS = {
    "year": "per year",
    "month": "per month",
    "week": "per week",
    "day": "per day",
    "hour": "per hour",
}
_SALARY_METADATA_KEYS = ("salary", "compensation", "pay range", "salary range")


class GreenhouseAdapter:
    adapter_id = "greenhouse"

    def fetch_scan(
        self,
        config: SourceConfig,
        http_client: HttpClient,
        *,
        known: Mapping[str, str | None] | None = None,
    ) -> ScanResult:
        """The board returns every role in one response, so `known` is unused."""
        started_at = _now_iso()
        board = str(config.options.get("board") or config.id)
        url = GREENHOUSE_ENDPOINT.format(board=board)

        try:
            payload = http_client.get_json(url)
        except HttpError as exc:
            return _failed(config, started_at, exc.summary())

        if not isinstance(payload, dict):
            return _failed(config, started_at, f"unexpected payload type: {type(payload).__name__}")
        records = payload.get("jobs")
        if not isinstance(records, list):
            return _failed(config, started_at, "response has no jobs array")

        jobs: list[Job] = []
        skipped: list[SkipReason] = []
        seen_ids: set[str] = set()

        for index, record in enumerate(records):
            mapped = _map_record(record, index, config)
            if isinstance(mapped, SkipReason):
                skipped.append(mapped)
                continue
            if mapped.source_job_id in seen_ids:
                skipped.append(
                    SkipReason(mapped.source_job_id, "duplicate_source_job_id", f"index={index}")
                )
                continue
            seen_ids.add(mapped.source_job_id)
            jobs.append(mapped)

        if skipped:
            status = ScanStatus.PARTIAL
        elif not jobs:
            status = ScanStatus.SUCCESS_EMPTY
        else:
            status = ScanStatus.SUCCESS

        return ScanResult(
            source_id=config.id,
            started_at=started_at,
            finished_at=_now_iso(),
            status=status,
            records_seen=len(records),
            records_parsed=len(jobs),
            jobs=tuple(jobs),
            skipped=tuple(skipped),
            source_reported_total=_reported_total(payload, len(records)),
            error_summary=(f"{len(skipped)} record(s) could not be mapped" if skipped else None),
        )


def _reported_total(payload: dict[str, Any], fallback: int) -> int:
    meta = payload.get("meta")
    if isinstance(meta, dict):
        total = meta.get("total")
        if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
            return total
    return fallback


def _map_record(record: Any, index: int, config: SourceConfig) -> Job | SkipReason:
    if not isinstance(record, dict):
        return SkipReason(None, "record_not_object", f"index={index}")

    source_job_id = _as_text(record.get("id"))
    if not source_job_id:
        return SkipReason(None, "missing_source_job_id", f"index={index}")

    title = _as_text(record.get("title"))
    if not title:
        return SkipReason(source_job_id, "missing_title", f"index={index}")

    url = _as_text(record.get("absolute_url"))
    if not url:
        return SkipReason(source_job_id, "missing_url", f"index={index}")

    return build_job(
        source_id=config.id,
        source_job_id=source_job_id,
        employer=config.employer,
        title=title,
        location_raw=_location_text(record),
        url=url,
        description=strip_html(_as_text(record.get("content"))),
        posted_at=_posted_date(record),
        closing_at=None,
        salary_text=_salary_text(record),
        workplace_type=None,
        source_revision=_as_text(record.get("updated_at")),
        uk_employer=uk_employer(config),
    )


def _location_text(record: dict[str, Any]) -> str:
    location = record.get("location")
    if isinstance(location, dict):
        name = _as_text(location.get("name"))
        if name:
            return name
    offices = record.get("offices")
    if isinstance(offices, list):
        names = [_as_text(office.get("name")) for office in offices if isinstance(office, dict)]
        joined = "; ".join(dict.fromkeys(name for name in names if name))
        if joined:
            return joined
    return ""


def _posted_date(record: dict[str, Any]) -> Any:
    """Greenhouse reports ISO timestamps; only the date part is kept."""
    for key in ("first_published", "updated_at", "created_at"):
        raw = _as_text(record.get(key))
        if not raw:
            continue
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            if len(raw) >= 10:
                try:
                    return datetime.strptime(raw[:10], "%Y-%m-%d").date()
                except ValueError:
                    continue
    return None


def _salary_text(record: dict[str, Any]) -> str | None:
    return _format_pay_input_ranges(record.get("pay_input_ranges")) or _salary_from_metadata(
        record.get("metadata")
    )


def _format_pay_input_ranges(pay_ranges: Any) -> str | None:
    """Format Greenhouse's structured pay ranges; not confirmed for this board."""
    if not isinstance(pay_ranges, list):
        return None
    for entry in pay_ranges:
        if not isinstance(entry, dict):
            continue
        low = _cents_to_amount(entry.get("min_cents"))
        high = _cents_to_amount(entry.get("max_cents"))
        if low is None and high is None:
            continue
        currency = str(entry.get("currency_type") or "").upper()
        prefix = _CURRENCY_SYMBOLS.get(currency, f"{currency} " if currency else "")
        if low is not None and high is not None:
            amount = f"{prefix}{low:,} - {prefix}{high:,}".strip()
        else:
            amount = f"{prefix}{(low if low is not None else high):,}".strip()
        interval = _PAY_INTERVALS.get(normalize_text(str(entry.get("unit") or "")))
        return f"{amount} {interval}" if interval else amount
    return None


def _cents_to_amount(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return round(value / 100)


def _salary_from_metadata(metadata: Any) -> str | None:
    if not isinstance(metadata, list):
        return None
    for entry in metadata:
        if not isinstance(entry, dict):
            continue
        name = normalize_text(_as_text(entry.get("name")) or "")
        if not any(key in name for key in _SALARY_METADATA_KEYS):
            continue
        value = entry.get("value")
        if isinstance(value, bool) or value is None:
            continue
        text = _as_text(value)
        if text:
            return text
    return None


def _failed(config: SourceConfig, started_at: str, summary: str) -> ScanResult:
    return ScanResult(
        source_id=config.id,
        started_at=started_at,
        finished_at=_now_iso(),
        status=ScanStatus.FAILED,
        records_seen=0,
        records_parsed=0,
        error_summary=summary,
    )


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
