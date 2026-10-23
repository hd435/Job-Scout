"""Lever postings adapter (used for Palantir in M1).

Endpoint: https://api.lever.co/v0/postings/{site}?mode=json
Lever's robots rules allow the API with a 1 second crawl delay, which is why the
adapter makes exactly one request per scan.
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

LEVER_ENDPOINT = "https://api.lever.co/v0/postings/{site}?mode=json"

_CURRENCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€"}
_SALARY_INTERVALS = {
    "per-year-salary": "per year",
    "per-month-salary": "per month",
    "per-hour-wage": "per hour",
    "per-week-wage": "per week",
    "per-day-wage": "per day",
}


class LeverAdapter:
    adapter_id = "lever"

    def fetch_scan(
        self,
        config: SourceConfig,
        http_client: HttpClient,
        *,
        known: Mapping[str, str | None] | None = None,
    ) -> ScanResult:
        """Lever returns every posting in one response, so `known` is unused."""
        started_at = _now_iso()
        site = str(config.options.get("site") or config.id)
        url = LEVER_ENDPOINT.format(site=site)

        try:
            payload = http_client.get_json(url)
        except HttpError as exc:
            return _failed(config, started_at, exc.summary())

        if not isinstance(payload, list):
            return _failed(
                config,
                started_at,
                f"unexpected payload type: {type(payload).__name__}",
            )

        jobs: list[Job] = []
        skipped: list[SkipReason] = []
        seen_ids: set[str] = set()

        for index, record in enumerate(payload):
            job_or_skip = _map_record(record, index, config)
            if isinstance(job_or_skip, SkipReason):
                skipped.append(job_or_skip)
                continue
            if job_or_skip.source_job_id in seen_ids:
                skipped.append(
                    SkipReason(
                        source_job_id=job_or_skip.source_job_id,
                        reason="duplicate_source_job_id",
                        detail=f"index={index}",
                    )
                )
                continue
            seen_ids.add(job_or_skip.source_job_id)
            jobs.append(job_or_skip)

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
            records_seen=len(payload),
            records_parsed=len(jobs),
            jobs=tuple(jobs),
            skipped=tuple(skipped),
            source_reported_total=len(payload),
            error_summary=(f"{len(skipped)} record(s) could not be mapped" if skipped else None),
        )


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


def _map_record(record: Any, index: int, config: SourceConfig) -> Job | SkipReason:
    if not isinstance(record, dict):
        return SkipReason(None, "record_not_object", f"index={index}")

    source_job_id = _as_text(record.get("id"))
    if not source_job_id:
        return SkipReason(None, "missing_source_job_id", f"index={index}")

    title = _as_text(record.get("text"))
    if not title:
        return SkipReason(source_job_id, "missing_title", f"index={index}")

    url = _as_text(record.get("hostedUrl"))
    if not url:
        return SkipReason(source_job_id, "missing_url", f"index={index}")

    return build_job(
        source_id=config.id,
        source_job_id=source_job_id,
        employer=config.employer,
        title=title,
        location_raw=_location_text(record),
        url=url,
        description=_as_text(record.get("descriptionPlain")),
        posted_at=_posted_date(record.get("createdAt")),
        closing_at=None,
        salary_text=_format_salary(record.get("salaryRange")),
        workplace_type=_as_text(record.get("workplaceType")),
        uk_employer=uk_employer(config),
    )


def _location_text(record: dict[str, Any]) -> str:
    categories = record.get("categories")
    if not isinstance(categories, dict):
        return ""
    all_locations = categories.get("allLocations")
    if isinstance(all_locations, list):
        cleaned = [_as_text(item) for item in all_locations]
        text = "; ".join(dict.fromkeys(item for item in cleaned if item))
        if text:
            return text
    return _as_text(categories.get("location")) or ""


def _posted_date(value: Any) -> Any:
    """Lever reports createdAt as epoch milliseconds. Never guess a relative date."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).date()
    except (OverflowError, OSError, ValueError):
        return None


def _format_salary(salary_range: Any) -> str | None:
    if not isinstance(salary_range, dict):
        return None
    low = salary_range.get("min")
    high = salary_range.get("max")
    if not isinstance(low, int) and not isinstance(high, int):
        return None
    currency = _as_text(salary_range.get("currency")) or ""
    symbol = _CURRENCY_SYMBOLS.get(currency.upper(), "")
    prefix = symbol or (f"{currency.upper()} " if currency else "")

    if isinstance(low, int) and isinstance(high, int):
        amount = f"{prefix}{low:,} - {prefix}{high:,}".strip()
    else:
        value = low if isinstance(low, int) else high
        amount = f"{prefix}{value:,}".strip()

    interval = _SALARY_INTERVALS.get(normalize_text(str(salary_range.get("interval") or "")))
    return f"{amount} {interval}" if interval else amount
