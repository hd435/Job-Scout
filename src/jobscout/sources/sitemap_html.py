"""Sitemap discovery plus server-rendered job page adapter (Wise and Arm in M4).

**Assumptions pending validation against real payloads.** No live request has been made;
everything below is exercised only against synthetic fixtures.

1. Discovery: a sitemap index or urlset lists job URLs with a `lastmod`. Wise publishes a
   vacancy sitemap referenced from its sitemap index; Arm's sitemap is a single urlset.
2. Identity: the source job id is taken from the job URL by the profile's pattern rather
   than from the page body.
3. Detail pages publish a schema.org `JobPosting` JSON-LD block. When they do not, only
   the title is recovered and the location stays unknown, which makes the role a
   `needs_review` candidate instead of guessing a location.
4. `lastmod` changes whenever a role's details change. A role whose stored `lastmod`
   still matches is not re-fetched; its details are kept and only the observation is
   recorded.
5. Both sitemaps allow crawling; Wise query-string job pages and Arm search-result paths
   are never requested. Detail requests are spaced by `request_delay` seconds.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

from ..config import SourceConfig
from ..http import HttpError
from ..models import Job, ScanResult, ScanStatus, SkipReason
from .base import build_job, uk_employer
from .htmltext import HtmlDocument, strip_html

SITEMAP_MAX_DOCUMENTS = 5
SITEMAP_MAX_DETAIL_FETCHES = 400
SITEMAP_REQUEST_DELAY = 0.5

_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
_UNSET = object()


class HtmlClient(Protocol):
    """Sitemaps and job pages need raw text, not JSON."""

    def get_text(self, url: str) -> str: ...


@dataclass(frozen=True)
class _Profile:
    name: str
    sitemap_url: str
    job_url_pattern: re.Pattern[str]
    sitemap_child_pattern: re.Pattern[str] | None = None


_PROFILES: dict[str, _Profile] = {
    "wise": _Profile(
        name="wise",
        sitemap_url="https://wise.jobs/sitemap.xml",
        sitemap_child_pattern=re.compile(r"vacanc", re.IGNORECASE),
        job_url_pattern=re.compile(
            r"^https://wise\.jobs/jobs/(?P<slug>[A-Za-z0-9\-]+?)-jid-(?P<id>\d+)/?$"
        ),
    ),
    "arm": _Profile(
        name="arm",
        sitemap_url="https://careers.arm.com/sitemap.xml",
        job_url_pattern=re.compile(
            r"^https://careers\.arm\.com/job/(?P<city>[^/?#]+)/(?P<slug>[^/?#]+)/(?P<id>\d+)/?$"
        ),
    ),
}

_CURRENCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€"}
_SALARY_UNITS = {
    "year": "per year",
    "month": "per month",
    "week": "per week",
    "day": "per day",
    "hour": "per hour",
}


@dataclass(frozen=True)
class _SitemapEntry:
    job_id: str
    url: str
    revision: str | None


class SitemapHtmlAdapter:
    adapter_id = "sitemap_html"

    def __init__(
        self,
        *,
        profile: str | None = None,
        max_sitemap_documents: int = SITEMAP_MAX_DOCUMENTS,
        max_detail_fetches: int = SITEMAP_MAX_DETAIL_FETCHES,
        request_delay: float = SITEMAP_REQUEST_DELAY,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._profile_name = profile
        self._max_sitemap_documents = max_sitemap_documents
        self._max_detail_fetches = max_detail_fetches
        self._request_delay = request_delay
        self._sleep = sleep

    def fetch_scan(
        self,
        config: SourceConfig,
        http_client: HtmlClient,
        *,
        known: Mapping[str, str | None] | None = None,
    ) -> ScanResult:
        started_at = _now_iso()
        profile = _resolve_profile(config, self._profile_name)
        if profile is None:
            return _failed(
                config,
                started_at,
                f"unknown sitemap profile '{config.options.get('profile')}';"
                f" known profiles: {', '.join(sorted(_PROFILES))}",
            )

        notes: list[str] = []
        incomplete = False
        try:
            entries, discovery_notes, discovery_incomplete = self._collect_entries(
                http_client, profile, config
            )
        except HttpError as exc:
            return _failed(config, started_at, exc.summary())
        except _MalformedSitemap as exc:
            return _failed(config, started_at, str(exc))
        notes.extend(discovery_notes)
        incomplete = incomplete or discovery_incomplete

        matched, duplicates = _match_entries(entries, profile)
        skipped: list[SkipReason] = list(duplicates)
        if entries and not matched:
            notes.append(
                f"none of {len(entries)} sitemap URL(s) matched the {profile.name} job URL pattern"
            )
            incomplete = True

        to_fetch, observed_only = _plan_refreshes(matched, known)
        max_detail_fetches = _option_int(
            config.options, "max_detail_fetches", self._max_detail_fetches
        )
        delay = _option_float(config.options, "request_delay_seconds", self._request_delay)
        if len(to_fetch) > max_detail_fetches:
            notes.append(
                f"detail fetches capped at {max_detail_fetches} ({len(to_fetch)} need refreshing)"
            )
            observed_only.extend(entry.job_id for entry in to_fetch[max_detail_fetches:])
            to_fetch = to_fetch[:max_detail_fetches]
            incomplete = True

        jobs: list[Job] = []
        fallback_pages = 0
        for position, entry in enumerate(to_fetch):
            if position and delay:
                self._sleep(delay)
            try:
                markup = http_client.get_text(entry.url)
            except HttpError as exc:
                skipped.append(SkipReason(entry.job_id, "detail_request_failed", exc.summary()))
                continue
            mapped, used_fallback = _map_page(markup, entry, config)
            if isinstance(mapped, SkipReason):
                skipped.append(mapped)
                continue
            fallback_pages += int(used_fallback)
            jobs.append(mapped)

        if fallback_pages:
            # Informational: a page without JSON-LD still yields a title and URL, and its
            # unknown location keeps the role out of automatic matches.
            notes.append(
                f"{fallback_pages} page(s) had no JobPosting JSON-LD;"
                " title-only fallback used, location unknown"
            )

        refreshed = {job.source_job_id for job in jobs}
        observed_ids = tuple(
            job_id for job_id in dict.fromkeys(observed_only) if job_id not in refreshed
        )

        if skipped or incomplete:
            status = ScanStatus.PARTIAL
        elif not matched:
            status = ScanStatus.SUCCESS_EMPTY
        else:
            status = ScanStatus.SUCCESS

        return ScanResult(
            source_id=config.id,
            started_at=started_at,
            finished_at=_now_iso(),
            status=status,
            records_seen=len(matched),
            records_parsed=len(jobs) + len(observed_ids),
            jobs=tuple(jobs),
            skipped=tuple(skipped),
            source_reported_total=len(matched),
            error_summary="; ".join(notes) if notes else None,
            observed_ids=observed_ids,
        )

    def _collect_entries(
        self,
        http_client: HtmlClient,
        profile: _Profile,
        config: SourceConfig,
    ) -> tuple[list[tuple[str, str | None]], list[str], bool]:
        """Return (loc, lastmod) pairs, following one index level."""
        root_url = str(config.options.get("sitemap") or profile.sitemap_url)
        notes: list[str] = []
        incomplete = False
        documents = 0
        pending = [root_url]
        entries: list[tuple[str, str | None]] = []

        while pending:
            url = pending.pop(0)
            if documents >= self._max_sitemap_documents:
                notes.append(f"sitemap documents capped at {self._max_sitemap_documents}")
                incomplete = True
                break
            documents += 1
            markup = http_client.get_text(url)
            root = _parse_xml(markup, url)
            if root.tag == f"{_SITEMAP_NS}sitemapindex":
                children = [
                    _text(child.find(f"{_SITEMAP_NS}loc"))
                    for child in root.findall(f"{_SITEMAP_NS}sitemap")
                ]
                selected = [
                    child
                    for child in children
                    if child
                    and (
                        profile.sitemap_child_pattern is None
                        or profile.sitemap_child_pattern.search(child)
                    )
                ]
                if not selected:
                    notes.append(f"no child sitemap matched in {url}")
                    incomplete = True
                pending.extend(selected)
                continue
            if root.tag != f"{_SITEMAP_NS}urlset":
                raise _MalformedSitemap(f"unexpected sitemap root element: {root.tag}")
            for element in root.iter(f"{_SITEMAP_NS}url"):
                loc = _text(element.find(f"{_SITEMAP_NS}loc"))
                if not loc:
                    continue
                lastmod = _text(element.find(f"{_SITEMAP_NS}lastmod"))
                entries.append((loc, lastmod))
        return entries, notes, incomplete


class _MalformedSitemap(Exception):
    """A sitemap that cannot be parsed or is not a sitemap at all."""


def _resolve_profile(config: SourceConfig, fallback: str | None) -> _Profile | None:
    name = str(config.options.get("profile") or fallback or config.id).strip().lower()
    return _PROFILES.get(name)


def _option_int(options: dict[str, Any], name: str, fallback: int) -> int:
    value = options.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return fallback
    return value


def _option_float(options: dict[str, Any], name: str, fallback: float) -> float:
    value = options.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return fallback
    return float(value)


def _parse_xml(markup: str, url: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(markup)
    except ElementTree.ParseError as exc:
        raise _MalformedSitemap(f"sitemap at {url} is not valid XML: {exc}") from exc


def _text(element: ElementTree.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    text = element.text.strip()
    return text or None


def _match_entries(
    entries: list[tuple[str, str | None]], profile: _Profile
) -> tuple[list[_SitemapEntry], list[SkipReason]]:
    matched: list[_SitemapEntry] = []
    skipped: list[SkipReason] = []
    seen: set[str] = set()
    for loc, lastmod in entries:
        match = profile.job_url_pattern.match(loc)
        if match is None:
            continue
        job_id = (match.groupdict().get("id") or "").strip()
        if not job_id:
            skipped.append(SkipReason(None, "missing_source_job_id", loc))
            continue
        if job_id in seen:
            skipped.append(SkipReason(job_id, "duplicate_source_job_id", loc))
            continue
        seen.add(job_id)
        matched.append(_SitemapEntry(job_id=job_id, url=loc, revision=lastmod))
    return matched, skipped


def _plan_refreshes(
    matched: list[_SitemapEntry], known: Mapping[str, str | None] | None
) -> tuple[list[_SitemapEntry], list[str]]:
    """Fetch only roles that are new, unrevisioned, or whose revision changed."""
    if not known:
        return list(matched), []
    to_fetch: list[_SitemapEntry] = []
    observed_only: list[str] = []
    for entry in matched:
        stored = known.get(entry.job_id, _UNSET)
        if stored is _UNSET or stored is None or entry.revision is None or stored != entry.revision:
            to_fetch.append(entry)
        else:
            observed_only.append(entry.job_id)
    return to_fetch, observed_only


def _map_page(
    markup: str, entry: _SitemapEntry, config: SourceConfig
) -> tuple[Job | SkipReason, bool]:
    document = HtmlDocument(markup)
    posting = document.job_posting()

    if posting is not None:
        title = _text_value(posting.get("title"))
        if not title:
            return SkipReason(entry.job_id, "missing_title", "JobPosting without title"), False
        return (
            build_job(
                source_id=config.id,
                source_job_id=entry.job_id,
                employer=config.employer,
                title=title,
                location_raw=_locations_text(posting),
                url=entry.url,
                description=strip_html(_text_value(posting.get("description"))),
                posted_at=_iso_date(posting.get("datePosted")),
                closing_at=_iso_date(posting.get("validThrough")),
                salary_text=_salary_text(posting.get("baseSalary")),
                workplace_type=None,
                source_revision=entry.revision,
                uk_employer=uk_employer(config),
            ),
            False,
        )

    heading = document.headings[0] if document.headings else None
    title = heading or _text_value(document.meta.get("og:title"))
    if not title:
        return (
            SkipReason(entry.job_id, "missing_title", "no JobPosting JSON-LD or heading"),
            False,
        )
    return (
        build_job(
            source_id=config.id,
            source_job_id=entry.job_id,
            employer=config.employer,
            title=title,
            location_raw="",
            url=entry.url,
            description=None,
            posted_at=_iso_date(document.times[0]) if document.times else None,
            closing_at=None,
            salary_text=None,
            workplace_type=None,
            source_revision=entry.revision,
        ),
        True,
    )


def _locations_text(posting: dict[str, Any]) -> str:
    raw = posting.get("jobLocation")
    candidates = raw if isinstance(raw, list) else [raw]
    locations: list[str] = []
    for candidate in candidates:
        text = _address_text(candidate)
        if text:
            locations.append(text)
    return "; ".join(dict.fromkeys(locations))


def _address_text(candidate: Any) -> str | None:
    if isinstance(candidate, str):
        return _text_value(candidate)
    if not isinstance(candidate, dict):
        return None
    address = candidate.get("address")
    if isinstance(address, str):
        return _text_value(address)
    if not isinstance(address, dict):
        return _text_value(candidate.get("name"))
    parts = [
        _text_value(address.get("addressLocality")),
        _text_value(address.get("addressRegion")),
        _text_value(address.get("addressCountry")),
    ]
    joined = ", ".join(part for part in parts if part)
    return joined or _text_value(candidate.get("name"))


def _salary_text(base_salary: Any) -> str | None:
    if not isinstance(base_salary, dict):
        return None
    currency = str(_text_value(base_salary.get("currency")) or "").upper()
    prefix = _CURRENCY_SYMBOLS.get(currency, f"{currency} " if currency else "")
    value = base_salary.get("value")
    if not isinstance(value, dict):
        amount = _money(value)
        return f"{prefix}{amount}" if amount else None
    low = _money(value.get("minValue"))
    high = _money(value.get("maxValue"))
    single = _money(value.get("value"))
    if low and high:
        amount = f"{prefix}{low} - {prefix}{high}"
    elif single or low or high:
        amount = f"{prefix}{single or low or high}"
    else:
        return None
    unit = _SALARY_UNITS.get(str(_text_value(value.get("unitText")) or "").lower())
    return f"{amount} {unit}" if unit else amount


def _money(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return f"{round(value):,}"


def _iso_date(value: Any) -> date | None:
    text = _text_value(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _text_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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
