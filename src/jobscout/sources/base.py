"""Source adapter contract and shared job-building helpers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Protocol

from ..config import SourceConfig
from ..http import HttpClient
from ..matching import normalize_location
from ..models import Job, ScanResult, SkipReason, compute_content_hash


class AdapterError(Exception):
    """Unknown or misconfigured adapter."""


class SourceAdapter(Protocol):
    """Adapters own HTTP access and mapping. They never touch storage or Telegram.

    `known` maps source job ids to the revision marker stored for that role, so a
    sitemap-driven adapter can refresh only new or changed detail pages. Adapters that
    read the whole source in one response ignore it.
    """

    adapter_id: str

    def fetch_scan(
        self,
        config: SourceConfig,
        http_client: HttpClient,
        *,
        known: Mapping[str, str | None] | None = None,
    ) -> ScanResult: ...


def build_job(
    *,
    source_id: str,
    source_job_id: str,
    employer: str,
    title: str,
    location_raw: str,
    url: str,
    description: str | None = None,
    posted_at: date | None = None,
    closing_at: date | None = None,
    salary_text: str | None = None,
    workplace_type: str | None = None,
    source_revision: str | None = None,
) -> Job:
    location = normalize_location(location_raw, workplace_type)
    content_hash = compute_content_hash(
        source_id=source_id,
        source_job_id=source_job_id,
        employer=employer,
        title=title,
        location_raw=location_raw,
        url=url,
        description=description,
        posted_at=posted_at,
        closing_at=closing_at,
        salary_text=salary_text,
        workplace_type=workplace_type,
    )
    return Job(
        source_id=source_id,
        source_job_id=source_job_id,
        employer=employer,
        title=title,
        location_raw=location_raw,
        location_normalized=location,
        url=url,
        content_hash=content_hash,
        description=description,
        posted_at=posted_at,
        closing_at=closing_at,
        salary_text=salary_text,
        workplace_type=workplace_type,
        source_revision=source_revision,
    )


__all__ = ["AdapterError", "SkipReason", "SourceAdapter", "build_job"]
