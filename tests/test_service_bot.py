"""The bot app: commands, the saved wizard, and refusing what it should refuse (D-093).

A fake Bot API is enough here — the conversation is pure and the store is real — so these tests
assert the exact messages a user would receive, and that one user's interaction never touches
another's data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobscout.config import SourceConfig
from jobscout.http import FixtureHttpClient
from jobscout.matching import DEFAULT_RULES
from jobscout.models import DeliveryOutcome, DeliveryResult
from jobscout.notifier import Notifier
from jobscout.scanner import run_scan
from jobscout.service.app import BotApp, parse_update
from jobscout.service.store import ServiceStore
from jobscout.sources import get_adapter
from jobscout.storage import Storage

SOURCE = SourceConfig(
    id="software_co",
    employer="Software Co",
    adapter="lever",
    options={"site": "software_co"},
    access_note="test fixture",
)


class FakeApi:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, object]] = []
        self.answered: list[str] = []
        self.fail = False

    def send_message(self, chat_id, text, *, buttons=None):
        if self.fail:
            from jobscout.service.bot import BotApiError

            raise BotApiError("send failed")
        self.sent.append((chat_id, text, buttons))
        return {"message_id": len(self.sent)}

    def send_text(self, chat_id, text):
        return self.send_message(chat_id, text)

    def answer_callback(self, callback_id, text=None) -> None:
        self.answered.append(callback_id)

    @property
    def last(self) -> str:
        return self.sent[-1][1]


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, chat_id: str, text: str) -> DeliveryResult:
        self.sent.append((chat_id, text))
        return DeliveryResult(DeliveryOutcome.SENT, message_id=str(len(self.sent)))


def message_update(user_id: int, text: str, *, chat_type: str = "private") -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": user_id},
            "chat": {"id": user_id, "type": chat_type},
            "text": text,
        },
    }


def callback_update(user_id: int, data: str, *, chat_type: str = "private") -> dict:
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": user_id},
            "data": data,
            "message": {"message_id": 5, "chat": {"id": user_id, "type": chat_type}},
        },
    }


@pytest.fixture
def app(tmp_path: Path) -> BotApp:
    storage = Storage.open(tmp_path / "service.db")
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps(
            [
                {
                    "id": "s1",
                    "text": "Graduate Software Engineer",
                    "categories": {
                        "location": "London, United Kingdom",
                        "allLocations": ["London, United Kingdom"],
                    },
                    "workplaceType": "onsite",
                    "descriptionPlain": "Fixture.",
                    "createdAt": 1759276800000,
                    "hostedUrl": "https://example.test/s1",
                }
            ]
        ),
        encoding="utf-8",
    )
    run_scan(
        storage,
        get_adapter("lever"),
        SOURCE,
        FixtureHttpClient(payload),
        rules=DEFAULT_RULES,
        clock=lambda: "2026-10-25T08:00:00Z",
    )
    store = ServiceStore(storage)
    api = FakeApi()
    bot = BotApp(
        api,  # type: ignore[arg-type]
        storage,
        store,
        sources=(SOURCE,),
        notifier=Notifier(FakeTransport()),
    )
    bot.api = api  # type: ignore[attr-defined]
    bot.service = store  # type: ignore[attr-defined]
    return bot


def test_start_creates_the_user_and_opens_the_wizard(app: BotApp) -> None:
    api = app.api  # type: ignore[attr-defined]

    app.handle_update(message_update(111, "/start"))

    assert "1/5" in api.last
    assert api.sent[0][2], "the first step offers buttons"
    user = app.service.get_user(111)  # type: ignore[attr-defined]
    assert user is not None and user.chat_id == "111"
    assert app.service.get_session(user.id) is not None


def test_the_whole_wizard_saves_a_scope_and_watermark(app: BotApp) -> None:
    app.handle_update(message_update(111, "/start"))
    for data in ("f:next", "e:next", "l:uk", "p:all", "save"):
        app.handle_update(callback_update(111, data))

    api = app.api  # type: ignore[attr-defined]
    assert "Saved" in api.last
    assert "monitor" in api.last.lower()

    user = app.service.get_user(111)  # type: ignore[attr-defined]
    assert user is not None
    stored = app.service.get_preferences(user.id)  # type: ignore[attr-defined]
    assert stored is not None and stored["locations"]["mode"] == "uk"
    assert app.service.list_subscriptions(user.id) == (SOURCE.id,)  # type: ignore[attr-defined]
    assert user.watermark > 0, "saving sets the watermark so history is not re-announced"
    assert app.service.get_session(user.id) is None  # type: ignore[attr-defined]


def test_callbacks_are_always_acknowledged(app: BotApp) -> None:
    app.handle_update(callback_update(111, "f:next"))

    assert app.api.answered == ["cb-1"]  # type: ignore[attr-defined]


def test_status_says_monitoring_is_working_without_promising_alerts(app: BotApp) -> None:
    app.handle_update(message_update(111, "/start"))
    for data in ("f:next", "e:next", "l:uk", "p:all", "save"):
        app.handle_update(callback_update(111, data))

    app.handle_update(message_update(111, "/status"))

    text = app.api.last  # type: ignore[attr-defined]
    assert "Monitoring is on" in text
    assert "Employers: 1" in text
    assert "Nothing new has matched yet" in text


def test_pause_and_resume(app: BotApp) -> None:
    app.handle_update(message_update(111, "/pause"))
    user = app.service.get_user(111)  # type: ignore[attr-defined]
    assert user is not None and user.paused

    app.handle_update(message_update(111, "/resume"))
    user = app.service.get_user(111)  # type: ignore[attr-defined]
    assert user is not None and not user.paused


def test_digest_needs_a_scope_first(app: BotApp) -> None:
    app.handle_update(message_update(111, "/digest"))

    assert "No employers chosen yet" in app.api.last  # type: ignore[attr-defined]


def test_delete_requires_confirmation_and_removes_the_user(app: BotApp) -> None:
    app.handle_update(message_update(111, "/start"))
    for data in ("f:next", "e:next", "l:uk", "p:all", "save"):
        app.handle_update(callback_update(111, data))
    user = app.service.get_user(111)  # type: ignore[attr-defined]
    assert user is not None

    app.handle_update(message_update(111, "/delete"))
    assert "cannot be undone" in app.api.last  # type: ignore[attr-defined]
    assert app.service.get_user(111) is not None  # type: ignore[attr-defined]

    app.handle_update(callback_update(111, "delete:cancel"))
    assert app.service.get_user(111) is not None  # type: ignore[attr-defined]

    app.handle_update(message_update(111, "/delete"))
    app.handle_update(callback_update(111, "delete:confirm"))
    assert app.service.get_user(111) is None  # type: ignore[attr-defined]


def test_a_group_chat_is_refused_and_creates_no_user(app: BotApp) -> None:
    app.handle_update(message_update(999, "/start", chat_type="group"))

    assert "private chat only" in app.api.last  # type: ignore[attr-defined]
    assert app.service.get_user(999) is None  # type: ignore[attr-defined]


def test_unknown_text_gets_help_rather_than_silence(app: BotApp) -> None:
    app.handle_update(message_update(111, "hello?"))

    assert "/start" in app.api.last  # type: ignore[attr-defined]


def test_two_users_keep_separate_scopes(app: BotApp) -> None:
    for user_id, location in ((111, "l:uk"), (222, "l:london_only")):
        app.handle_update(message_update(user_id, "/start"))
        for data in ("f:next", "e:next", location, "p:all", "save"):
            app.handle_update(callback_update(user_id, data))

    one = app.service.get_user(111)  # type: ignore[attr-defined]
    two = app.service.get_user(222)  # type: ignore[attr-defined]
    assert one is not None and two is not None
    assert app.service.get_preferences(one.id)["locations"]["mode"] == "uk"  # type: ignore[attr-defined]
    assert (
        app.service.get_preferences(two.id)["locations"]["mode"] == "pinned"  # type: ignore[attr-defined]
    )


def test_a_stale_button_restarts_the_wizard_instead_of_failing(app: BotApp) -> None:
    app.handle_update(callback_update(111, "f:next"))

    assert "1/5" in app.api.last  # type: ignore[attr-defined]


def test_a_failed_reply_does_not_stop_the_bot(app: BotApp) -> None:
    app.api.fail = True  # type: ignore[attr-defined]

    app.handle_update(message_update(111, "/help"))  # must not raise
    app.api.fail = False  # type: ignore[attr-defined]
    app.handle_update(message_update(111, "/help"))
    assert "/digest" in app.api.last  # type: ignore[attr-defined]


def test_parse_update_ignores_what_it_cannot_serve() -> None:
    assert parse_update({"edited_message": {}}) is None
    assert parse_update({"callback_query": {"id": "x"}}) is None
    parsed = parse_update(message_update(111, "/start"))
    assert parsed is not None and parsed.is_private
