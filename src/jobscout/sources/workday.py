"""Workday CXS adapter, shared by Barclays and Lloyds Graduate.

The listing endpoint is a read-only query, so it is the one POST allowed to retry
(`post_json(..., retry=True)`). Telegram sends keep the no-retry default.

**Assumptions pending validation against real payloads.** Everything below is
exercised only against synthetic fixtures; no live request has been made.

1. Listing: `POST https://{host}/wday/cxs/{tenant}/{site}/jobs` with
   `{"appliedFacets": {}, "searchText": "", "limit": N, "offset": M}` returns
   `{"total": <int>, "jobPostings": [...]}`.
2. Identity: the requisition ID is `bulletFields[0]`.
3. Detail: `GET https://{host}/wday/cxs/{tenant}/{site}{externalPath}` returns
   `{"jobPostingInfo": {...}}` containing `location`, `additionalLocations`,
   `jobDescription`, `endDate`, `externalUrl`. A detail response only counts as
   resolved when it supplies a usable, non-placeholder location; otherwise the
   scan is `partial` and the placeholder is left as recorded.
4. Public URL: `https://{host}/{locale}/{site}{externalPath}` when no detail
   lookup was needed; Workday's locale prefix and redirect behaviour is unverified.
5. Multi-location placeholders look like "2 Locations" or "Multiple Locations".
6. `postedOn` is relative text; only "today", "yesterday" and "N days ago" are
   interpreted (against the scan start date), bucket labels such as
   "30+ Days Ago" stay `None`.
7. The sitemap fallback described in the architecture is not implemented in M3;
   completeness comes from the reported `total`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from ..config import SourceConfig
from ..http import HttpClient, HttpError
from ..models import ScanResult, ScanStatus, SkipReason
from .base import build_job

WORKDAY_PAGE_SIZE = 20
WORKDAY_MAX_PAGES = 200
WORKDAY_MAX_DETAIL_LOOKUPS = 10

_PLACEHOLDER_LOCATION = re.compile(
    r"^\s*(?:\d+\s+locations?|multiple locations|various locations)\s*$", re.IGNORECASE
)
_RELATIVE_POSTED = re.compile(
    r"^posted\s+(?:(today|yesterday)|(\d+)(\+)?\s+days?\s+ago)$", re.IGNORECASE
)


class WorkdayHttpClient(HttpClient, Protocol):
    """Workday needs a POST for the listing query; Palantir does not."""

    def post_json(self, url: str, payload: dict[str, Any], *, retry: bool = False) -> Any: ...


@dataclass(frozen=True)
class _Site:
    host: str
    tenant: str
    site: str
    locale: str

    @property
    def cxs_base(self) -> str:
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}"

    @property
    def listing_url(self) -> str:
        return f"{self.cxs_base}/jobs"

    def detail_url(self, external_path: str) -> str:
        return f"{self.cxs_base}{external_path}"

    def public_url(self, external_path: str) -> str:
        return f"https://{self.host}/{self.locale}/{self.site}{external_path}"


@dataclass
class _Posting:
    requisition_id: str
    title: str
    external_path: str
    location_raw: str
    posted_at: date | None
    url: str
    description: str | None = None
    closing_at: date | None = None


class WorkdayAdapter:
    adapter_id = "workday"

    def __init__(
        self,
        *,
        page_size: int = WORKDAY_PAGE_SIZE,
        max_pages: int = WORKDAY_MAX_PAGES,
        max_detail_lookups: int = WORKDAY_MAX_DETAIL_LOOKUPS,
    ) -> None:
        self._page_size = page_size
        self._max_pages = max_pages
        self._max_detail_lookups = max_detail_lookups

    def fetch_scan(
        self,
        config: SourceConfig,
        http_client: WorkdayHttpClient,
        *,
        known: Mapping[str, str | None] | None = None,
    ) -> ScanResult:
        """The listing is complete on its own, so `known` is unused here."""
        started_at = _now_iso()
        reference = _date_of(started_at)
        try:
            site, page_size = _site_from(config, page_size=self._page_size)
        except ValueError as exc:
            return _failed(config, started_at, str(exc))

        try:
            postings, total, notes, complete = self._collect_pages(http_client, site, page_size)
        except (HttpError, _MalformedPayload) as exc:
            summary = exc.summary() if isinstance(exc, HttpError) else str(exc)
            return _failed(config, started_at, summary)

        skipped: list[SkipReason] = []
        drafts: list[_Posting] = []
        seen: set[str] = set()
        for index, record in enumerate(postings):
            mapped = _map_posting(record, index, site, reference)
            if isinstance(mapped, SkipReason):
                skipped.append(mapped)
                continue
            if mapped.requisition_id in seen:
                skipped.append(
                    SkipReason(mapped.requisition_id, "duplicate_source_job_id", f"index={index}")
                )
                continue
            seen.add(mapped.requisition_id)
            drafts.append(mapped)

        complete = self._resolve_locations(http_client, site, drafts, notes) and complete

        jobs = tuple(
            build_job(
                source_id=config.id,
                source_job_id=draft.requisition_id,
                employer=config.employer,
                title=draft.title,
                location_raw=draft.location_raw,
                url=draft.url,
                description=draft.description,
                posted_at=draft.posted_at,
                closing_at=draft.closing_at,
            )
            for draft in drafts
        )

        if skipped or not complete:
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
            records_seen=len(postings),
            records_parsed=len(jobs),
            jobs=jobs,
            skipped=tuple(skipped),
            source_reported_total=total,
            error_summary="; ".join(notes) if notes else None,
        )

    def _collect_pages(
        self, http_client: WorkdayHttpClient, site: _Site, page_size: int
    ) -> tuple[list[dict[str, Any]], int | None, list[str], bool]:
        postings: list[dict[str, Any]] = []
        notes: list[str] = []
        total: int | None = None
        complete = True
        offset = 0
        pages = 0

        while True:
            if pages >= self._max_pages:
                notes.append(f"stopped after {self._max_pages} page(s)")
                complete = False
                break

            payload = http_client.post_json(
                site.listing_url,
                {
                    "appliedFacets": {},
                    "searchText": "",
                    "limit": page_size,
                    "offset": offset,
                },
                retry=True,
            )
            if not isinstance(payload, dict):
                if not postings:
                    raise _MalformedPayload("listing response was not an object")
                notes.append("listing response was not an object")
                complete = False
                break

            if total is None:
                raw_total = payload.get("total")
                usable = (
                    isinstance(raw_total, int)
                    and not isinstance(raw_total, bool)
                    and raw_total >= 0
                )
                if usable:
                    total = raw_total
                else:
                    notes.append("listing response has no usable total")
                    complete = False

            page = payload.get("jobPostings")
            if not isinstance(page, list):
                if not postings:
                    raise _MalformedPayload("listing response has no jobPostings array")
                notes.append("page without a jobPostings array")
                complete = False
                break

            pages += 1
            if not page:
                if total is not None and offset < total:
                    notes.append(f"empty page at offset {offset} before the reported total")
                    complete = False
                break

            postings.extend(item for item in page if isinstance(item, dict))
            malformed = sum(1 for item in page if not isinstance(item, dict))
            if malformed:
                notes.append(f"{malformed} posting(s) were not objects")
                complete = False
            offset += len(page)

            if total is not None and offset >= total:
                break
            if len(page) < page_size:
                # A short page ends the listing; if a total was reported, we fell short of it.
                if total is not None:
                    notes.append(f"short page before the reported total ({offset} of {total})")
                    complete = False
                break

        return postings, total, notes, complete

    def _resolve_locations(
        self,
        http_client: WorkdayHttpClient,
        site: _Site,
        drafts: list[_Posting],
        notes: list[str],
    ) -> bool:
        """Resolve placeholder locations through the detail endpoint, bounded."""
        complete = True
        lookups = 0
        for draft in drafts:
            if not _PLACEHOLDER_LOCATION.match(draft.location_raw):
                continue
            if lookups >= self._max_detail_lookups:
                notes.append(f"detail lookups capped at {self._max_detail_lookups}")
                complete = False
                break
            lookups += 1
            try:
                detail = http_client.get_json(site.detail_url(draft.external_path))
            except HttpError as exc:
                notes.append(f"detail lookup failed for {draft.requisition_id}: {exc.summary()}")
                complete = False
                continue
            problem = _apply_detail(draft, detail)
            if problem is not None:
                notes.append(f"detail lookup for {draft.requisition_id} {problem}")
                complete = False
        return complete


class _MalformedPayload(Exception):
    """Raised internally, then reported as a failed scan."""


def _site_from(config: SourceConfig, *, page_size: int) -> tuple[_Site, int]:
    options = config.options
    missing = [
        name for name in ("host", "tenant", "site") if not str(options.get(name) or "").strip()
    ]
    if missing:
        raise ValueError(
            f"workday source '{config.id}' is missing required options: {', '.join(missing)}"
        )
    configured_page_size = options.get("page_size", page_size)
    if not isinstance(configured_page_size, int) or configured_page_size < 1:
        configured_page_size = page_size
    site = _Site(
        host=str(options["host"]).strip(),
        tenant=str(options["tenant"]).strip(),
        site=str(options["site"]).strip(),
        locale=str(options.get("locale") or "en-US").strip(),
    )
    return site, configured_page_size


def _map_posting(record: Any, index: int, site: _Site, reference: date) -> _Posting | SkipReason:
    if not isinstance(record, dict):
        return SkipReason(None, "record_not_object", f"index={index}")

    requisition_id = _requisition_id(record)
    if not requisition_id:
        return SkipReason(None, "missing_source_job_id", f"index={index}")

    title = _text(record.get("title"))
    if not title:
        return SkipReason(requisition_id, "missing_title", f"index={index}")

    external_path = _text(record.get("externalPath"))
    if not external_path:
        return SkipReason(requisition_id, "missing_url", f"index={index}")
    if not external_path.startswith("/"):
        external_path = f"/{external_path}"

    return _Posting(
        requisition_id=requisition_id,
        title=title,
        external_path=external_path,
        location_raw=_text(record.get("locationsText")) or "",
        posted_at=_posted_date(_text(record.get("postedOn")), reference),
        url=site.public_url(external_path),
    )


def _apply_detail(draft: _Posting, detail: Any) -> str | None:
    """Apply a detail payload to a posting whose location is a placeholder.

    Returns `None` only when the payload supplies a usable location that replaces the
    placeholder. Any other outcome returns a short reason, which the caller records as a
    partial-scan note, so an unresolved placeholder can never leave the scan complete.
    """
    info = detail.get("jobPostingInfo") if isinstance(detail, dict) else None
    if not isinstance(info, dict):
        return "had no jobPostingInfo"

    locations: list[str] = []
    primary = _location_text(info.get("location"))
    if primary:
        locations.append(primary)
    additional = info.get("additionalLocations")
    if isinstance(additional, list):
        for item in additional:
            text = _location_text(item)
            if text:
                locations.append(text)

    usable = [text for text in locations if not _PLACEHOLDER_LOCATION.match(text)]
    if not usable:
        if not locations:
            return "had no usable location"
        return f"had only placeholder locations '{'; '.join(dict.fromkeys(locations))}'"
    joined = "; ".join(dict.fromkeys(usable))

    draft.location_raw = joined
    draft.description = _text(info.get("jobDescription")) or draft.description
    draft.closing_at = _iso_date(_text(info.get("endDate"))) or draft.closing_at
    draft.url = _text(info.get("externalUrl")) or draft.url
    return None


def _location_text(value: Any) -> str | None:
    """A location must be a non-empty string; numbers and objects are unusable."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _requisition_id(record: dict[str, Any]) -> str | None:
    bullets = record.get("bulletFields")
    if isinstance(bullets, list):
        for bullet in bullets:
            text = _text(bullet)
            if text:
                return text
    for key in ("jobRequisitionId", "id"):
        text = _text(record.get(key))
        if text:
            return text
    return None


def _posted_date(raw: str | None, reference: date) -> date | None:
    """Interpret only explicit relative forms; never guess a bucket like '30+ days'."""
    if not raw:
        return None
    match = _RELATIVE_POSTED.match(raw.strip())
    if match:
        if match.group(1):
            word = match.group(1).casefold()
            return reference if word == "today" else reference - timedelta(days=1)
        if match.group(3):
            return None
        return reference - timedelta(days=int(match.group(2)))
    return _iso_date(raw)


def _iso_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _date_of(timestamp: str) -> date:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date()


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
