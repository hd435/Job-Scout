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
from jobscout.matching import DEFAULT_RULES, RULES_VERSION

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
    assert [source.id for source in config.sources] == [
        "palantir",
        "barclays",
        "lloyds_graduate",
        "monzo",
        "wise",
        "arm",
    ]
    assert config.source("palantir").cadence_minutes == 30


def test_default_workday_sources_are_configured() -> None:
    config = default_config()

    barclays = config.source("barclays")
    lloyds = config.source("lloyds_graduate")

    assert barclays.adapter == "workday"
    assert barclays.employer == "Barclays"
    assert barclays.options["tenant"] == "barclays"
    assert barclays.options["site"] == "External_Career_Site_Barclays"
    assert lloyds.adapter == "workday"
    assert lloyds.employer == "Lloyds Banking Group"
    assert lloyds.options["tenant"] == "lbg"
    assert lloyds.options["site"] == "Graduate_careers"


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
    with pytest.raises(ConfigError, match="unknown source 'acme'"):
        default_config().source("acme")


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


MATCHING_CONFIG = """
[matching]
extra_exclude_terms = ["contract"]

[matching.extra_family_terms]
data = ["Bioinformatics Analyst"]
"""


def write_matching_config(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_matching_section_is_optional(tmp_path) -> None:
    path = write_matching_config(tmp_path, CONFIG)

    assert load_config(path).matching is DEFAULT_RULES


def test_matching_overrides_are_loaded(tmp_path) -> None:
    config = load_config(write_matching_config(tmp_path, MATCHING_CONFIG))

    assert config.matching.extra_family_terms == {"data": ("bioinformatics analyst",)}
    assert config.matching.extra_exclude_terms == ("contract",)
    assert config.matching.version.startswith(f"{RULES_VERSION}+")
    assert config.matching.has_overrides is True


def test_unknown_matching_key_is_rejected(tmp_path) -> None:
    path = write_matching_config(tmp_path, '[matching]\nremove_exclusions = ["sales"]\n')

    with pytest.raises(ConfigError, match="unknown \\[matching\\] key"):
        load_config(path)


def test_unknown_family_in_config_is_rejected(tmp_path) -> None:
    path = write_matching_config(
        tmp_path, '[matching.extra_family_terms]\nastrology = ["horoscope"]\n'
    )

    with pytest.raises(ConfigError) as exc:
        load_config(path)

    assert "unknown role family 'astrology'" in str(exc.value)


def test_matching_exclusions_must_be_an_array(tmp_path) -> None:
    path = write_matching_config(tmp_path, '[matching]\nextra_exclude_terms = "contract"\n')

    with pytest.raises(ConfigError, match="must be an array of strings"):
        load_config(path)


def test_matching_family_terms_must_be_a_table(tmp_path) -> None:
    path = write_matching_config(tmp_path, '[matching]\nextra_family_terms = ["x"]\n')

    with pytest.raises(ConfigError, match="must be a table"):
        load_config(path)


def test_matching_term_types_are_validated(tmp_path) -> None:
    path = write_matching_config(tmp_path, "[matching.extra_family_terms]\ndata = [1]\n")

    with pytest.raises(ConfigError, match="invalid \\[matching\\] configuration"):
        load_config(path)


def test_uk_only_employer_boards_are_marked_and_the_global_one_is_not() -> None:
    """D-065: the flag relaxes town-name collisions only for UK-only boards."""
    sources = {source.id: source for source in default_config().sources}

    assert sources["palantir"].options.get("uk_employer") is None, "Lever board is global"
    for source_id in ("barclays", "lloyds_graduate", "monzo", "wise", "arm"):
        assert sources[source_id].options.get("uk_employer") is True, source_id
