"""Telegram Bot API access for the hosted service: inbound updates and messages with buttons.

The outbound-only `TelegramTransport` used for alerts stays as it is; this adds the two calls a
conversational bot needs — `getUpdates` and `sendMessage` with an inline keyboard — plus the
callback acknowledgement Telegram requires before it will stop the button spinner.

Long polling is deliberate (D-091): no webhook means no domain, no certificate, no inbound port,
and nothing publicly addressable to attack. The bot token is part of every URL, so no error text
leaves this module unscrubbed.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..http import HttpError, JsonPoster, UrllibHttpClient
from ..notifier import redact

TELEGRAM_API_BASE = "https://api.telegram.org"


class BotApiError(RuntimeError):
    """A failed Bot API call that the loop should log and retry on the next iteration."""


@dataclass(frozen=True)
class Button:
    label: str
    data: str


Rows = Sequence[Sequence[Button]]


class BotApi:
    """Calls against the Bot API.

    `timeout_seconds` is Telegram's long-poll hold time, and it **must stay below** the HTTP
    client's own timeout (20s by default) or every idle poll fails as a client-side timeout —
    which is exactly the normal state of a quiet bot. 8 seconds leaves ample margin and still
    makes the loop cheap: one request per 8 seconds while idle.
    """

    def __init__(
        self,
        token: str,
        *,
        poster: JsonPoster | None = None,
        api_base: str = TELEGRAM_API_BASE,
        timeout_seconds: int = 8,
    ) -> None:
        self._token = token
        self._poster = poster or UrllibHttpClient()
        self._base = api_base.rstrip("/")
        self._timeout = timeout_seconds

    def _url(self, method: str) -> str:
        return f"{self._base}/bot{self._token}/{method}"

    def _scrub(self, text: str) -> str:
        return redact(text, secret_values=(self._token,)) or ""

    def get_updates(
        self, offset: int | None, *, timeout: int | None = None
    ) -> list[dict[str, Any]]:
        """Long-poll for updates. Returns [] when Telegram has nothing to say."""
        payload: dict[str, Any] = {
            "timeout": self._timeout if timeout is None else timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        try:
            response = self._poster.post_json(self._url("getUpdates"), payload)
        except HttpError as exc:
            raise BotApiError(self._scrub(f"getUpdates failed: {exc}")) from exc
        if not isinstance(response, dict) or not response.get("ok"):
            raise BotApiError(self._scrub("getUpdates was rejected"))
        result = response.get("result")
        return list(result) if isinstance(result, list) else []

    def send_message(
        self, chat_id: str | int, text: str, *, buttons: Rows | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4096]}
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": button.label, "callback_data": button.data} for button in row]
                    for row in buttons
                ]
            }
        try:
            response = self._poster.post_json(self._url("sendMessage"), payload)
        except HttpError as exc:
            raise BotApiError(self._scrub(f"sendMessage failed: {exc}")) from exc
        if not isinstance(response, dict) or not response.get("ok"):
            raise BotApiError(self._scrub("sendMessage was rejected"))
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def edit_message_text(
        self,
        chat_id: str | int,
        message_id: int,
        text: str,
        *,
        buttons: Rows | None = None,
    ) -> dict[str, Any]:
        """Replace the text of a message the bot sent, keeping one message per browsing session.

        Telegram answers `400 Bad Request: message is not modified` when the content is identical,
        which is a no-op rather than a failure: a double-tapped button must not produce an error
        the caller then turns into a duplicate message.
        """
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:4096],
        }
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": button.label, "callback_data": button.data} for button in row]
                    for row in buttons
                ]
            }
        try:
            response = self._poster.post_json(self._url("editMessageText"), payload)
        except HttpError as exc:
            if "message is not modified" in str(exc).lower():
                return {}
            raise BotApiError(self._scrub(f"editMessageText failed: {exc}")) from exc
        if not isinstance(response, dict) or not response.get("ok"):
            raise BotApiError(self._scrub("editMessageText was rejected"))
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def send_text(self, chat_id: str | int, text: str) -> dict[str, Any]:
        """Send a plain message, splitting on Telegram's 4096-character limit."""
        result: dict[str, Any] = {}
        for chunk in _chunks(text, 4000):
            result = self.send_message(chat_id, chunk)
        return result

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        # A missing acknowledgement only leaves a spinner; never fail the interaction.
        with contextlib.suppress(HttpError):
            self._poster.post_json(self._url("answerCallbackQuery"), payload)


def _chunks(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > size:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks or [text[:size]]


class PollingLoop:
    """The smallest thing that can serve a bot: poll, dispatch, occasionally scan."""

    def __init__(
        self,
        api: BotApi,
        *,
        on_update: Callable[[dict[str, Any]], None],
        on_tick: Callable[[], None] | None = None,
        offset_store: Callable[[], int | None] | None = None,
        offset_saver: Callable[[int], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        error_sleep: float = 5.0,
    ) -> None:
        self._api = api
        self._on_update = on_update
        self._on_tick = on_tick
        self._offset = offset_store() if offset_store else None
        self._save_offset = offset_saver
        self._sleep = sleep
        self._error_sleep = error_sleep

    def poll_once(self) -> int:
        """One poll, one dispatch round. Returns how many updates were handled."""
        updates = self._api.get_updates(self._offset)
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self._offset = update_id + 1
                if self._save_offset:
                    self._save_offset(self._offset)
            self._on_update(update)
        return len(updates)

    def run(self, *, max_iterations: int | None = None) -> None:
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            iterations += 1
            try:
                self.poll_once()
            except BotApiError:
                self._sleep(self._error_sleep)
            if self._on_tick is not None:
                self._on_tick()


__all__ = ["BotApi", "BotApiError", "Button", "PollingLoop", "Rows"]
