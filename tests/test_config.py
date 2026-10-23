from __future__ import annotations

from pathlib import Path

import pytest

from jobscout.config import (
    TELEGRAM_CHAT_ID_ENV,
    TELEGRAM_TOKEN_ENV,
    ConfigError,
    default_config,
    load_config,
    load_telegram_config,
)

BOT_TOKEN = "123456789:AAFakeTokenValueForRedactionTests"

CONFIG = """
[jobscout]
database = "state/custom.db"

[[sources]]
id = "palantir"
employer = "Palantir Technologies"
adapter = "lever"
cadence_minutes = 45

[sources.options]
site = "palantir"
"""


def test_defaults_are_usable_without_a_config_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.database_path == default_config().database_path
    assert [source.id for source in config.sources] == ["palantir"]
    assert config.source("palantir").cadence_minutes == 30


def test_config_file_overrides_database_and_sources(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG, encoding="utf-8")

    config = load_config(path)

    assert config.database_path == Path("state/custom.db")
    source = config.source("palantir")
    assert source.cadence_minutes == 45
    assert source.options == {"site": "palantir"}


def test_utf8_bom_is_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("\ufeff" + CONFIG, encoding="utf-8")

    assert load_config(path).source("palantir").employer == "Palantir Technologies"


def test_missing_config_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "absent.toml")


def test_invalid_toml_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[jobscout\ndatabase = 1", encoding="utf-8")

    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(path)


def test_duplicate_source_ids_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[[sources]]
id = "palantir"
employer = "A"
adapter = "lever"

[[sources]]
id = "palantir"
employer = "B"
adapter = "lever"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="duplicate source id"):
        load_config(path)


def test_incomplete_source_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[[sources]]\nid = "palantir"\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="requires id, employer, and adapter"):
        load_config(path)


def test_unknown_source_is_reported() -> None:
    with pytest.raises(ConfigError, match="unknown source 'monzo'"):
        default_config().source("monzo")


def test_telegram_configuration_comes_from_the_environment() -> None:
    config = load_telegram_config(
        {TELEGRAM_TOKEN_ENV: BOT_TOKEN, TELEGRAM_CHAT_ID_ENV: "555000111"}
    )

    assert config.token == BOT_TOKEN
    assert config.chat_id == "555000111"


def test_missing_telegram_configuration_names_the_variables() -> None:
    with pytest.raises(ConfigError) as exc:
        load_telegram_config({TELEGRAM_CHAT_ID_ENV: "555000111"})

    assert TELEGRAM_TOKEN_ENV in str(exc.value)
    assert "--dry-run" in str(exc.value)


def test_missing_chat_id_is_reported() -> None:
    with pytest.raises(ConfigError) as exc:
        load_telegram_config({TELEGRAM_TOKEN_ENV: BOT_TOKEN})

    assert TELEGRAM_CHAT_ID_ENV in str(exc.value)


def test_malformed_token_is_rejected_without_echoing_it() -> None:
    with pytest.raises(ConfigError) as exc:
        load_telegram_config(
            {TELEGRAM_TOKEN_ENV: "not-a-real-token", TELEGRAM_CHAT_ID_ENV: "555000111"}
        )

    assert "not-a-real-token" not in str(exc.value)
    assert "does not look like" in str(exc.value)


def test_telegram_config_repr_hides_the_token() -> None:
    config = load_telegram_config(
        {TELEGRAM_TOKEN_ENV: BOT_TOKEN, TELEGRAM_CHAT_ID_ENV: "555000111"}
    )

    assert BOT_TOKEN not in repr(config)
    assert "redacted" in repr(config)
