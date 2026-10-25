"""Workable adapter.

Endpoint: `https://apply.workable.com/api/v1/widget/accounts/{account}?details=true`

One unauthenticated request returns every published role for an account, including an HTML
description, so there is no pagination and no detail refresh. Observed 2026-10-25 against the
`zoopla` account.

Workable has no numeric job id in this payload; `shortcode` is the identifier that appears in the
public URL (`https://apply.workable.com/j/8E20F2A1BD`), so it is the source job id and it is stable
across scans.
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

WORKABLE_ENDPOINT = "https://apply.workable.com/api/v1/widget/accounts/{account}?details=true"


class WorkableAdapter:
    adapter_id = "workable"

    def fetch_scan(
        self,
        config: SourceConfig,
        http_client: HttpClient,
        *,
        known: Mapping[str, str | None] | None = None,
    ) -> ScanResult:
        """The account returns every role in one response, so `known` is unused."""
        started_at = _now_iso()
        account = str(config.options.get("account") or config.id)
        url = WORKABLE_ENDPOINT.format(account=account)

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
            source_reported_total=len(records),
            error_summary=(f"{len(skipped)} record(s) could not be mapped" if skipped else None),
        )


def _map_record(record: Any, index: int, config: SourceConfig) -> Job | SkipReason:
    if not isinstance(record, dict):
        return SkipReason(None, "record_not_object", f"index={index}")

    source_job_id = _as_text(record.get("shortcode")) or _as_text(record.get("code"))
    if not source_job_id:
        return SkipReason(None, "missing_source_job_id", f"index={index}")

    title = _as_text(record.get("title"))
    if not title:
        return SkipReason(source_job_id, "missing_title", f"index={index}")

    url = _as_text(record.get("url")) or _as_text(record.get("shortlink"))
    if not url:
        return SkipReason(source_job_id, "missing_url", f"index={index}")

    workplace = "remote" if record.get("telecommuting") is True else None

    return build_job(
        source_id=config.id,
        source_job_id=source_job_id,
        employer=config.employer,
        title=title,
        location_raw=_location_text(record),
        url=url,
        description=strip_html(_as_text(record.get("description"))),
        posted_at=_posted_date(record),
        closing_at=None,
        salary_text=None,
        workplace_type=workplace,
        source_revision=_as_text(record.get("published_on")) or _as_text(record.get("created_at")),
        uk_employer=uk_employer(config),
    )


def _location_text(record: dict[str, Any]) -> str:
    """City/region/country plus any additional `locations` entries, semicolon-joined."""
    parts: list[str] = []
    for key in ("city", "state", "country"):
        text = _as_text(record.get(key))
        if text:
            parts.append(text)
    extra = record.get("locations")
    if isinstance(extra, list):
        for entry in extra:
            if not isinstance(entry, dict):
                continue
            pieces = [_as_text(entry.get(key)) for key in ("city", "region", "country")]
            joined = ", ".join(piece for piece in pieces if piece)
            if joined:
                parts.append(joined)
    return "; ".join(dict.fromkeys(parts))


def _posted_date(record: dict[str, Any]) -> Any:
    for key in ("published_on", "created_at"):
        raw = _as_text(record.get(key))
        if not raw:
            continue
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
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
