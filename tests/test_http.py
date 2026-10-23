from __future__ import annotations

import json
import socket

import pytest

from jobscout.http import (
    HttpError,
    HttpRequest,
    HttpResponse,
    UrllibHttpClient,
    connection_never_opened,
    default_transport,
)


def ok(body: bytes = b'{"ok": true}') -> HttpResponse:
    return HttpResponse(200, body, {})


def test_get_retries_transient_status_then_succeeds() -> None:
    seen = []

    def transport(request, timeout):
        seen.append(request)
        return HttpResponse(429, b"", {"retry-after": "0"}) if len(seen) < 3 else ok()

    client = UrllibHttpClient(transport=transport, sleep=lambda _s: None)

    assert client.get_json("https://example.test/jobs") == {"ok": True}
    assert len(seen) == 3
    assert all(request.method == "GET" for request in seen)


def test_get_gives_up_after_max_attempts() -> None:
    seen = []

    def transport(request, timeout):
        seen.append(request)
        return HttpResponse(503, b"", {})

    client = UrllibHttpClient(transport=transport, sleep=lambda _s: None, max_attempts=3)

    with pytest.raises(HttpError) as exc:
        client.get_json("https://example.test/jobs")
    assert exc.value.retryable is True
    assert exc.value.status == 503
    assert exc.value.uncertain is False
    assert len(seen) == 3


def test_get_does_not_retry_permanent_status() -> None:
    seen = []

    def transport(request, timeout):
        seen.append(request)
        return HttpResponse(404, b"", {})

    client = UrllibHttpClient(transport=transport, sleep=lambda _s: None)

    with pytest.raises(HttpError) as exc:
        client.get_json("https://example.test/jobs")
    assert exc.value.retryable is False
    assert exc.value.status == 404
    assert len(seen) == 1


def test_post_never_retries_internally() -> None:
    seen = []

    def transport(request, timeout):
        seen.append(request)
        return HttpResponse(502, b"", {})

    client = UrllibHttpClient(transport=transport, sleep=lambda _s: None)

    with pytest.raises(HttpError) as exc:
        client.post_json("https://example.test/send", {"chat_id": "1", "text": "hi"})

    assert exc.value.retryable is True
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert json.loads(seen[0].body) == {"chat_id": "1", "text": "hi"}
    assert seen[0].headers["Content-Type"] == "application/json"


def test_post_returns_success_payload() -> None:
    def transport(request, timeout):
        return ok(b'{"ok": true, "result": {"message_id": 7}}')

    client = UrllibHttpClient(transport=transport)

    assert client.post_json("https://example.test/send", {}) == {
        "ok": True,
        "result": {"message_id": 7},
    }


def test_invalid_json_is_reported_without_echoing_the_url() -> None:
    def transport(request, timeout):
        return HttpResponse(200, b"not json", {})

    client = UrllibHttpClient(transport=transport)

    with pytest.raises(HttpError) as exc:
        client.get_json("https://example.test/jobs?token=secret")
    assert "invalid JSON" in str(exc.value)
    assert "token=secret" not in str(exc.value)


def test_default_transport_marks_timeouts_as_uncertain(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", boom)

    with pytest.raises(HttpError) as exc:
        default_transport(HttpRequest("POST", "https://example.test/send", {}), 1.0)
    assert exc.value.retryable is True
    assert exc.value.uncertain is True


def test_default_transport_marks_refused_connections_as_certain(monkeypatch) -> None:
    import urllib.error

    def boom(*_args, **_kwargs):
        raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

    monkeypatch.setattr("urllib.request.urlopen", boom)

    with pytest.raises(HttpError) as exc:
        default_transport(HttpRequest("POST", "https://example.test/send", {}), 1.0)
    assert exc.value.retryable is True
    assert exc.value.uncertain is False


def test_connection_never_opened_classification() -> None:
    assert connection_never_opened(socket.gaierror("dns failure")) is True
    assert connection_never_opened(ConnectionRefusedError()) is True
    assert connection_never_opened(TimeoutError()) is False
    assert connection_never_opened(ConnectionResetError()) is False
