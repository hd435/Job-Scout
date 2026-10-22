"""HTTP access with bounded retries and a descriptive user agent.

Only JSON is needed for M1 (Palantir via Lever). The transport is injectable so
retry behaviour can be tested without network access.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

USER_AGENT = "Job-Scout/0.1 (personal job monitor; +https://github.com/hd435/Job-Scout)"


class HttpError(Exception):
    """A request that failed, with enough context for a scan status decision."""

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.status = status
        self.retryable = retryable

    def summary(self) -> str:
        parts = [str(self)]
        if self.status is not None:
            parts.append(f"status={self.status}")
        parts.append("retryable" if self.retryable else "permanent")
        return " ".join(parts)


class HttpClient(Protocol):
    def get_json(self, url: str) -> Any: ...


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str]


Transport = Callable[[str, dict[str, str], float], HttpResponse]


def _urllib_transport(url: str, headers: dict[str, str], timeout: float) -> HttpResponse:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return HttpResponse(
                status=response.status,
                body=response.read(),
                headers={key.lower(): value for key, value in response.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(
            status=exc.code,
            body=b"",
            headers={key.lower(): value for key, value in (exc.headers or {}).items()},
        )
    except urllib.error.URLError as exc:
        raise HttpError(f"network error: {exc.reason}", url=url, retryable=True) from exc
    except TimeoutError as exc:
        raise HttpError("request timed out", url=url, retryable=True) from exc


class UrllibHttpClient:
    """GET JSON with bounded retries for 429 and 5xx responses."""

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
        max_retry_after: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        transport: Transport = _urllib_transport,
    ) -> None:
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._backoff = backoff_seconds
        self._max_retry_after = max_retry_after
        self._sleep = sleep
        self._transport = transport

    def get_json(self, url: str) -> Any:
        raw = self._get(url)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(f"invalid JSON payload: {exc}", url=url) from exc

    def _get(self, url: str) -> bytes:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        last_status: int | None = None
        for attempt in range(1, self._max_attempts + 1):
            response = self._transport(url, headers, self._timeout)
            if 200 <= response.status < 300:
                return response.body
            last_status = response.status
            retryable = response.status == 429 or 500 <= response.status < 600
            if not retryable:
                raise HttpError(
                    "request rejected", url=url, status=response.status, retryable=False
                )
            if attempt < self._max_attempts:
                self._sleep(self._delay(response, attempt))
        raise HttpError("request failed after retries", url=url, status=last_status, retryable=True)

    def _delay(self, response: HttpResponse, attempt: int) -> float:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), self._max_retry_after)
            except ValueError:
                pass
        return self._backoff * (2 ** (attempt - 1))


class FixtureHttpClient:
    """Serves a recorded payload from disk. Used for dry runs and tests."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self.requests: list[str] = []

    def get_json(self, url: str) -> Any:
        self.requests.append(url)
        return json.loads(self._path.read_text(encoding="utf-8"))
