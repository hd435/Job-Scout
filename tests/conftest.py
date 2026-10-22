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
from jobscout.models import ScanOutcome  # noqa: E402
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
        config: SourceConfig | None = None,
        storage: Storage | None = None,
    ) -> ScanOutcome:
        return run_scan(
            storage or store,
            get_adapter("lever"),
            config or palantir,
            FakeHttp(payload, error=error),
            accept_empty=accept_empty,
            clock=clock,
        )

    return _scan
