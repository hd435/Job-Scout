"""Recruitee adapter.

Endpoint: `https://{account}.recruitee.com/api/offers/`

One unauthenticated request returns every published offer for an account, including description
and requirements as HTML, so there is no pagination and no detail refresh. Observed 2026-11-13
against the `theeventacademy` account.

Fields worth noting, because they are richer than the other ATS payloads:

- `guid` is a short stable identifier; `id` is numeric. `guid` is preferred because it appears in
  nothing else we key on and survives the offer being re-published.
- `careers_url` is the public offer page — the direct link a user should click.
- `location` is a ready-made "London, England, United Kingdom" string; `locations` may hold more
  than one site for a multi-location offer.
- `remote` and `hybrid` describe the workplace; `experience_code` says `entry_level`, which is
  recorded in the description's place only insofar as the matcher already reads titles.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

from ..config import SourceConfig
from ..http import HttpClient, HttpError
from ..models import Job, ScanResult, ScanStatus, SkipReason
from .base import build_job, uk_employer
from .htmltext import strip_html

RECRUITEE_ENDPOINT = "https://{account}.recruitee.com/api/offers/"


class RecruiteeAdapter:
    adapter_id = "recruitee"

    def fetch_scan(
        self,
        config: SourceConfig,
        http_client: HttpClient,
        *,
        known: Mapping[str, str | None] | None = None,
    ) -> ScanResult:
        """The account returns every offer in one response, so `known` is unused."""
        started_at = _now_iso()
        account = str(config.options.get("account") or config.id)
        url = RECRUITEE_ENDPOINT.format(account=account)

        try:
            payload = http_client.get_json(url)
        except HttpError as exc:
            return _failed(config, started_at, exc.summary())

        if not isinstance(payload, dict):
            return _failed(config, started_at, f"unexpected payload type: {type(payload).__name__}")
        records = payload.get("offers")
        if not isinstance(records, list):
            return _failed(config, started_at, "response has no offers array")

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

    source_job_id = _as_text(record.get("guid")) or _as_text(record.get("id"))
    if not source_job_id:
        return SkipReason(None, "missing_source_job_id", f"index={index}")

    title = _as_text(record.get("title"))
    if not title:
        return SkipReason(source_job_id, "missing_title", f"index={index}")

    url = _as_text(record.get("careers_url")) or _as_text(record.get("careers_apply_url"))
    if not url:
        return SkipReason(source_job_id, "missing_url", f"index={index}")

    return build_job(
        source_id=config.id,
        source_job_id=source_job_id,
        employer=config.employer,
        title=title,
        location_raw=_location_text(record),
        url=url,
        description=_description(record),
        posted_at=_posted_date(record),
        closing_at=None,
        salary_text=None,
        workplace_type=_workplace_type(record),
        source_revision=_as_text(record.get("published_at")) or _as_text(record.get("created_at")),
        uk_employer=uk_employer(config),
    )


def _location_text(record: dict[str, Any]) -> str:
    """The ready-made `location`, plus any additional `locations` entries, semicolon-joined."""
    parts: list[str] = []
    primary = _as_text(record.get("location"))
    if primary:
        parts.append(primary)
    extra = record.get("locations")
    if isinstance(extra, list):
        for entry in extra:
            if not isinstance(entry, dict):
                continue
            pieces = [_as_text(entry.get(key)) for key in ("name", "state", "country")]
            joined = ", ".join(piece for piece in pieces if piece)
            if joined:
                parts.append(joined)
    if not parts:
        for key in ("city", "country"):
            text = _as_text(record.get(key))
            if text:
                parts.append(text)
    return "; ".join(dict.fromkeys(parts))


def _description(record: dict[str, Any]) -> str:
    """Description plus requirements, so early-career wording in either reaches the matcher."""
    pieces = [strip_html(_as_text(record.get("description")) or "")]
    pieces.append(strip_html(_as_text(record.get("requirements")) or ""))
    return "\n".join(piece for piece in pieces if piece).strip()


def _workplace_type(record: dict[str, Any]) -> str | None:
    if record.get("remote") is True:
        return "remote"
    if record.get("hybrid") is True:
        return "hybrid"
    return None


def _posted_date(record: dict[str, Any]) -> date | None:
    for key in ("published_at", "created_at"):
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
