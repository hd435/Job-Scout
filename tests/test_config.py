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
    read_env_file,
)

BOT_TOKEN = "123456789:AAFakeTokenValueForRedactionTests"
OTHER_TOKEN = "111111:AAOtherFakeTokenValueForTests"

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
    assert "555000111" not in repr(config)
    assert "redacted" in repr(config)


def test_dotenv_file_supplies_credentials(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# local credentials\n"
        f"export {TELEGRAM_TOKEN_ENV}={BOT_TOKEN}\n"
        f"{TELEGRAM_CHAT_ID_ENV}='555000111'\n",
        encoding="utf-8",
    )

    config = load_telegram_config({}, env_file=env_file)

    assert config.token == BOT_TOKEN
    assert config.chat_id == "555000111"


def test_environment_variables_override_dotenv(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"{TELEGRAM_TOKEN_ENV}={OTHER_TOKEN}\n{TELEGRAM_CHAT_ID_ENV}=111\n",
        encoding="utf-8",
    )

    config = load_telegram_config(
        {TELEGRAM_TOKEN_ENV: BOT_TOKEN, TELEGRAM_CHAT_ID_ENV: "555000111"}, env_file=env_file
    )

    assert config.token == BOT_TOKEN
    assert config.chat_id == "555000111"


def test_blank_environment_variable_falls_back_to_dotenv(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"{TELEGRAM_TOKEN_ENV}={BOT_TOKEN}\n{TELEGRAM_CHAT_ID_ENV}=555000111\n",
        encoding="utf-8",
    )

    config = load_telegram_config({TELEGRAM_TOKEN_ENV: "   "}, env_file=env_file)

    assert config.token == BOT_TOKEN


def test_missing_dotenv_file_is_not_an_error_but_still_reports_missing_values(tmp_path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_telegram_config({}, env_file=tmp_path / "absent.env")

    assert ".env" in str(exc.value)
    assert "--dry-run" in str(exc.value)


def test_dotenv_token_shape_is_still_validated(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"{TELEGRAM_TOKEN_ENV}=not-a-token\n{TELEGRAM_CHAT_ID_ENV}=555000111\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc:
        load_telegram_config({}, env_file=env_file)

    assert "not-a-token" not in str(exc.value)
    assert "does not look like" in str(exc.value)


def test_env_file_parsing_ignores_noise_and_inline_comments(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n# comment\nnot-a-pair\nKEY = value # trailing comment\n"
        "QUOTED=\"a b # kept\"\nSINGLE='x'\nexport EXPORTED=yes\n",
        encoding="utf-8",
    )

    values = read_env_file(env_file)

    assert values["KEY"] == "value"
    assert values["QUOTED"] == "a b # kept"
    assert values["SINGLE"] == "x"
    assert values["EXPORTED"] == "yes"
    assert "not-a-pair" not in values


def test_env_file_read_errors_yield_no_values(tmp_path) -> None:
    assert read_env_file(tmp_path / "missing.env") == {}
    assert read_env_file(tmp_path) == {}
