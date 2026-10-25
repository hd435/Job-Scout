"""Ashby job-board adapter.

Endpoint: `https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true`

One unauthenticated request returns every published role with a plain-text description, the
primary location, any secondary locations and the workplace type, so — like Greenhouse — there
is no pagination and no detail refresh to schedule. Observed 2026-10-25 against the `linear`,
`posthog`, `elevenlabs` and `multiverse` boards.

`isListed=false` records are not on the public board, so they are skipped rather than counted
as extraction failures.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from ..config import SourceConfig
from ..http import HttpClient, HttpError
from ..models import Job, ScanResult, ScanStatus, SkipReason
from .base import build_job, uk_employer
from .htmltext import strip_html

ASHBY_ENDPOINT = "https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"


class AshbyAdapter:
    adapter_id = "ashby"

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
        url = ASHBY_ENDPOINT.format(board=board)

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
        unlisted = 0

        for index, record in enumerate(records):
            if isinstance(record, dict) and record.get("isListed") is False:
                unlisted += 1
                continue
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
            records_seen=len(records) - unlisted,
            records_parsed=len(jobs),
            jobs=tuple(jobs),
            skipped=tuple(skipped),
            source_reported_total=len(records) - unlisted,
            error_summary=(f"{len(skipped)} record(s) could not be mapped" if skipped else None),
        )


def _map_record(record: Any, index: int, config: SourceConfig) -> Job | SkipReason:
    if not isinstance(record, dict):
        return SkipReason(None, "record_not_object", f"index={index}")

    source_job_id = _as_text(record.get("id"))
    if not source_job_id:
        return SkipReason(None, "missing_source_job_id", f"index={index}")

    title = _as_text(record.get("title"))
    if not title:
        return SkipReason(source_job_id, "missing_title", f"index={index}")

    url = _as_text(record.get("jobUrl"))
    if not url:
        return SkipReason(source_job_id, "missing_url", f"index={index}")

    return build_job(
        source_id=config.id,
        source_job_id=source_job_id,
        employer=config.employer,
        title=title,
        location_raw=_location_text(record),
        url=url,
        description=_as_text(record.get("descriptionPlain"))
        or strip_html(_as_text(record.get("descriptionHtml"))),
        posted_at=_posted_date(record),
        closing_at=None,
        salary_text=_salary_text(record),
        workplace_type=_as_text(record.get("workplaceType")),
        source_revision=_as_text(record.get("publishedAt")),
        uk_employer=uk_employer(config),
    )


def _location_text(record: dict[str, Any]) -> str:
    """Primary location first, then any secondary ones, semicolon-joined.

    `normalize_location` splits on separators and accepts the first qualifying segment, so a
    role that is 'London' plus 'Remote (EMEA)' still qualifies on London.
    """
    parts: list[str] = []
    primary = _as_text(record.get("location"))
    if primary:
        parts.append(primary)
    secondary = record.get("secondaryLocations")
    if isinstance(secondary, list):
        for entry in secondary:
            name = entry.get("location") if isinstance(entry, dict) else entry
            text = _as_text(name)
            if text:
                parts.append(text)
    return "; ".join(dict.fromkeys(parts))


def _posted_date(record: dict[str, Any]) -> Any:
    raw = _as_text(record.get("publishedAt"))
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _salary_text(record: dict[str, Any]) -> str | None:
    compensation = record.get("compensation")
    if not isinstance(compensation, dict):
        return None
    return _as_text(compensation.get("compensationTierSummary")) or _as_text(
        compensation.get("scrapeableCompensationSalarySummary")
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
