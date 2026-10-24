"""Per-host pacing: one clock for every source, and a ceiling on a runaway pass."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jobscout.config import ConfigError, load_config
from jobscout.http import (
    DEFAULT_PACING,
    PacedHttpClient,
    PacingBudgetExceeded,
    PacingPolicy,
)
from jobscout.models import ScanStatus
from jobscout.sources.sitemap_html import SitemapHtmlAdapter


class Recording:
    """A client that records URLs and never touches the network."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def get_json(self, url: str) -> Any:
        self.urls.append(url)
        return {}

    def get_text(self, url: str) -> str:
        self.urls.append(url)
        return (
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://example.test/jobs/1</loc></url></urlset>"
        )

    def post_json(self, url: str, payload: dict[str, Any], *, retry: bool = False) -> Any:
        self.urls.append(url)
        return {}


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.naps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.naps.append(round(seconds, 3))
        self.now += seconds


def paced(policy: PacingPolicy, **kwargs) -> tuple[PacedHttpClient, Recording, FakeTime]:
    inner = Recording()
    time = FakeTime()
    client = PacedHttpClient(inner, policy=policy, clock=time.clock, sleep=time.sleep, **kwargs)
    return client, inner, time


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


# spacing -------------------------------------------------------------------


def test_two_requests_to_one_host_are_spaced() -> None:
    client, inner, time = paced(PacingPolicy(default_seconds=2.0))

    client.get_json("https://boards.test/a")
    client.get_json("https://boards.test/b")

    assert time.naps == [2.0]
    assert inner.urls == ["https://boards.test/a", "https://boards.test/b"]


def test_different_hosts_do_not_wait_for_each_other() -> None:
    client, _inner, time = paced(PacingPolicy(default_seconds=2.0))

    client.get_json("https://one.test/a")
    client.get_json("https://two.test/a")

    assert time.naps == []


def test_the_host_is_case_insensitive() -> None:
    """Two sources may spell the same host differently; the clock must not care."""
    client, _inner, time = paced(PacingPolicy(default_seconds=1.0))

    client.get_json("https://Boards.Test/a")
    client.get_json("https://boards.test/b")

    assert time.naps == [1.0]


def test_a_per_host_override_is_honoured() -> None:
    client, _inner, time = paced(PacingPolicy(default_seconds=1.0, hosts={"slow.test": 4.0}))

    client.get_text("https://slow.test/a")
    client.get_text("https://slow.test/b")
    client.get_text("https://fast.test/a")
    client.get_text("https://fast.test/b")

    assert time.naps == [4.0, 1.0]


def test_time_already_spent_counts_towards_the_interval() -> None:
    """A slow response must not be followed by a full extra wait."""
    client, _inner, time = paced(PacingPolicy(default_seconds=2.0))

    client.get_json("https://boards.test/a")
    time.now += 1.5  # the request itself took time
    client.get_json("https://boards.test/b")

    assert time.naps == [0.5]


def test_post_and_get_share_the_host_clock() -> None:
    client, _inner, time = paced(PacingPolicy(default_seconds=1.0))

    client.get_json("https://wd.test/listing")
    client.post_json("https://wd.test/listing", {"offset": 0})

    assert time.naps == [1.0]


def test_zero_interval_disables_waiting() -> None:
    client, _inner, time = paced(PacingPolicy(default_seconds=0))

    client.get_json("https://boards.test/a")
    client.get_json("https://boards.test/b")

    assert time.naps == []


# the budget ----------------------------------------------------------------


def test_the_process_budget_stops_a_runaway_pass() -> None:
    client, inner, _time = paced(PacingPolicy(default_seconds=0, max_requests=2))

    client.get_json("https://boards.test/a")
    client.get_json("https://boards.test/b")

    with pytest.raises(PacingBudgetExceeded, match="request budget of 2 exhausted"):
        client.get_json("https://boards.test/c")
    assert inner.urls == ["https://boards.test/a", "https://boards.test/b"]


def test_the_budget_error_is_an_http_error_so_adapters_report_a_failed_scan() -> None:
    from jobscout.http import HttpError

    assert issubclass(PacingBudgetExceeded, HttpError)
    assert PacingBudgetExceeded("stop").retryable is False


def test_a_sitemap_scan_fails_visibly_when_the_budget_runs_out(tmp_path: Path) -> None:
    from jobscout.config import SourceConfig

    source = SourceConfig(
        id="example",
        employer="Example Ltd",
        adapter="sitemap_html",
        options={
            "sitemap_urls": ["https://example.test/sitemap.xml"],
            "job_url_pattern": r"^https://example\.test/jobs/(?P<id>\d+)$",
        },
    )
    client, _inner, _time = paced(PacingPolicy(default_seconds=0, max_requests=1))
    adapter = SitemapHtmlAdapter(request_delay=0.0, sleep=lambda _s: None)

    result = adapter.fetch_scan(source, client)

    assert result.status is ScanStatus.FAILED
    assert "request budget of 1 exhausted" in (result.error_summary or "")


# configuration -------------------------------------------------------------


def test_the_defaults_are_on_and_conservative() -> None:
    assert DEFAULT_PACING.default_seconds > 0
    assert DEFAULT_PACING.max_requests >= 1000
    assert DEFAULT_PACING.interval_for("anything.test") == DEFAULT_PACING.default_seconds


def test_pacing_loads_from_toml(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        """
[pacing]
default_seconds = 0.75
max_requests = 900

[pacing.hosts]
"careers.example.test" = 2.5
"CAREERS.other.test" = 3
""",
    )

    pacing = load_config(config).pacing

    assert pacing.default_seconds == 0.75
    assert pacing.max_requests == 900
    assert pacing.interval_for("careers.example.test") == 2.5
    assert pacing.interval_for("careers.other.test") == 3.0


def test_an_unknown_pacing_key_is_rejected(tmp_path: Path) -> None:
    config = write_config(tmp_path, "[pacing]\ndefault_second = 1\n")

    with pytest.raises(ConfigError, match="unknown \\[pacing\\] key\\(s\\): default_second"):
        load_config(config)


def test_a_negative_interval_is_rejected(tmp_path: Path) -> None:
    config = write_config(tmp_path, "[pacing]\ndefault_seconds = -1\n")

    with pytest.raises(ConfigError, match="must be a number of seconds"):
        load_config(config)


def test_a_zero_budget_is_rejected(tmp_path: Path) -> None:
    config = write_config(tmp_path, "[pacing]\nmax_requests = 0\n")

    with pytest.raises(ConfigError, match="must be a positive integer"):
        load_config(config)


def test_a_bad_host_override_is_rejected_naming_the_host(tmp_path: Path) -> None:
    config = write_config(tmp_path, '[pacing.hosts]\n"x.test" = "slow"\n')

    with pytest.raises(ConfigError, match="'x.test' = 'slow' must be a number of seconds"):
        load_config(config)
