"""Configuration loading.

Defaults are built in so the CLI works with no configuration file. An optional
TOML file can override the database location and source settings without code
changes (PRD FR-12). Matching keywords stay in code for M1 and are versioned by
`matching.RULES_VERSION`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path("data") / "jobscout.db"
DEFAULT_CONFIG_PATH = Path("config.toml")


class ConfigError(Exception):
    """Invalid or unusable configuration."""


@dataclass(frozen=True)
class SourceConfig:
    id: str
    employer: str
    adapter: str
    enabled: bool = True
    cadence_minutes: int = 30
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AppConfig:
    database_path: Path
    sources: tuple[SourceConfig, ...]
    config_path: Path | None = None

    def source(self, source_id: str) -> SourceConfig:
        for source in self.sources:
            if source.id == source_id:
                return source
        known = ", ".join(source.id for source in self.sources)
        raise ConfigError(f"unknown source '{source_id}' (configured: {known})")

    def enabled_sources(self) -> tuple[SourceConfig, ...]:
        return tuple(source for source in self.sources if source.enabled)


DEFAULT_SOURCES: tuple[SourceConfig, ...] = (
    SourceConfig(
        id="palantir",
        employer="Palantir Technologies",
        adapter="lever",
        cadence_minutes=30,
        options={"site": "palantir"},
    ),
)


def default_config() -> AppConfig:
    return AppConfig(database_path=DEFAULT_DB_PATH, sources=DEFAULT_SOURCES)


def load_config(path: Path | str | None = None) -> AppConfig:
    """Load configuration, falling back to built-in defaults."""
    if path is None:
        candidate = DEFAULT_CONFIG_PATH
        if not candidate.exists():
            return default_config()
        config_path: Path | None = candidate
    else:
        config_path = Path(path)
        if not config_path.exists():
            raise ConfigError(f"configuration file not found: {config_path}")

    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    section = data.get("jobscout", {})
    database_path = Path(section.get("database", DEFAULT_DB_PATH))
    raw_sources = data.get("sources") or []

    sources: list[SourceConfig] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_sources):
        if not isinstance(raw, dict):
            raise ConfigError(f"sources[{index}] must be a table")
        source_id = str(raw.get("id", "")).strip()
        employer = str(raw.get("employer", "")).strip()
        adapter = str(raw.get("adapter", "")).strip()
        if not source_id or not employer or not adapter:
            raise ConfigError(f"sources[{index}] requires id, employer, and adapter")
        if source_id in seen:
            raise ConfigError(f"duplicate source id '{source_id}'")
        seen.add(source_id)
        cadence = raw.get("cadence_minutes", 30)
        if not isinstance(cadence, int) or cadence <= 0:
            raise ConfigError(f"sources[{index}].cadence_minutes must be a positive integer")
        options = raw.get("options", {})
        if not isinstance(options, dict):
            raise ConfigError(f"sources[{index}].options must be a table")
        sources.append(
            SourceConfig(
                id=source_id,
                employer=employer,
                adapter=adapter,
                enabled=bool(raw.get("enabled", True)),
                cadence_minutes=cadence,
                options=dict(options),
            )
        )

    if not sources:
        sources = list(DEFAULT_SOURCES)
    return AppConfig(database_path=database_path, sources=tuple(sources), config_path=config_path)
