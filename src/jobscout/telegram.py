"""Telegram Bot API transport (M2.2).

One `send` is exactly one HTTP POST and there is no internal retry: repeating a
POST could deliver the same message twice, so retry policy stays in the delivery
state machine (`notifier.Notifier`).

Outcome mapping:

| Result | Outcome | Reasoning |
|---|---|---|
| 200 with `ok: true` | `sent` | Message accepted; message id recorded |
| 200 with `ok: false` | `permanent` | Telegram refused the message |
| 429, 5xx | `retryable` | Message not accepted; safe to try again later |
| other 4xx | `permanent` | Request or chat is invalid; retrying cannot help |
| timeout | `ambiguous` | May have been delivered, so it is never auto-retried (D-024) |
| DNS failure / connection refused | `retryable` | Request provably never reached Telegram |

The bot token is part of the request URL, so no error text, log line, or stored
`last_error` may include the URL; every message is scrubbed of the token and the
chat id before it leaves this module.
"""

from __future__ import annotations

from typing import Any

from .config import TelegramConfig
from .http import HttpError, JsonPoster
from .models import DeliveryOutcome, DeliveryResult
from .notifier import redact

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramTransport:
    def __init__(
        self,
        config: TelegramConfig,
        *,
        poster: JsonPoster,
        api_base: str = TELEGRAM_API_BASE,
    ) -> None:
        self._config = config
        self._poster = poster
        self._api_base = api_base.rstrip("/")

    def send(self, chat_id: str, text: str) -> DeliveryResult:
        url = f"{self._api_base}/bot{self._config.token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}
        try:
            response = self._poster.post_json(url, payload)
        except HttpError as exc:
            return self._from_error(exc)
        except Exception as exc:  # noqa: BLE001 - a transport bug must not crash a scan
            detail = self._scrub(f"unexpected transport error: {type(exc).__name__}")
            return DeliveryResult(
                DeliveryOutcome.AMBIGUOUS,
                error=f"{detail} (delivery state unknown)",
            )
        return self._from_response(response)

    def _from_response(self, response: Any) -> DeliveryResult:
        if isinstance(response, dict) and response.get("ok") is True:
            result = response.get("result")
            message_id = result.get("message_id") if isinstance(result, dict) else None
            return DeliveryResult(
                DeliveryOutcome.SENT,
                message_id=str(message_id) if message_id is not None else None,
            )
        description = ""
        if isinstance(response, dict):
            description = str(response.get("description") or "")
        detail = self._scrub(f"telegram rejected the message: {description or 'unknown reason'}")
        return DeliveryResult(DeliveryOutcome.PERMANENT, error=detail)

    def _from_error(self, exc: HttpError) -> DeliveryResult:
        detail = self._scrub(str(exc))
        if exc.uncertain:
            return DeliveryResult(
                DeliveryOutcome.AMBIGUOUS, error=f"{detail} (delivery state unknown)"
            )
        if exc.retryable or exc.status is None:
            return DeliveryResult(DeliveryOutcome.RETRYABLE, error=detail)
        return DeliveryResult(DeliveryOutcome.PERMANENT, error=detail)

    def _scrub(self, text: str) -> str:
        return redact(text, secret_values=(self._config.token, self._config.chat_id)) or ""
