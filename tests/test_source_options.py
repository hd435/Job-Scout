"""The per-adapter option schema and the duplicate-source guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobscout.config import ConfigError, load_config
from jobscout.source_options import (
    allowed_options,
    describe,
    known_adapters,
    missing_options,
    unknown_options,
)


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_every_registered_adapter_declares_its_options() -> None:
    assert set(known_adapters()) == {
        "ashby",
        "greenhouse",
        "lever",
        "sitemap_html",
        "workable",
        "workday",
    }
    for adapter in known_adapters():
        assert describe(adapter)


def test_unknown_and_missing_options_are_reported() -> None:
    assert unknown_options("greenhouse", {"board": "x", "boards": "y"}) == ("boards",)
    assert unknown_options("greenhouse", {"board": "x"}) == ()
    assert missing_options("workday", {"host": "h", "tenant": "t"}) == ("site",)
    assert missing_options("workday", {"host": "h", "tenant": "t", "site": "s"}) == ()


def test_an_unknown_adapter_declares_nothing() -> None:
    # The option set for a misspelled adapter is unknown, so it is not this module's call.
    assert allowed_options("mystery") == frozenset()
    assert unknown_options("mystery", {"anything": 1}) == ()
    assert missing_options("mystery", {}) == ()


@pytest.mark.parametrize(
    ("adapter", "options"),
    [
        ("lever", 'site = "board"'),
        ("lever", ""),
        ("greenhouse", 'board = "b"\nuk_employer = true'),
        ("workday", 'host = "h"\ntenant = "t"\nsite = "s"\nlocale = "en-GB"\npage_size = 20'),
        (
            "sitemap_html",
            'sitemap_urls = ["https://x.test/s.xml"]\njob_url_pattern = "(?P<id>\\\\d+)"',
        ),
    ],
)
def test_documented_options_load(tmp_path: Path, adapter: str, options: str) -> None:
    config = write_config(
        tmp_path,
        "\n".join(
            [
                "[[sources]]",
                'id = "one"',
                'employer = "One Ltd"',
                f'adapter = "{adapter}"',
                "[sources.options]",
                options,
                "",
            ]
        ),
    )

    app = load_config(config)

    assert app.sources[0].adapter == adapter


@pytest.mark.parametrize(
    ("adapter", "options", "message"),
    [
        ("lever", 'sight = "board"', "unknown option\\(s\\) for the lever adapter: sight"),
        ("greenhouse", 'boardd = "b"', "unknown option\\(s\\) for the greenhouse adapter: boardd"),
        ("workday", 'host = "h"', "the workday adapter needs site, tenant"),
        (
            "sitemap_html",
            'sitemap_urls = ["https://x.test/s.xml"]',
            "the sitemap_html adapter needs job_url_pattern",
        ),
    ],
)
def test_bad_options_are_rejected_naming_the_source(
    tmp_path: Path, adapter: str, options: str, message: str
) -> None:
    config = write_config(
        tmp_path,
        "\n".join(
            [
                "[[sources]]",
                'id = "broken"',
                'employer = "Broken Ltd"',
                f'adapter = "{adapter}"',
                "[sources.options]",
                options,
                "",
            ]
        ),
    )

    with pytest.raises(ConfigError, match=message):
        load_config(config)


def test_the_shipped_defaults_pass_their_own_schema() -> None:
    from jobscout.config import default_config

    app = default_config()

    for source in app.sources:
        assert unknown_options(source.adapter, source.options) == ()
        assert missing_options(source.adapter, source.options) == ()
    assert app.warnings == ()


def test_two_sources_for_one_employer_warn_at_load(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        """
[[sources]]
id = "monzo_one"
employer = "Monzo"
adapter = "greenhouse"
[sources.options]
board = "monzo"

[[sources]]
id = "monzo_two"
employer = "monzo"
adapter = "greenhouse"
[sources.options]
board = "monzo-careers"
""",
    )

    app = load_config(config)

    # Different boards make two feeds, so only the shared employer is suspicious.
    assert len(app.warnings) == 1
    assert "both monitor" in app.warnings[0]
    assert "monzo_one" in app.warnings[0] and "monzo_two" in app.warnings[0]
    assert not any("same feed" in warning for warning in app.warnings)


def test_two_sources_for_one_feed_warn_at_load(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        """
[[sources]]
id = "arm_one"
employer = "Arm Holdings"
adapter = "sitemap_html"
[sources.options]
sitemap_urls = ["https://careers.arm.com/sitemap.xml"]
job_url_pattern = "(?P<id>\\\\d+)"

[[sources]]
id = "arm_two"
employer = "Arm Ltd"
adapter = "sitemap_html"
[sources.options]
sitemap_urls = ["https://CAREERS.arm.com/sitemap.xml"]
job_url_pattern = "(?P<id>\\\\d+)"
""",
    )

    app = load_config(config)

    assert any("read the same feed" in warning for warning in app.warnings)


def test_a_disabled_duplicate_does_not_warn(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        """
[[sources]]
id = "monzo"
employer = "Monzo"
adapter = "greenhouse"
[sources.options]
board = "monzo"

[[sources]]
id = "monzo_old"
employer = "Monzo"
adapter = "greenhouse"
enabled = false
[sources.options]
board = "monzo"
""",
    )

    assert load_config(config).warnings == ()
