"""Configuration loading.

Defaults are built in so the CLI works with no configuration file. An optional
TOML file can override the database location and source settings without code
changes (PRD FR-12). Matching keywords stay in code for M1 and are versioned by
`matching.RULES_VERSION`.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .matching import DEFAULT_RULES, MatchingRules, build_rules

DEFAULT_DB_PATH = Path("data") / "jobscout.db"
DEFAULT_CONFIG_PATH = Path("config.toml")
DEFAULT_ENV_PATH = Path(".env")

TELEGRAM_TOKEN_ENV = "JOBSCOUT_TELEGRAM_TOKEN"
TELEGRAM_CHAT_ID_ENV = "JOBSCOUT_TELEGRAM_CHAT_ID"

_BOT_TOKEN_SHAPE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{20,}$")


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
    matching: MatchingRules = DEFAULT_RULES

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


@dataclass(frozen=True)
class TelegramConfig:
    """Telegram destination, loaded from `.env` or the environment."""

    token: str
    chat_id: str

    def __repr__(self) -> str:  # keep secrets out of tracebacks and debug output
        return "TelegramConfig(chat_id=<redacted>, token=<redacted>)"


def read_env_file(path: Path | str) -> dict[str, str]:
    """Minimal `.env` reader: `KEY=VALUE` lines, optional `export`, no interpolation.

    Deliberately dependency-free. Missing or unreadable files yield an empty
    mapping; values are never logged.
    """
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return values

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.casefold().startswith("export "):
            line = line[len("export ") :].strip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


def load_telegram_config(
    environ: Mapping[str, str] | None = None, *, env_file: str | Path | None = None
) -> TelegramConfig:
    """Read Telegram credentials from the environment, falling back to `.env`.

    Real environment variables win, so the file is a convenience rather than a
    requirement. Values are validated and never echoed.
    """
    env = os.environ if environ is None else environ
    dotenv = read_env_file(DEFAULT_ENV_PATH if env_file is None else env_file)

    def resolve(name: str) -> str:
        for value in (env.get(name), dotenv.get(name)):
            if value and value.strip():
                return value.strip()
        return ""

    token = resolve(TELEGRAM_TOKEN_ENV)
    chat_id = resolve(TELEGRAM_CHAT_ID_ENV)

    missing = [
        name
        for name, value in ((TELEGRAM_TOKEN_ENV, token), (TELEGRAM_CHAT_ID_ENV, chat_id))
        if not value
    ]
    if missing:
        raise ConfigError(
            "missing Telegram configuration: set "
            + " and ".join(missing)
            + " in .env or the environment (see .env.example); or re-run with --dry-run"
        )
    if not _BOT_TOKEN_SHAPE.match(token):
        raise ConfigError(
            f"{TELEGRAM_TOKEN_ENV} does not look like a Telegram bot token "
            "(expected '<bot id>:<secret>'); the value is not echoed here"
        )
    return TelegramConfig(token=token, chat_id=chat_id)


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
    return AppConfig(
        database_path=database_path,
        sources=tuple(sources),
        config_path=config_path,
        matching=parse_matching(data.get("matching")),
    )


_MATCHING_KEYS = {"extra_family_terms", "extra_exclude_terms"}


def parse_matching(section: Any) -> MatchingRules:
    """Build matching rules from an optional `[matching]` table.

    Only additive overrides are accepted: extra phrases for an approved role
    family, and extra title exclusions. Unknown keys are rejected so a typo
    cannot silently weaken the approved rules.
    """
    if section is None:
        return DEFAULT_RULES
    if not isinstance(section, dict):
        raise ConfigError("[matching] must be a table")
    unknown = sorted(set(section) - _MATCHING_KEYS)
    if unknown:
        raise ConfigError(
            f"unknown [matching] key(s): {', '.join(unknown)}"
            f" (allowed: {', '.join(sorted(_MATCHING_KEYS))})"
        )

    families = section.get("extra_family_terms", {})
    if not isinstance(families, dict):
        raise ConfigError("[matching.extra_family_terms] must be a table of family = [terms]")
    excludes = section.get("extra_exclude_terms", [])
    if not isinstance(excludes, list):
        raise ConfigError("[matching.extra_exclude_terms] must be an array of strings")

    try:
        return build_rules(extra_family_terms=families, extra_exclude_terms=excludes)
    except ValueError as exc:
        raise ConfigError(f"invalid [matching] configuration: {exc}") from exc
