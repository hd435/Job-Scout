"""SmartRecruiters adapter.

Endpoint: `https://api.smartrecruiters.com/v1/companies/{identifier}/postings?limit=100&offset=N`

The company's own SmartRecruiters tenant, read without authentication, paged 100 at a time. Observed
2026-11-14 against `LegalAndGeneral`.

What the payload gives and does not:

- `id` is a stable numeric identifier and appears in nothing else we key on.
- `location.fullLocation` is a ready-made "Cardiff, Wales, United Kingdom"; `location.country` is
  the ISO code, which is what makes a UK test cheap and unambiguous.
- There is **no description in the list**. `expand=true` adds a `jobAd` on some tenants and not on
  others (`LegalAndGeneral` returns none), so the adapter reads titles rather than paying a detail
  request per role. Early-career and family matching both work from titles; the cost is that a role
  whose early-career signal lives only in its prose will need review instead of matching.
- The public apply link is `https://jobs.smartrecruiters.com/{identifier}/{id}-{slug}`, which is the
  employer's own tenant page — a direct application link, not an aggregator redirect.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

from ..config import SourceConfig
from ..http import HttpClient, HttpError
from ..models import Job, ScanResult, ScanStatus, SkipReason
from .base import build_job, uk_employer

POSTINGS_ENDPOINT = "https://api.smartrecruiters.com/v1/companies/{identifier}/postings"
PAGE_SIZE = 100

#: A tenant that never stops paging is a bug or a hostile payload, not a board.
MAX_PAGES = 40


class SmartRecruitersAdapter:
    adapter_id = "smartrecruiters"

    def fetch_scan(
        self,
        config: SourceConfig,
        http_client: HttpClient,
        *,
        known: Mapping[str, str | None] | None = None,
    ) -> ScanResult:
        started_at = _now_iso()
        identifier = str(config.options.get("company") or config.id)
        jobs: list[Job] = []
        skipped: list[SkipReason] = []
        seen_ids: set[str] = set()
        seen_records = 0
        reported_total: int | None = None
        offset = 0
        complete = False

        for _ in range(MAX_PAGES):
            url = (
                f"{POSTINGS_ENDPOINT.format(identifier=identifier)}"
                f"?limit={PAGE_SIZE}&offset={offset}"
            )
            try:
                payload = http_client.get_json(url)
            except HttpError as exc:
                if not jobs:
                    return _failed(config, started_at, exc.summary())
                # A partial board must never baseline silently, so report it rather than succeed.
                return _result(
                    config,
                    started_at,
                    jobs,
                    skipped,
                    seen_records,
                    reported_total,
                    partial_reason=exc.summary(),
                )

            if not isinstance(payload, dict):
                return _failed(config, started_at, "response is not an object")
            records = payload.get("content")
            if not isinstance(records, list):
                return _failed(config, started_at, "response has no content array")
            if reported_total is None:
                total = payload.get("totalFound")
                reported_total = int(total) if isinstance(total, int) else None

            seen_records += len(records)
            for index, record in enumerate(records):
                mapped = _map_record(record, index, config, identifier)
                if isinstance(mapped, SkipReason):
                    skipped.append(mapped)
                    continue
                if mapped.source_job_id in seen_ids:
                    skipped.append(
                        SkipReason(
                            mapped.source_job_id, "duplicate_source_job_id", f"offset={offset}"
                        )
                    )
                    continue
                seen_ids.add(mapped.source_job_id)
                jobs.append(mapped)

            offset += len(records)
            if not records or (reported_total is not None and offset >= reported_total):
                complete = True
                break
            if len(records) < PAGE_SIZE:
                complete = True
                break

        return _result(
            config,
            started_at,
            jobs,
            skipped,
            seen_records,
            reported_total,
            partial_reason=None if complete else f"pagination stopped after {MAX_PAGES} pages",
        )


def _result(
    config: SourceConfig,
    started_at: str,
    jobs: list[Job],
    skipped: list[SkipReason],
    seen_records: int,
    reported_total: int | None,
    *,
    partial_reason: str | None,
) -> ScanResult:
    if partial_reason or skipped:
        status = ScanStatus.PARTIAL
        summary = partial_reason or f"{len(skipped)} record(s) could not be mapped"
    elif not jobs:
        status = ScanStatus.SUCCESS_EMPTY
        summary = None
    else:
        status = ScanStatus.SUCCESS
        summary = None
    return ScanResult(
        source_id=config.id,
        started_at=started_at,
        finished_at=_now_iso(),
        status=status,
        records_seen=seen_records,
        records_parsed=len(jobs),
        jobs=tuple(jobs),
        skipped=tuple(skipped),
        source_reported_total=reported_total,
        error_summary=summary,
    )


def _map_record(record: Any, index: int, config: SourceConfig, identifier: str) -> Job | SkipReason:
    if not isinstance(record, dict):
        return SkipReason(None, "record_not_object", f"index={index}")

    source_job_id = _as_text(record.get("id")) or _as_text(record.get("uuid"))
    if not source_job_id:
        return SkipReason(None, "missing_source_job_id", f"index={index}")

    title = _as_text(record.get("name"))
    if not title:
        return SkipReason(source_job_id, "missing_title", f"index={index}")

    return build_job(
        source_id=config.id,
        source_job_id=source_job_id,
        employer=config.employer,
        title=title,
        location_raw=_location_text(record),
        url=f"https://jobs.smartrecruiters.com/{identifier}/{source_job_id}",
        description="",
        posted_at=_posted_date(record),
        closing_at=None,
        salary_text=None,
        workplace_type=_workplace_type(record),
        source_revision=_as_text(record.get("releasedDate")),
        uk_employer=uk_employer(config),
    )


def _location_text(record: dict[str, Any]) -> str:
    location = record.get("location")
    if isinstance(location, dict):
        full = _as_text(location.get("fullLocation"))
        if full:
            return full
        pieces = [_as_text(location.get(key)) for key in ("city", "region", "country")]
        joined = ", ".join(piece for piece in pieces if piece)
        if joined:
            return joined
    return ""


def _workplace_type(record: dict[str, Any]) -> str | None:
    location = record.get("location")
    if isinstance(location, dict):
        if location.get("remote") is True:
            return "remote"
        if location.get("hybrid") is True:
            return "hybrid"
    return None


def _posted_date(record: dict[str, Any]) -> date | None:
    raw = _as_text(record.get("releasedDate"))
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
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
