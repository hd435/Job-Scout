"""Adapter registry. A small explicit map, not dynamic plugin discovery."""

from __future__ import annotations

from .base import AdapterError, SourceAdapter, build_job
from .lever import LeverAdapter
from .workday import WorkdayAdapter

_ADAPTERS: dict[str, SourceAdapter] = {
    adapter.adapter_id: adapter for adapter in (LeverAdapter(), WorkdayAdapter())
}


def get_adapter(adapter_id: str) -> SourceAdapter:
    try:
        return _ADAPTERS[adapter_id]
    except KeyError as exc:
        known = ", ".join(sorted(_ADAPTERS))
        raise AdapterError(f"unknown adapter '{adapter_id}' (known: {known})") from exc


__all__ = ["AdapterError", "SourceAdapter", "build_job", "get_adapter"]
