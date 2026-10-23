from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobscout.config import SourceConfig  # noqa: E402
from jobscout.http import HttpError  # noqa: E402
from jobscout.matching import DEFAULT_RULES, MatchingRules  # noqa: E402
from jobscout.models import DeliveryOutcome, DeliveryResult, ScanOutcome  # noqa: E402
from jobscout.notifier import Notifier  # noqa: E402
from jobscout.scanner import run_scan  # noqa: E402
from jobscout.sources import get_adapter  # noqa: E402
from jobscout.storage import Storage  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


class FakeHttp:
    """Stand-in for the HTTP client; records calls and never uses the network."""

    def __init__(self, payload: Any = None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.requests: list[str] = []

    def get_json(self, url: str) -> Any:
        self.requests.append(url)
        if self.error is not None:
            raise self.error
        return self.payload


class FakeClock:
    """Deterministic ISO timestamps, 30 seconds apart."""

    def __init__(self) -> None:
        self._current = datetime(2026, 10, 22, 10, 0, 0, tzinfo=UTC)

    def __call__(self) -> str:
        current = self._current
        self._current += timedelta(seconds=30)
        return current.strftime("%Y-%m-%dT%H:%M:%SZ")


class RecordingTransport:
    """Records what would be sent and replays scripted delivery outcomes."""

    def __init__(self, results: list[DeliveryResult] | None = None) -> None:
        self.results = list(results or [])
        self.sent: list[tuple[str, str]] = []

    def send(self, chat_id: str, text: str) -> DeliveryResult:
        index = len(self.sent)
        self.sent.append((chat_id, text))
        if index < len(self.results):
            return self.results[index]
        return DeliveryResult(DeliveryOutcome.SENT, message_id=str(index + 1))

    @property
    def messages(self) -> list[str]:
        return [text for _chat_id, text in self.sent]


class FakeWorkdayHttp:
    """Serves scripted Workday CXS listing pages and detail lookups; no network."""

    def __init__(
        self,
        pages: list[Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.pages = list(pages or [])
        self.details = dict(details or {})
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.gets: list[str] = []
        self.post_error: Exception | None = None

    def post_json(self, url: str, payload: dict[str, Any], *, retry: bool = False) -> Any:
        self.posts.append((url, payload))
        if self.post_error is not None:
            raise self.post_error
        if not self.pages:
            raise AssertionError(f"unexpected listing request: {url}")
        response = self.pages.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get_json(self, url: str) -> Any:
        self.gets.append(url)
        for path, response in self.details.items():
            if url.endswith(path):
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"unexpected detail request: {url}")


class FakeHtmlHttp:
    """Serves scripted sitemaps and job pages as text; records every request."""

    def __init__(self, documents: dict[str, Any] | None = None) -> None:
        self.documents = dict(documents or {})
        self.gets: list[str] = []
        self.error: Exception | None = None

    def get_text(self, url: str) -> str:
        self.gets.append(url)
        if self.error is not None:
            raise self.error
        if url in self.documents:
            document = self.documents[url]
            if isinstance(document, Exception):
                raise document
            return str(document)
        raise AssertionError(f"unexpected text request: {url}")


def lever_record(
    source_job_id: str,
    title: str,
    location: str | None = "London, United Kingdom",
    *,
    workplace_type: str | None = "hybrid",
    description: str = "A role description.",
    salary: dict[str, Any] | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": source_job_id,
        "text": title,
        "categories": {"team": "Engineering", "commitment": "Full-time"},
        "workplaceType": workplace_type,
        "descriptionPlain": description,
        "hostedUrl": url or f"https://jobs.lever.co/palantir/{source_job_id}",
    }
    if location is not None:
        record["categories"]["location"] = location
        record["categories"]["allLocations"] = [location]
    if salary is not None:
        record["salaryRange"] = salary
    return record


@pytest.fixture(autouse=True)
def isolate_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read the developer's real `.env`; it may hold live credentials."""
    monkeypatch.setattr("jobscout.config.DEFAULT_ENV_PATH", tmp_path / "absent.env")


@pytest.fixture
def palantir() -> SourceConfig:
    return SourceConfig(
        id="palantir",
        employer="Palantir Technologies",
        adapter="lever",
        options={"site": "palantir"},
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Storage]:
    storage = Storage.open(tmp_path / "jobscout.db")
    try:
        yield storage
    finally:
        storage.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake_http() -> type[FakeHttp]:
    return FakeHttp


@pytest.fixture
def fake_transport() -> type[RecordingTransport]:
    return RecordingTransport


@pytest.fixture
def fake_workday_http() -> type[FakeWorkdayHttp]:
    return FakeWorkdayHttp


@pytest.fixture
def barclays() -> SourceConfig:
    return SourceConfig(
        id="barclays",
        employer="Barclays",
        adapter="workday",
        options={
            "host": "barclays.wd3.myworkdayjobs.com",
            "tenant": "barclays",
            "site": "External_Career_Site_Barclays",
            "locale": "en-US",
        },
    )


@pytest.fixture
def lloyds() -> SourceConfig:
    return SourceConfig(
        id="lloyds_graduate",
        employer="Lloyds Banking Group",
        adapter="workday",
        options={
            "host": "lbg.wd3.myworkdayjobs.com",
            "tenant": "lbg",
            "site": "Graduate_careers",
            "locale": "en-US",
        },
    )


@pytest.fixture
def workday_payloads() -> dict[str, Any]:
    return {
        name: json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        for name in (
            "workday_barclays_page1",
            "workday_barclays_page2",
            "workday_barclays_detail",
            "workday_lloyds_graduate",
        )
    }


@pytest.fixture
def fake_html_http() -> type[FakeHtmlHttp]:
    return FakeHtmlHttp


@pytest.fixture
def monzo() -> SourceConfig:
    return SourceConfig(
        id="monzo",
        employer="Monzo",
        adapter="greenhouse",
        options={"board": "monzo"},
    )


@pytest.fixture
def wise() -> SourceConfig:
    return SourceConfig(
        id="wise",
        employer="Wise",
        adapter="sitemap_html",
        options={"profile": "wise"},
    )


@pytest.fixture
def arm() -> SourceConfig:
    return SourceConfig(
        id="arm",
        employer="Arm",
        adapter="sitemap_html",
        options={"profile": "arm"},
    )


@pytest.fixture
def greenhouse_payload() -> dict[str, Any]:
    return json.loads((FIXTURES / "greenhouse_monzo.json").read_text(encoding="utf-8"))


@pytest.fixture
def html_payloads() -> dict[str, str]:
    return {
        name: (FIXTURES / name).read_text(encoding="utf-8")
        for name in (
            "wise_sitemap_index.xml",
            "wise_vacancies_sitemap.xml",
            "wise_job_page.html",
            "arm_sitemap.xml",
            "arm_job_page.html",
            "arm_job_page_no_ld.html",
        )
    }


@pytest.fixture
def make_notifier(clock: FakeClock):
    def _make(transport, **kwargs):
        kwargs.setdefault("sleep", lambda _seconds: None)
        kwargs.setdefault("clock", clock)
        return Notifier(transport, **kwargs)

    return _make


@pytest.fixture
def record() -> Callable[..., dict[str, Any]]:
    """Build a Lever-shaped record for transition tests."""
    return lever_record


@pytest.fixture
def http_error() -> type[HttpError]:
    return HttpError


@pytest.fixture
def fixture_payload() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "palantir_jobs.json").read_text(encoding="utf-8"))


@pytest.fixture
def scan(store: Storage, palantir: SourceConfig, clock: FakeClock) -> Callable[..., ScanOutcome]:
    """Run one Palantir scan against an in-memory payload."""

    def _scan(
        payload: Any = None,
        *,
        error: Exception | None = None,
        accept_empty: bool = False,
        notify_chat_id: str | None = None,
        rules: MatchingRules = DEFAULT_RULES,
        config: SourceConfig | None = None,
        storage: Storage | None = None,
    ) -> ScanOutcome:
        return run_scan(
            storage or store,
            get_adapter("lever"),
            config or palantir,
            FakeHttp(payload, error=error),
            accept_empty=accept_empty,
            notify_chat_id=notify_chat_id,
            rules=rules,
            clock=clock,
        )

    return _scan
