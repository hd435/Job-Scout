"""The bot application: commands, the setup wizard, and turning decisions into messages.

This is the only place that knows both Telegram and the store. It is deliberately thin — the
conversation is a pure state machine in `conversation.py`, the per-user evaluation is in
`notify.py` — so the parts worth testing can be tested without a network.

Privacy rules enforced here (D-093):

- only private chats are served; a group is told why and creates no user record;
- no name, username or email is stored: identity is the numeric Telegram user id;
- `/delete` removes preferences, subscriptions, alerts and the wizard session immediately.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..matching import preferences_fingerprint
from ..notifier import Notifier, format_message, redact
from ..storage import Storage
from . import conversation
from .bot import BotApi, BotApiError, Button, Rows
from .notify import digest_matches, notify_pass, user_rules
from .packs import available_packs
from .store import ServiceStore

HELP = (
    "Job-Scout finds new UK early-career roles on employers' own job boards and messages you "
    "when one matches.\n\n"
    "/start — set up or restart your search\n"
    "/settings — change roles, location or employers\n"
    "/digest — roles that already match right now\n"
    "/status — is monitoring working?\n"
    "/pause — stop alerts\n"
    "/resume — start alerts again\n"
    "/delete — remove my data\n"
    "/help — this message"
)

DIGEST_LIMIT = 10


@dataclass(frozen=True)
class Incoming:
    """One user-visible interaction, reduced to what the app needs."""

    telegram_user_id: int
    chat_id: int
    chat_type: str
    text: str | None
    callback_id: str | None
    callback_data: str | None

    @property
    def is_private(self) -> bool:
        return self.chat_type == "private"


def parse_update(update: dict[str, Any]) -> Incoming | None:
    """Reduce a Telegram update to `Incoming`, or None for anything we do not serve."""
    if "callback_query" in update:
        query = update["callback_query"] or {}
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        sender = query.get("from") or {}
        user_id = sender.get("id")
        chat_id = chat.get("id")
        if not isinstance(user_id, int) or not isinstance(chat_id, int):
            return None
        return Incoming(
            telegram_user_id=user_id,
            chat_id=chat_id,
            chat_type=str(chat.get("type", "")),
            text=None,
            callback_id=str(query.get("id", "")),
            callback_data=str(query.get("data", "")),
        )
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    user_id = sender.get("id")
    chat_id = chat.get("id")
    if not isinstance(user_id, int) or not isinstance(chat_id, int):
        return None
    return Incoming(
        telegram_user_id=user_id,
        chat_id=chat_id,
        chat_type=str(chat.get("type", "")),
        text=str(message.get("text", "")).strip(),
        callback_id=None,
        callback_data=None,
    )


class BotApp:
    """Handle one interaction at a time against one store."""

    def __init__(
        self,
        api: BotApi,
        storage: Storage,
        store: ServiceStore,
        *,
        sources: Sequence[Any],
        notifier: Notifier | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._api = api
        self._storage = storage
        self._store = store
        self._sources = tuple(sources)
        self._notifier = notifier
        self._log = log or (lambda _message: None)

    # entry point -----------------------------------------------------------

    def handle_update(self, update: dict[str, Any]) -> None:
        incoming = parse_update(update)
        if incoming is None:
            return
        if incoming.callback_id:
            self._api.answer_callback(incoming.callback_id)
        if not incoming.is_private:
            self._reply(
                incoming.chat_id,
                "Job-Scout works in a private chat only, so one person's job search stays "
                "theirs. Open a direct chat with me and send /start.",
            )
            return

        user = self._store.ensure_user(incoming.telegram_user_id, str(incoming.chat_id))
        if incoming.callback_data is not None:
            self._handle_callback(user.id, incoming.chat_id, incoming.callback_data)
            return
        self._handle_text(user.id, incoming.chat_id, incoming.text or "")

    # text commands ---------------------------------------------------------

    def _handle_text(self, user_id: int, chat_id: int, text: str) -> None:
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if command in ("/start", "/settings"):
            self._start_wizard(user_id, chat_id)
            return
        if command == "/status":
            self._reply(chat_id, self._status_text(user_id))
            return
        if command == "/pause":
            self._store.set_paused(user_id, True)
            self._reply(chat_id, "Paused. I will not message you until you send /resume.")
            return
        if command == "/resume":
            self._store.set_paused(user_id, False)
            self._reply(chat_id, "Resumed. New matching roles will arrive here.")
            return
        if command == "/digest":
            self._send_digest(user_id, chat_id)
            return
        if command == "/delete":
            self._reply(
                chat_id,
                "Delete everything? Your preferences, employers and alert history will be "
                "removed and monitoring stops. This cannot be undone.",
                (
                    (
                        Button("Yes, delete my data", "delete:confirm"),
                        Button("Keep it", "delete:cancel"),
                    ),
                ),
            )
            return
        if command == "/help":
            self._reply(chat_id, HELP)
            return
        if not text:
            self._reply(chat_id, HELP)
            return
        # Anything else is most likely a new user typing to the bot.
        self._reply(chat_id, "I did not understand that.\n\n" + HELP)

    # wizard ----------------------------------------------------------------

    def _start_wizard(self, user_id: int, chat_id: int) -> None:
        packs = available_packs(self._sources)
        if not packs:
            self._reply(chat_id, "The service has no employers configured yet. Please try later.")
            return
        stored = self._store.get_preferences(user_id)
        draft = conversation.Draft.from_json(stored) if stored else None
        step = conversation.begin(packs, draft)
        self._store.save_session(user_id, step.step, step.draft.as_json())
        self._reply(chat_id, step.reply.text, self._buttons(step.reply.buttons))

    def _handle_callback(self, user_id: int, chat_id: int, data: str) -> None:
        if data.startswith("delete:"):
            if data == "delete:confirm":
                self._store.delete_user(user_id)
                self._reply(
                    chat_id,
                    "Deleted. Your preferences, employers and alert history are gone, and "
                    "monitoring has stopped. Send /start if you want to begin again.",
                )
            else:
                self._reply(chat_id, "Kept. Nothing was changed.")
            return

        packs = available_packs(self._sources)
        session = self._store.get_session(user_id)
        if session is None:
            # A stale button from an old message: restart cleanly rather than guess.
            self._start_wizard(user_id, chat_id)
            return
        step_name, draft_json = session
        step = conversation.handle(step_name, conversation.Draft.from_json(draft_json), data, packs)
        if step.finished:
            self._store.clear_session(user_id)
            if step.settings is not None:
                self._save_scope(user_id, chat_id, step.settings, step.source_ids)
        else:
            self._store.save_session(user_id, step.step, step.draft.as_json())
        self._reply(chat_id, step.reply.text, self._buttons(step.reply.buttons))

    def _save_scope(
        self, user_id: int, chat_id: int, settings: dict, source_ids: tuple[str, ...]
    ) -> None:
        rules = user_rules(settings)
        self._store.save_preferences(user_id, settings, fingerprint=preferences_fingerprint(rules))
        self._store.set_subscriptions(user_id, source_ids)
        # Never announce history: the watermark is the newest stored role (D-095).
        self._store.set_watermark(user_id, self._storage.highest_job_id())
        self._log(f"user {user_id} saved a scope over {len(source_ids)} employer(s)")

    # replies ---------------------------------------------------------------

    def _status_text(self, user_id: int) -> str:
        store = self._store
        user = store.get_user_by_id(user_id)
        sources = store.list_subscriptions(user_id)
        counts = store.count_alerts(user_id)
        lines = ["Monitoring is paused." if user and user.paused else "Monitoring is on."]
        if not sources:
            lines.append("No employers chosen yet — send /start to set up your search.")
            return "\n".join(lines)
        lines.append(f"Employers: {len(sources)}")
        healthy = 0
        for source_id in sources:
            state = self._storage.get_source_state(source_id)
            if state is not None and state.last_scan_status == "success":
                healthy += 1
        lines.append(f"Employers scanned successfully last time: {healthy} of {len(sources)}")
        sent = counts.get("sent", 0)
        pending = counts.get("pending", 0)
        lines.append(f"Alerts sent to you: {sent}" + (f" ({pending} queued)" if pending else ""))
        if sent == 0:
            lines.append(
                "Nothing new has matched yet. That is normal: you are told about roles that "
                "appear after you set up. /digest shows what already matches."
            )
        return "\n".join(lines)

    def _send_digest(self, user_id: int, chat_id: int) -> None:
        user = self._store.get_user_by_id(user_id)
        if user is None:
            self._reply(chat_id, "Send /start to set up your search first.")
            return
        if not self._store.list_subscriptions(user_id):
            self._reply(chat_id, "No employers chosen yet — send /start to set up your search.")
            return
        matches = digest_matches(self._storage, self._store, user)
        if not matches:
            self._reply(
                chat_id,
                "Nothing matches your current filters right now. You can widen them with "
                "/settings, and I will message you when something new matches.",
            )
            return
        shown = matches[:DIGEST_LIMIT]
        text = "Roles that already match, newest first:\n\n" + "\n".join(shown)
        if len(matches) > len(shown):
            text += f"\n\n…and {len(matches) - len(shown)} more."
        self._reply(chat_id, text)

    def _reply(self, chat_id: int, text: str, buttons: Rows | None = None) -> None:
        try:
            self._api.send_message(chat_id, text, buttons=buttons or None)
        except BotApiError as exc:
            self._log(f"reply to {chat_id} failed: {exc}")

    @staticmethod
    def _buttons(rows: Sequence[Sequence[tuple[str, str]]]) -> Rows | None:
        """The wizard speaks (label, data) pairs; the Bot API wants `Button` objects."""
        if not rows:
            return None
        return tuple(tuple(Button(label, data) for label, data in row) for row in rows)

    # background work -------------------------------------------------------

    def deliver_pending(self) -> int:
        """Send whatever the notify pass queued. Returns how many messages went out."""
        if self._notifier is None:
            return 0
        sent = 0
        for user in self._store.active_users():
            report = self._notifier.deliver_pending(self._storage, user.chat_id)
            sent += report.sent
        return sent

    def notify(self) -> int:
        """Evaluate newly appearing roles for every subscriber, then deliver."""
        reports = notify_pass(self._storage, self._store)
        queued = sum(report.queued for report in reports)
        if queued:
            self._log(f"queued {queued} alert(s) for {len(reports)} user(s)")
        return self.deliver_pending()


def format_user_alert(notifier: Notifier, storage: Storage, chat_id: str) -> tuple[str, ...]:
    """The exact texts a notifier would send, for logging and tests."""
    return tuple(
        redact(format_message(notification)) or ""
        for notification in storage.list_pending_notifications(chat_id)
    )


__all__ = ["BotApp", "HELP", "Incoming", "format_user_alert", "parse_update"]
