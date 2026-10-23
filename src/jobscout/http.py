"""HTTP access with bounded retries and a descriptive user agent.

GET requests retry on 429/5xx. POST requests never retry internally: repeating a
POST could deliver the same message twice, so retry policy belongs to the
caller — the notifier's delivery state machine.

The transport is injectable so retry behaviour and failure classification can be
tested without network access.
"""

from __future__ import annotations

import errno
import json
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

USER_AGENT = "Job-Scout/0.1 (personal job monitor; +https://github.com/hd435/Job-Scout)"


class HttpError(Exception):
    """A request that failed, with enough context for a delivery/scan decision.

    `retryable` says whether trying again could help. `uncertain` says the
    request may already have reached the server, so a retry could duplicate an
    effect (used for timeouts after a send).
    """

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        status: int | None = None,
        retryable: bool = False,
        uncertain: bool = False,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.status = status
        self.retryable = retryable
        self.uncertain = uncertain

    def summary(self) -> str:
        parts = [str(self)]
        if self.status is not None:
            parts.append(f"status={self.status}")
        parts.append("retryable" if self.retryable else "permanent")
        if self.uncertain:
            parts.append("outcome-unknown")
        return " ".join(parts)


class HttpClient(Protocol):
    def get_json(self, url: str) -> Any: ...


class JsonPoster(Protocol):
    def post_json(self, url: str, payload: dict[str, Any]) -> Any: ...


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str]


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None = None


Transport = Callable[[HttpRequest, float], HttpResponse]


def connection_never_opened(reason: object) -> bool:
    """True when the failure proves the request never reached the server."""
    if isinstance(reason, (socket.gaierror, ConnectionRefusedError)):
        return True
    if isinstance(reason, TimeoutError):
        return False
    return isinstance(reason, OSError) and reason.errno in {
        errno.ECONNREFUSED,
        errno.ENETUNREACH,
        errno.EHOSTUNREACH,
    }


def default_transport(request: HttpRequest, timeout: float) -> HttpResponse:
    prepared = urllib.request.Request(
        request.url,
        data=request.body,
        headers=request.headers,
        method=request.method,
    )
    try:
        with urllib.request.urlopen(prepared, timeout=timeout) as response:  # noqa: S310
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
        if connection_never_opened(exc.reason):
            raise HttpError(
                f"connection failed: {exc.reason}", url=request.url, retryable=True
            ) from exc
        raise HttpError(
            f"network error: {exc.reason}",
            url=request.url,
            retryable=True,
            uncertain=True,
        ) from exc
    except TimeoutError as exc:
        raise HttpError(
            "request timed out", url=request.url, retryable=True, uncertain=True
        ) from exc


class UrllibHttpClient:
    """GET (retried) and POST (single attempt) JSON client."""

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
        max_retry_after: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        transport: Transport = default_transport,
    ) -> None:
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._backoff = backoff_seconds
        self._max_retry_after = max_retry_after
        self._sleep = sleep
        self._transport = transport

    def get_json(self, url: str) -> Any:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        raw = self._request(HttpRequest("GET", url, headers), retry=True)
        return self._decode(raw, url, "GET")

    def post_json(self, url: str, payload: dict[str, Any]) -> Any:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        raw = self._request(HttpRequest("POST", url, headers, body), retry=False)
        return self._decode(raw, url, "POST")

    def _request(self, request: HttpRequest, *, retry: bool) -> bytes:
        attempts = self._max_attempts if retry else 1
        for attempt in range(1, attempts + 1):
            response = self._transport(request, self._timeout)
            if 200 <= response.status < 300:
                return response.body
            retryable = response.status == 429 or 500 <= response.status < 600
            if not retryable:
                raise HttpError("request rejected", url=request.url, status=response.status)
            if attempt < attempts:
                self._sleep(self._delay(response, attempt))
                continue
            raise HttpError(
                "request failed" + (" after retries" if retry else ""),
                url=request.url,
                status=response.status,
                retryable=True,
            )
        raise AssertionError("unreachable")  # pragma: no cover

    def _decode(self, raw: bytes, url: str, method: str) -> Any:
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(f"invalid JSON in {method} response: {exc}", url=url) from exc

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
