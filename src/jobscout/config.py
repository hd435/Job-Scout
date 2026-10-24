"""Configuration loading.

Defaults are built in so the CLI works with no configuration file. An optional TOML file
sets the database location, the sources, and the user's job-search preferences
(`[preferences]`: role families, seniority, employment types, locations, title
exclusions). The approved keyword lists stay in code — a typo cannot invent a category —
and the active set is fingerprinted by `matching.RULES_VERSION` plus a digest.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from importlib.resources import files
from pathlib import Path
from typing import Any

from .http import DEFAULT_PACING, PacingPolicy
from .matching import (
    DEFAULT_LOCATION_PREFERENCES,
    DEFAULT_RULES,
    LOCATION_MODES,
    LocationPreferences,
    MatchingRules,
    build_rules,
)
from .source_options import describe, known_adapters, missing_options, unknown_options

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
    warnings: tuple[str, ...] = ()
    pacing: PacingPolicy = DEFAULT_PACING

    def source(self, source_id: str) -> SourceConfig:
        for source in self.sources:
            if source.id == source_id:
                return source
        known = ", ".join(source.id for source in self.sources)
        raise ConfigError(f"unknown source '{source_id}' (configured: {known})")

    def enabled_sources(self) -> tuple[SourceConfig, ...]:
        return tuple(source for source in self.sources if source.enabled)


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
    catalogue_path = _catalogue_path(section, config_path)
    disabled = _disabled_ids(section)
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
        _validate_source_options(source_id, adapter, options, origin=f"sources[{index}]")
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
        # The catalogue is the list; `[[sources]]` replaces it entirely, which is how a
        # config that named one source before v0.6 keeps meaning exactly one source.
        sources = list(
            catalogue_directory(catalogue_path)
            if catalogue_path is not None
            else bundled_catalogue()
        )
    sources = apply_disable(sources, disabled)
    return AppConfig(
        database_path=database_path,
        sources=tuple(sources),
        config_path=config_path,
        matching=parse_rules(data),
        warnings=duplicate_source_warnings(sources),
        pacing=parse_pacing(data.get("pacing")),
    )


def _catalogue_path(section: Mapping[str, Any], config_path: Path) -> Path | None:
    """`[jobscout] catalogue = "sources"`, resolved next to the config file."""
    raw = section.get("catalogue")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError("[jobscout].catalogue must be a directory path")
    candidate = Path(raw.strip())
    return candidate if candidate.is_absolute() else config_path.parent / candidate


def _disabled_ids(section: Mapping[str, Any]) -> tuple[str, ...]:
    """`[jobscout] disable = ["id", ...]`: narrow the catalogue without editing its files."""
    raw = section.get("disable", [])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ConfigError("[jobscout].disable must be an array of source ids")
    return tuple(item.strip() for item in raw if item.strip())


def _source_from_table(data: Mapping[str, Any], *, origin: str) -> SourceConfig:
    """One employer from a parsed catalogue table."""
    source_id = str(data.get("id", "")).strip()
    employer = str(data.get("employer", "")).strip()
    adapter = str(data.get("adapter", "")).strip()
    if not source_id or not employer or not adapter:
        raise ConfigError(f"{origin}: id, employer and adapter are all required")
    cadence = data.get("cadence_minutes", 30)
    if not isinstance(cadence, int) or isinstance(cadence, bool) or cadence <= 0:
        raise ConfigError(f"{origin}: cadence_minutes must be a positive integer")
    options = data.get("options", {})
    if not isinstance(options, dict):
        raise ConfigError(f"{origin}: options must be a table")
    _validate_source_options(source_id, adapter, options, origin=origin)
    return SourceConfig(
        id=source_id,
        employer=employer,
        adapter=adapter,
        enabled=bool(data.get("enabled", True)),
        cadence_minutes=cadence,
        options=dict(options),
    )


def _validate_source_options(
    source_id: str,
    adapter: str,
    options: Mapping[str, Any],
    *,
    origin: str | None = None,
) -> None:
    """Reject options an adapter does not read, and options it cannot work without.

    A source defined by configuration is exactly where a typo is likely, and an ignored
    option is the worst failure mode: the source loads, scans, and returns the wrong thing.
    An unknown adapter is left to `config validate`, because the option set for it is
    unknown by definition and a disabled source should not stop the file loading.
    """
    if adapter not in known_adapters():
        return
    where = f"{origin} (source '{source_id}')" if origin else f"source '{source_id}'"
    unknown = unknown_options(adapter, options)
    if unknown:
        raise ConfigError(
            f"{where}: unknown option(s) for the {adapter} adapter:"
            f" {', '.join(unknown)} (accepted: {describe(adapter)})"
        )
    missing = missing_options(adapter, options)
    if missing:
        raise ConfigError(
            f"{where}: the {adapter} adapter needs"
            f" {', '.join(missing)} (accepted: {describe(adapter)})"
        )


def duplicate_source_warnings(sources: Sequence[SourceConfig]) -> tuple[str, ...]:
    """Warn when two enabled sources watch the same employer or the same feed.

    Neither is fatal - an employer can genuinely publish two boards - but both produce the
    same role twice under different source ids, and deduplication is per source id.
    """
    warnings: list[str] = []
    seen_employer: dict[str, str] = {}
    seen_feed: dict[str, str] = {}
    for source in sources:
        if not source.enabled:
            continue
        employer = " ".join(str(source.employer).casefold().split())
        other = seen_employer.setdefault(employer, source.id)
        if other != source.id:
            warnings.append(
                f"sources '{other}' and '{source.id}' both monitor '{source.employer}';"
                " the same role can then alert twice"
            )
        for feed in _feed_keys(source):
            owner = seen_feed.setdefault(feed, source.id)
            if owner != source.id:
                warnings.append(
                    f"sources '{owner}' and '{source.id}' read the same feed ({feed});"
                    " expect duplicate roles"
                )
    return tuple(warnings)


def _feed_keys(source: SourceConfig) -> tuple[str, ...]:
    """The identifying values of a source's feed, normalised for comparison."""
    options = source.options
    keys: list[str] = []
    if source.adapter in {"lever", "greenhouse"}:
        site = str(options.get("board") or options.get("site") or source.id).casefold()
        keys.append(f"{source.adapter}:{site}")
    elif source.adapter == "workday":
        keys.append(
            "workday:"
            + "/".join(str(options.get(key) or "").casefold() for key in ("host", "tenant", "site"))
        )
    elif source.adapter == "sitemap_html":
        urls = options.get("sitemap_urls")
        if isinstance(urls, list):
            keys.extend(str(url).strip().casefold() for url in urls if str(url).strip())
        single = str(options.get("sitemap") or "").strip()
        if single:
            keys.append(single.casefold())
    return tuple(dict.fromkeys(keys))


_MATCHING_KEYS = {"extra_family_terms", "extra_exclude_terms"}
_PREFERENCE_KEYS = {
    "role_families",
    "seniority_exclusions",
    "employment_types",
    "title_exclusions",
    "locations",
}
_LOCATION_KEYS = {"mode", "cities", "remote_uk", "hybrid_uk"}


_PACING_KEYS = {"default_seconds", "hosts", "max_requests"}


def parse_pacing(section: Any) -> PacingPolicy:
    """`[pacing]`: the per-host floor between requests and a per-process ceiling.

    Both are safety limits rather than tuning knobs: at 100 sources the default floor is what
    stops two sources on one host from being scanned back to back.
    """
    if section is None:
        return DEFAULT_PACING
    if not isinstance(section, dict):
        raise ConfigError("[pacing] must be a table")
    unknown = sorted(set(section) - _PACING_KEYS)
    if unknown:
        raise ConfigError(
            f"unknown [pacing] key(s): {', '.join(unknown)}"
            f" (allowed: {', '.join(sorted(_PACING_KEYS))})"
        )

    seconds = section.get("default_seconds", DEFAULT_PACING.default_seconds)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
        raise ConfigError(f"[pacing].default_seconds = {seconds!r} must be a number of seconds")

    max_requests = section.get("max_requests", DEFAULT_PACING.max_requests)
    if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests <= 0:
        raise ConfigError(f"[pacing].max_requests = {max_requests!r} must be a positive integer")

    hosts_raw = section.get("hosts", {})
    if not isinstance(hosts_raw, dict):
        raise ConfigError("[pacing.hosts] must be a table of host = seconds")
    hosts: dict[str, float] = {}
    for host, value in hosts_raw.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ConfigError(f"[pacing.hosts] {host!r} = {value!r} must be a number of seconds")
        hosts[str(host).strip().casefold()] = float(value)

    return PacingPolicy(default_seconds=float(seconds), hosts=hosts, max_requests=int(max_requests))


def parse_rules(data: Mapping[str, Any]) -> MatchingRules:
    """Build the active ruleset from `[matching]` additions and `[preferences]` choices."""
    additions = _parse_matching_terms(data.get("matching"))
    preferences = parse_preferences(data.get("preferences"))
    if preferences:
        try:
            return build_rules(**additions, **preferences)
        except ValueError as exc:
            raise ConfigError(f"invalid [preferences] configuration: {exc}") from exc
    try:
        return build_rules(**additions)
    except ValueError as exc:
        raise ConfigError(f"invalid [matching] configuration: {exc}") from exc


def _parse_matching_terms(section: Any) -> dict[str, Any]:
    """Optional additive terms inside an approved role family and extra exclusions.

    Only additive overrides are accepted: extra phrases for an approved role
    family, and extra title exclusions. Unknown keys are rejected so a typo
    cannot silently weaken the approved rules.
    """
    if section is None:
        return {}
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
    return {"extra_family_terms": families, "extra_exclude_terms": excludes}


def parse_preferences(section: Any) -> dict[str, Any]:
    """Parse `[preferences]`, rejecting unknown keys and values that cannot take effect.

    Every message names the key and the offending value: a preference that silently
    does nothing is worse than one that refuses to load.
    """
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ConfigError("[preferences] must be a table")
    unknown = sorted(set(section) - _PREFERENCE_KEYS)
    if unknown:
        raise ConfigError(
            f"unknown [preferences] key(s): {', '.join(unknown)}"
            f" (allowed: {', '.join(sorted(_PREFERENCE_KEYS))})"
        )

    parsed: dict[str, Any] = {}
    for key in ("role_families", "seniority_exclusions", "employment_types", "title_exclusions"):
        if key not in section:
            continue
        value = section[key]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConfigError(f"[preferences].{key} must be an array of strings")
        parsed[key] = value

    if "locations" in section:
        parsed["locations"] = _parse_location_preferences(section["locations"])
    return parsed


def _parse_location_preferences(section: Any) -> LocationPreferences:
    if not isinstance(section, dict):
        raise ConfigError("[preferences.locations] must be a table")
    unknown = sorted(set(section) - _LOCATION_KEYS)
    if unknown:
        raise ConfigError(
            f"unknown [preferences.locations] key(s): {', '.join(unknown)}"
            f" (allowed: {', '.join(sorted(_LOCATION_KEYS))})"
        )

    mode = section.get("mode", DEFAULT_LOCATION_PREFERENCES.mode)
    if not isinstance(mode, str) or mode not in LOCATION_MODES:
        raise ConfigError(
            f"[preferences.locations].mode = {mode!r} is not one of {', '.join(LOCATION_MODES)}"
        )

    cities = section.get("cities", [])
    if not isinstance(cities, list) or not all(isinstance(city, str) for city in cities):
        raise ConfigError("[preferences.locations].cities must be an array of strings")
    cleaned_cities = tuple(city for city in (city.strip() for city in cities) if city)
    if mode == "pinned" and not cleaned_cities:
        raise ConfigError(
            "[preferences.locations].mode = 'pinned' needs at least one entry in"
            " [preferences.locations].cities; use mode = 'uk' for the whole UK"
        )

    toggles: dict[str, bool] = {}
    for key in ("remote_uk", "hybrid_uk"):
        value = section.get(key, getattr(DEFAULT_LOCATION_PREFERENCES, key))
        if not isinstance(value, bool):
            raise ConfigError(f"[preferences.locations].{key} must be true or false")
        toggles[key] = value

    return LocationPreferences(mode=mode, cities=cleaned_cities, **toggles)


CATALOGUE_DIR_NAME = "catalogue"


def bundled_catalogue() -> tuple[SourceConfig, ...]:
    """The employers that ship with the package: one TOML file each, no Python to edit."""
    root = files("jobscout") / CATALOGUE_DIR_NAME
    return _load_catalogue_entries(root, origin="bundled catalogue")


@dataclass(frozen=True)
class _CatalogueEntry:
    source: SourceConfig
    origin: str


def _load_catalogue_entries(directory: Any, *, origin: str) -> tuple[SourceConfig, ...]:
    """Read one employer per `*.toml` file from a catalogue directory.

    A file is a single table: `id`, `employer`, `adapter`, optional `enabled` and
    `cadence_minutes`, and an `[options]` table. One employer per file keeps a 100-entry
    catalogue reviewable and free of merge conflicts.
    """
    entries: list[SourceConfig] = []
    seen: dict[str, str] = {}
    paths = sorted(
        (entry for entry in directory.iterdir() if str(entry.name).endswith(".toml")),
        key=lambda entry: str(entry.name),
    )
    for path in paths:
        name = str(path.name)
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{origin}: invalid TOML in {name}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{origin}: {name} must be a table of source fields")
        if "sources" in data:
            raise ConfigError(
                f"{origin}: {name} must describe one employer with `id`, not a `[[sources]]` list"
            )
        source_id = str(data.get("id", "")).strip()
        if not source_id:
            raise ConfigError(f"{origin}: {name} has no `id`")
        if source_id in seen:
            raise ConfigError(
                f"{origin}: '{source_id}' appears in both {seen[source_id]} and {name}"
            )
        seen[source_id] = name
        entries.append(_source_from_table(data, origin=f"{origin}/{name}"))
    return tuple(entries)


def catalogue_directory(path: Path) -> tuple[SourceConfig, ...]:
    """Load an operator's own catalogue directory; a missing directory is an error."""
    if not path.exists():
        raise ConfigError(f"catalogue directory not found: {path}")
    if not path.is_dir():
        raise ConfigError(f"catalogue must be a directory: {path}")
    return _load_catalogue_entries(path, origin=str(path))


def apply_disable(sources: Sequence[SourceConfig], disabled: Sequence[str]) -> list[SourceConfig]:
    """Turn catalogue entries off by id, so an operator can narrow without editing files."""
    known = {source.id for source in sources}
    unknown = [source_id for source_id in disabled if source_id not in known]
    if unknown:
        raise ConfigError(
            f"disable names unknown source(s): {', '.join(sorted(unknown))}"
            f" (known: {', '.join(sorted(known))})"
        )
    off = set(disabled)
    return [replace(source, enabled=False) if source.id in off else source for source in sources]


# The shipped catalogue, read once at import: every existing reference to `DEFAULT_SOURCES`
# keeps working, and the files stay the single source of truth.
DEFAULT_SOURCES: tuple[SourceConfig, ...] = bundled_catalogue()
