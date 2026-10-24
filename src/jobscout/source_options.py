"""Declarative option schema for each adapter.

A source added by configuration is only useful if a typo fails loudly. This module is the
single place that says which `[sources.options]` keys an adapter accepts and which it
cannot work without, so `load_config` can reject a bad source before it is ever scanned.

It lives on its own, with no imports from `config` or the adapters, because both sides
need it and either import direction would be circular.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Keys an adapter cannot work without.
REQUIRED_OPTIONS: dict[str, frozenset[str]] = {
    "lever": frozenset(),
    "greenhouse": frozenset(),
    "workday": frozenset({"host", "tenant", "site"}),
    "sitemap_html": frozenset({"job_url_pattern"}),
}

# Keys an adapter reads. Anything else is a typo or a leftover from an older config.
OPTIONAL_OPTIONS: dict[str, frozenset[str]] = {
    # `site` falls back to the source id, so it is optional but still meaningful.
    "lever": frozenset({"site", "uk_employer"}),
    "greenhouse": frozenset({"board", "uk_employer"}),
    "workday": frozenset({"locale", "page_size", "max_pages", "uk_employer"}),
    "sitemap_html": frozenset(
        {
            "sitemap",
            "sitemap_urls",
            "sitemap_child_pattern",
            "location_from",
            "max_sitemap_documents",
            "max_detail_fetches",
            "request_delay_seconds",
            "uk_employer",
        }
    ),
}


def known_adapters() -> tuple[str, ...]:
    return tuple(sorted(REQUIRED_OPTIONS))


def allowed_options(adapter: str) -> frozenset[str]:
    """Every key the adapter accepts; empty for an unknown adapter."""
    return REQUIRED_OPTIONS.get(adapter, frozenset()) | OPTIONAL_OPTIONS.get(adapter, frozenset())


def unknown_options(adapter: str, options: Mapping[str, Any]) -> tuple[str, ...]:
    if adapter not in REQUIRED_OPTIONS:
        return ()
    return tuple(sorted(set(options) - allowed_options(adapter)))


def missing_options(adapter: str, options: Mapping[str, Any]) -> tuple[str, ...]:
    if adapter not in REQUIRED_OPTIONS:
        return ()
    return tuple(
        sorted(key for key in REQUIRED_OPTIONS[adapter] if not str(options.get(key) or ""))
    )


def describe(adapter: str) -> str:
    """A one-line summary for error messages and `sources list`."""
    required = sorted(REQUIRED_OPTIONS.get(adapter, frozenset()))
    optional = sorted(OPTIONAL_OPTIONS.get(adapter, frozenset()))
    parts = []
    if required:
        parts.append(f"required: {', '.join(required)}")
    if optional:
        parts.append(f"optional: {', '.join(optional)}")
    return "; ".join(parts) or "no options"


__all__ = [
    "OPTIONAL_OPTIONS",
    "REQUIRED_OPTIONS",
    "allowed_options",
    "describe",
    "known_adapters",
    "missing_options",
    "unknown_options",
]
