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
from .notify import (
    DIGEST_PAGE_SIZE,
    DigestEntry,
    DigestPage,
    digest_entries,
    format_digest_page,
    notify_pass,
    user_rules,
)
from .packs import Pack, available_packs
from .store import ServiceStore

HELP = (
    "Job-Scout finds new UK early-career roles on employers' own job boards and messages you "
    "when one matches.\n\n"
    "/start — set up or restart your search\n"
    "/settings — change roles, location or employers\n"
    "/digest — browse roles that already match, 5 at a time\n"
    "/status — is monitoring working?\n"
    "/pause — stop alerts\n"
    "/resume — start alerts again\n"
    "/delete — remove my data\n"
    "/help — this message"
)


@dataclass(frozen=True)
class Incoming:
    """One user-visible interaction, reduced to what the app needs."""

    telegram_user_id: int
    chat_id: int
    chat_type: str
    text: str | None
    callback_id: str | None
    callback_data: str | None
    message_id: int | None = None

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
        message_id = message.get("message_id")
        return Incoming(
            telegram_user_id=user_id,
            chat_id=chat_id,
            chat_type=str(chat.get("type", "")),
            text=None,
            callback_id=str(query.get("id", "")),
            callback_data=str(query.get("data", "")),
            message_id=message_id if isinstance(message_id, int) else None,
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
        message_id=message.get("message_id")
        if isinstance(message.get("message_id"), int)
        else None,
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
        # Evaluating a user's whole scope takes seconds, so keep the last digest per user and
        # only recompute when something that could change the answer has moved.
        self._digest_cache: dict[
            int, tuple[Any, tuple[str, ...], int, tuple[DigestEntry, ...]]
        ] = {}

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
            self._handle_callback(
                user.id,
                incoming.chat_id,
                incoming.callback_data,
                message_id=incoming.message_id,
            )
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
        step = conversation.begin(packs, self._restored_draft(user_id, packs))
        self._store.save_session(user_id, step.step, step.draft.as_json())
        self._reply(chat_id, step.reply.text, self._buttons(step.reply.buttons))

    def _restored_draft(self, user_id: int, packs: Sequence[Pack]) -> conversation.Draft | None:
        """The draft to reopen with: the saved answers, or None for a first run.

        Rebuilding from the stored preferences *and* the stored subscriptions is what makes
        /settings show the user's real scope; a first-time user gets the wizard's own defaults.
        """
        saved = self._store.get_preferences(user_id)
        if not saved or not saved.get("role_families"):
            return None
        return conversation.draft_from_state(saved, self._store.list_subscriptions(user_id), packs)

    def _handle_callback(
        self, user_id: int, chat_id: int, data: str, *, message_id: int | None = None
    ) -> None:
        if data.startswith("d:"):
            self._open_digest_page(user_id, chat_id, data, message_id=message_id)
            return
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
        fingerprint = preferences_fingerprint(rules)
        # Merge over what was saved so keys the wizard does not show (title and seniority
        # exclusions) survive a re-save instead of being silently deleted.
        previous = self._store.get_preferences(user_id) or {}
        merged = {**previous, **settings}
        unchanged = self._store.get_fingerprint(user_id) == fingerprint and tuple(
            sorted(self._store.list_subscriptions(user_id))
        ) == tuple(sorted(source_ids))
        self._store.save_preferences(user_id, merged, fingerprint=fingerprint)
        if unchanged:
            # Re-saving identical settings must not narrow the subscription set or jump the
            # watermark past roles that arrived since the last save (D-095).
            self._log(f"user {user_id} re-saved an unchanged scope")
            return
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
        page = self._digest(user, page=1)
        self._reply(chat_id, format_digest_page(page), self._digest_buttons(page))

    def _open_digest_page(
        self, user_id: int, chat_id: int, data: str, *, message_id: int | None
    ) -> None:
        """Show one digest page, editing the message the button came from when possible."""
        user = self._store.get_user_by_id(user_id)
        if user is None or not self._store.list_subscriptions(user_id):
            self._reply(chat_id, "Send /start to set up your search first.")
            return
        try:
            requested = int(data.split(":", 1)[1])
        except (IndexError, ValueError):
            requested = 1
        # A stale button can ask for a page that no longer exists: clamp rather than fail.
        page = self._digest(user, page=requested)
        text = format_digest_page(page)
        buttons = self._digest_buttons(page)
        if message_id is not None:
            try:
                self._api.edit_message_text(chat_id, message_id, text, buttons=buttons)
            except BotApiError as exc:
                # The message may have been deleted or be too old to edit; a fresh page is
                # better than silence. "message is not modified" is handled inside BotApi.
                self._log(f"digest edit failed, sending a new message: {exc}")
                self._reply(chat_id, text, buttons)
        else:
            self._reply(chat_id, text, buttons)

    def _digest(self, user: Any, *, page: int) -> DigestPage:
        entries = self._digest_cache_entries(user)
        return DigestPage.paginate(entries, page=page, page_size=DIGEST_PAGE_SIZE)

    def _digest_cache_entries(self, user: Any) -> tuple[DigestEntry, ...]:
        """The user's whole match set, recomputed only when the scope or the jobs have moved."""
        fingerprint = self._store.get_fingerprint(user.id)
        subscriptions = tuple(sorted(self._store.list_subscriptions(user.id)))
        newest = self._storage.highest_job_id()
        cached = self._digest_cache.get(user.id)
        if cached is not None and cached[:3] == (fingerprint, subscriptions, newest):
            return cached[3]
        entries = digest_entries(self._storage, self._store, user)
        self._digest_cache[user.id] = (fingerprint, subscriptions, newest, entries)
        return entries

    def _digest_buttons(self, page: DigestPage) -> Rows | None:
        row: list[Button] = []
        if page.has_previous:
            row.append(Button("◀ Previous", f"d:{page.page - 1}"))
        if page.has_next:
            row.append(Button("Next ▶", f"d:{page.page + 1}"))
        return (tuple(row),) if row else None

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
