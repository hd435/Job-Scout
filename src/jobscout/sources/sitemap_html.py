"""Sitemap discovery plus server-rendered job page adapter.

A generic source shape, driven entirely by configuration (v0.5 increment 0.3):

1. Discovery: one or more sitemaps (an index is followed one level) list job URLs with a
   `lastmod`.
2. Identity: the source job id comes from a named `id` group in `job_url_pattern`, so the
   URL is the identifier and the page body never has to be trusted for it.
3. Detail pages publish a schema.org `JobPosting` JSON-LD block. Without one, only the
   title is recovered: by default the location stays unknown, which keeps the role in
   review rather than guessing. `location_from = "url"` uses a named `location` group from
   the URL instead, for sources that put the town in the path.
4. `lastmod` changes whenever a role's details change. A role whose stored `lastmod` still
   matches is not re-fetched; its details are kept and only the observation is recorded.
5. Detail requests are spaced by `request_delay_seconds`, sitemap documents and detail
   fetches are capped, and only URLs matching the pattern are ever requested, so a
   search-result path can never be crawled.
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
class _SourceSpec:
    """Resolved from `[sources.options]`: what to read and how to recognise a role."""

    sitemaps: tuple[str, ...]
    job_url_pattern: re.Pattern[str]
    child_pattern: re.Pattern[str] | None
    location_from_url: bool


class _Misconfigured(Exception):
    """Options that cannot describe a usable source; reported as a failed scan."""


def _option_spec(config: SourceConfig) -> _SourceSpec:
    raw_pattern = str(config.options.get("job_url_pattern") or "").strip()
    if not raw_pattern:
        raise _Misconfigured(
            "sitemap_html requires options.job_url_pattern, a regular expression matching"
            " job URLs with a named group (?P<id>...)"
        )
    try:
        job_url_pattern = re.compile(raw_pattern)
    except re.error as exc:
        raise _Misconfigured(f"options.job_url_pattern is not a valid regex: {exc}") from exc
    if "id" not in job_url_pattern.groupindex:
        raise _Misconfigured(
            "options.job_url_pattern needs a named group (?P<id>...) to give each role a"
            " stable source job id"
        )

    child_raw = str(config.options.get("sitemap_child_pattern") or "").strip()
    try:
        child_pattern = re.compile(child_raw) if child_raw else None
    except re.error as exc:
        raise _Misconfigured(f"options.sitemap_child_pattern is not a valid regex: {exc}") from exc

    raw_urls = config.options.get("sitemap_urls")
    sitemaps: list[str] = []
    if raw_urls is not None:
        if not isinstance(raw_urls, list) or not all(isinstance(url, str) for url in raw_urls):
            raise _Misconfigured("options.sitemap_urls must be an array of URLs")
        sitemaps.extend(url.strip() for url in raw_urls if url.strip())
    single = str(config.options.get("sitemap") or "").strip()
    if single and single not in sitemaps:
        sitemaps.insert(0, single)
    if not sitemaps:
        raise _Misconfigured(
            "sitemap_html requires options.sitemap_urls (or options.sitemap) to find the board"
        )

    location_from = str(config.options.get("location_from") or "").strip().lower()
    if location_from not in {"", "url"}:
        raise _Misconfigured(
            f"options.location_from = {location_from!r} is not supported (only 'url', or omit it)"
        )
    if location_from == "url" and "location" not in job_url_pattern.groupindex:
        raise _Misconfigured(
            "options.location_from = 'url' needs a named group (?P<location>...) in job_url_pattern"
        )

    return _SourceSpec(
        sitemaps=tuple(sitemaps),
        job_url_pattern=job_url_pattern,
        child_pattern=child_pattern,
        location_from_url=location_from == "url",
    )


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
    location_hint: str | None = None


class SitemapHtmlAdapter:
    adapter_id = "sitemap_html"

    def __init__(
        self,
        *,
        max_sitemap_documents: int = SITEMAP_MAX_DOCUMENTS,
        max_detail_fetches: int = SITEMAP_MAX_DETAIL_FETCHES,
        request_delay: float = SITEMAP_REQUEST_DELAY,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
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
        try:
            spec = _option_spec(config)
        except _Misconfigured as exc:
            return _failed(config, started_at, str(exc))

        notes: list[str] = []
        incomplete = False
        try:
            entries, discovery_notes, discovery_incomplete = self._collect_entries(
                http_client, spec, config
            )
        except HttpError as exc:
            return _failed(config, started_at, exc.summary())
        except _MalformedSitemap as exc:
            return _failed(config, started_at, str(exc))
        notes.extend(discovery_notes)
        incomplete = incomplete or discovery_incomplete

        matched, duplicates = _match_entries(entries, spec)
        skipped: list[SkipReason] = list(duplicates)
        if entries and not matched:
            notes.append(f"none of {len(entries)} sitemap URL(s) matched options.job_url_pattern")
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
            mapped, used_fallback = _map_page(markup, entry, config, spec)
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
                " title-only fallback used"
                + (" (location taken from the URL)" if spec.location_from_url else "")
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
        spec: _SourceSpec,
        config: SourceConfig,
    ) -> tuple[list[tuple[str, str | None]], list[str], bool]:
        """Return (loc, lastmod) pairs, following one index level."""
        notes: list[str] = []
        incomplete = False
        documents = 0
        pending = list(dict.fromkeys(spec.sitemaps))
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
                    if child and (spec.child_pattern is None or spec.child_pattern.search(child))
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
    entries: list[tuple[str, str | None]], spec: _SourceSpec
) -> tuple[list[_SitemapEntry], list[SkipReason]]:
    matched: list[_SitemapEntry] = []
    skipped: list[SkipReason] = []
    seen: set[str] = set()
    for loc, lastmod in entries:
        match = spec.job_url_pattern.match(loc)
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
        groups = match.groupdict()
        hint = (groups.get("location") or "").strip() or None
        matched.append(_SitemapEntry(job_id=job_id, url=loc, revision=lastmod, location_hint=hint))
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
    markup: str,
    entry: _SitemapEntry,
    config: SourceConfig,
    spec: _SourceSpec,
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
                location_raw=_locations_text(posting) or _url_location(entry, spec),
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
            location_raw=_url_location(entry, spec),
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


def _url_location(entry: _SitemapEntry, spec: _SourceSpec) -> str:
    """The town from the URL, for sources that put it there and publish no JSON-LD address."""
    if spec.location_from_url and entry.location_hint:
        return entry.location_hint.replace("-", " ")
    return ""


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
