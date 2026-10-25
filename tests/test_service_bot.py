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
from jobscout.service import app as app_module
from jobscout.service.app import BotApp, parse_update
from jobscout.service.bot import BotApi
from jobscout.service.packs import EMPLOYMENT, FAMILIES, LOCATIONS
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
        self.edits: list[tuple[int, int, str, object]] = []
        self.answered: list[str] = []
        self.fail = False
        self.fail_edit = False

    def send_message(self, chat_id, text, *, buttons=None):
        if self.fail:
            from jobscout.service.bot import BotApiError

            raise BotApiError("send failed")
        self.sent.append((chat_id, text, buttons))
        return {"message_id": len(self.sent)}

    def edit_message_text(self, chat_id, message_id, text, *, buttons=None):
        if self.fail_edit:
            from jobscout.service.bot import BotApiError

            raise BotApiError("edit failed")
        self.edits.append((chat_id, message_id, text, buttons))
        return {"message_id": message_id}

    def send_text(self, chat_id, text):
        return self.send_message(chat_id, text)

    def answer_callback(self, callback_id, text=None) -> None:
        self.answered.append(callback_id)

    @property
    def last(self) -> str:
        return self.sent[-1][1]

    @property
    def last_buttons(self) -> object:
        return self.sent[-1][2]

    @property
    def last_edit(self) -> str:
        return self.edits[-1][2]


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


class RecordingPoster:
    """Stands in for the HTTP client so the real `BotApi` serialises the real payload."""

    def __init__(self) -> None:
        self.payloads: list[tuple[str, dict]] = []

    def post_json(self, url: str, payload: dict) -> dict:
        self.payloads.append((url, payload))
        return {"ok": True, "result": {"message_id": len(self.payloads)}}

    def messages(self) -> list[dict]:
        return [payload for url, payload in self.payloads if url.endswith("/sendMessage")]

    def edits(self) -> list[dict]:
        return [payload for url, payload in self.payloads if url.endswith("/editMessageText")]


def test_buttons_reach_the_bot_api_as_a_valid_inline_keyboard(tmp_path: Path) -> None:
    """A fake API accepts anything; only the real serialiser proves Telegram would accept it."""
    storage = Storage.open(tmp_path / "service.db")
    poster = RecordingPoster()
    bot = BotApp(
        BotApi("123:test-token", poster=poster),
        storage,
        ServiceStore(storage),
        sources=(SOURCE,),
    )

    bot.handle_update(message_update(111, "/start"))

    keyboard = poster.messages()[0]["reply_markup"]["inline_keyboard"]
    assert any(button["callback_data"] == "f:next" for row in keyboard for button in row)
    assert all(
        set(button) == {"text", "callback_data"} and button["text"]
        for row in keyboard
        for button in row
    )


def test_the_delete_prompt_also_sends_working_buttons(tmp_path: Path) -> None:
    storage = Storage.open(tmp_path / "service.db")
    poster = RecordingPoster()
    bot = BotApp(
        BotApi("123:test-token", poster=poster),
        storage,
        ServiceStore(storage),
        sources=(SOURCE,),
    )

    bot.handle_update(message_update(111, "/delete"))

    keyboard = poster.messages()[-1]["reply_markup"]["inline_keyboard"]
    assert keyboard[0][0]["callback_data"] == "delete:confirm"


# ---------------------------------------------------------------------------
# /settings must restore what was saved, and a no-change save must not alter it
# ---------------------------------------------------------------------------

SAVED_SCOPE = {
    "role_families": ["software", "quant"],
    "employment_types": ["graduate_scheme", "internship"],
    "locations": {"mode": "pinned", "cities": ["London"], "remote_uk": True, "hybrid_uk": True},
    # A key the wizard never shows: it must survive a re-save rather than be deleted.
    "seniority_exclusions": ["staff"],
}


def _subscribed_user(app: BotApp) -> int:
    store = app.service  # type: ignore[attr-defined]
    storage = store.storage
    user = store.ensure_user(111, "111")
    store.save_preferences(user.id, SAVED_SCOPE, fingerprint="fp-saved")
    store.set_subscriptions(user.id, (SOURCE.id,))
    store.set_watermark(user.id, storage.highest_job_id())
    return user.id


def test_settings_reopens_with_the_saved_answers_selected(app: BotApp) -> None:
    _subscribed_user(app)

    app.handle_update(message_update(111, "/settings"))

    buttons = app.api.last_buttons  # type: ignore[attr-defined]
    labels = [button.label for row in buttons for button in row]
    selected = {label for label in labels if label.startswith("✅")}
    assert "1/5" in app.api.last  # type: ignore[attr-defined]
    assert any("Software engineering" in label for label in selected)
    assert any("Quantitative / risk" in label for label in selected)
    assert not any("Data / AI / ML" in label for label in selected), "unselected stays unselected"


def test_saving_settings_without_changes_preserves_preferences_and_subscriptions(
    app: BotApp,
) -> None:
    user_id = _subscribed_user(app)
    store = app.service  # type: ignore[attr-defined]
    before_user = store.get_user_by_id(user_id)

    # Walk the wizard pressing only what was already saved.
    app.handle_update(message_update(111, "/settings"))
    for data in ("f:next", "e:next", "l:london_remote", "p:all", "save"):
        app.handle_update(callback_update(111, data))

    after_user = store.get_user_by_id(user_id)
    stored = store.get_preferences(user_id)
    assert stored["role_families"] == ["software", "quant"]
    assert stored["employment_types"] == ["graduate_scheme", "internship"]
    assert stored["locations"] == SAVED_SCOPE["locations"]
    assert stored["seniority_exclusions"] == ["staff"], "keys the wizard does not show survive"
    assert store.list_subscriptions(user_id) == (SOURCE.id,)
    assert before_user is not None and after_user is not None
    assert after_user.watermark == before_user.watermark, "an unchanged save must not move it"


def test_settings_keeps_an_employer_set_that_matches_no_pack(app: BotApp) -> None:
    """A saved set that is not exactly one of the packs must survive reopening /settings."""
    store = app.service  # type: ignore[attr-defined]
    user = store.ensure_user(222, "222")
    store.save_preferences(user.id, SAVED_SCOPE, fingerprint="fp-saved")
    extra = SourceConfig(
        id="another_co",
        employer="Another Co",
        adapter="lever",
        options={"site": "another_co"},
        access_note="test fixture",
    )
    third = SourceConfig(
        id="third_co",
        employer="Third Co",
        adapter="lever",
        options={"site": "third_co"},
        access_note="test fixture",
    )
    # Two of the three offered employers: matches no pack, so only "keep" can preserve it.
    store.set_subscriptions(user.id, (SOURCE.id, extra.id))
    bot = BotApp(
        app.api,  # type: ignore[arg-type]
        store.storage,
        store,
        sources=(SOURCE, extra, third),
    )
    bot.api = app.api  # type: ignore[attr-defined]
    bot.service = store  # type: ignore[attr-defined]

    bot.handle_update(message_update(222, "/settings"))
    for data in ("f:next", "e:next", "l:london_remote"):
        bot.handle_update(callback_update(222, data))

    labels = [button.label for row in app.api.last_buttons for button in row]  # type: ignore
    assert any(label.startswith("✅ Keep the 2 employers") for label in labels), labels
    assert not any(label.startswith("✅ Every employer") for label in labels), "no false match"

    for data in ("p:__keep__", "save"):
        bot.handle_update(callback_update(222, data))

    assert set(store.list_subscriptions(user.id)) == {SOURCE.id, extra.id}


# ---------------------------------------------------------------------------
# /digest browsing
# ---------------------------------------------------------------------------


def _digest_app(tmp_path: Path, roles: int, *, api=None) -> BotApp:
    storage = Storage.open(tmp_path / "digest.db")
    payload = tmp_path / "digest.json"
    payload.write_text(
        json.dumps(
            [
                {
                    "id": f"d{index:03d}",
                    "text": f"Graduate Software Engineer {index}",
                    "categories": {
                        "location": "London, United Kingdom",
                        "allLocations": ["London, United Kingdom"],
                    },
                    "workplaceType": "onsite",
                    "descriptionPlain": "Fixture.",
                    "createdAt": 1759276800000,
                    "hostedUrl": f"https://example.test/d{index:03d}",
                }
                for index in range(roles)
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
    user = store.ensure_user(111, "111")
    store.save_preferences(user.id, SAVED_SCOPE, fingerprint="fp")
    store.set_subscriptions(user.id, (SOURCE.id,))
    store.set_watermark(user.id, storage.highest_job_id())
    api = api or FakeApi()
    bot = BotApp(api, storage, store, sources=(SOURCE,))  # type: ignore[arg-type]
    bot.api = api  # type: ignore[attr-defined]
    bot.service = store  # type: ignore[attr-defined]
    return bot


def test_the_digest_states_the_full_total_and_offers_next(tmp_path: Path) -> None:
    bot = _digest_app(tmp_path, 12)

    bot.handle_update(message_update(111, "/digest"))

    assert "12 matching open roles" in bot.api.last  # type: ignore[attr-defined]
    assert "Showing 1–5 of 12" in bot.api.last  # type: ignore[attr-defined]
    buttons = [button for row in bot.api.last_buttons for button in row]  # type: ignore[attr-defined]
    assert [button.label for button in buttons] == ["Next ▶"]


def test_a_digest_callback_edits_the_same_message_instead_of_sending_a_new_one(
    tmp_path: Path,
) -> None:
    bot = _digest_app(tmp_path, 12)
    bot.handle_update(message_update(111, "/digest"))
    sent_before = len(bot.api.sent)  # type: ignore[attr-defined]

    bot.handle_update(callback_update(111, "d:2"))

    assert len(bot.api.sent) == sent_before, "navigation must not add messages"
    chat_id, message_id, text, buttons = bot.api.edits[-1]  # type: ignore[attr-defined]
    assert message_id == 5, "the message the button came from"
    assert "Showing 6–10 of 12" in text
    assert [button.label for row in buttons for button in row] == ["◀ Previous", "Next ▶"]


def test_a_stale_digest_page_is_clamped_not_crashed(tmp_path: Path) -> None:
    bot = _digest_app(tmp_path, 7)

    bot.handle_update(callback_update(111, "d:999"))
    assert "Showing 6–7 of 7" in bot.api.last_edit  # type: ignore[attr-defined]

    bot.handle_update(callback_update(111, "d:not-a-number"))
    assert "Showing 1–5 of 7" in bot.api.last_edit  # type: ignore[attr-defined]


def test_a_failed_edit_falls_back_to_a_new_message(tmp_path: Path) -> None:
    bot = _digest_app(tmp_path, 12)
    bot.api.fail_edit = True  # type: ignore[attr-defined]

    bot.handle_update(callback_update(111, "d:2"))

    assert "Showing 6–10 of 12" in bot.api.last  # type: ignore[attr-defined]


def test_browsing_the_digest_queues_no_alerts(tmp_path: Path) -> None:
    bot = _digest_app(tmp_path, 12)
    store = bot.service  # type: ignore[attr-defined]
    user = store.get_user(111)
    assert user is not None
    before = store.count_alerts(user.id)

    bot.handle_update(message_update(111, "/digest"))
    for page in (2, 3, 1):
        bot.handle_update(callback_update(111, f"d:{page}"))

    assert store.count_alerts(user.id) == before
    counts = store.storage.notification_counts("111")
    assert counts.get("pending", 0) == 0


def test_onboarding_keyboards_use_one_button_per_row(tmp_path: Path) -> None:
    """Choice buttons get their own row; only short navigation pairs may share one.

    Checked on the serialised payload, because that is the shape Telegram actually receives.
    """
    storage = Storage.open(tmp_path / "screens.db")
    poster = RecordingPoster()
    bot = BotApp(
        BotApi("123:test-token", poster=poster),
        storage,
        ServiceStore(storage),
        sources=(SOURCE,),
    )

    bot.handle_update(message_update(111, "/start"))
    for data in ("f:next", "e:next", "l:uk", "p:all"):
        bot.handle_update(callback_update(111, data))

    keys = [
        payload["reply_markup"]["inline_keyboard"]
        for payload in poster.messages()
        if payload.get("reply_markup")
    ]
    assert len(keys) >= 5, "every wizard step must send a keyboard"
    choice_rows = 0
    for keyboard in keys:
        for row in keyboard:
            assert len(row) <= 2, f"crowded row: {[button['text'] for button in row]}"
            for button in row:
                assert len(button["callback_data"].encode()) <= 64
                assert len(button["text"]) <= 60
            if any(button["callback_data"][:2] in {"f:", "e:", "l:", "p:"} for button in row):
                choice_rows += 1
                assert len(row) == 1, f"choices share a row: {[b['text'] for b in row]}"
    assert choice_rows >= len(FAMILIES) + len(EMPLOYMENT) + len(LOCATIONS), choice_rows


def test_paging_reuses_one_digest_computation(tmp_path: Path, monkeypatch) -> None:
    """Evaluating a whole scope takes seconds; flipping pages must not repeat it."""
    calls: list[int] = []
    real = app_module.digest_entries

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(app_module, "digest_entries", counting)
    bot = _digest_app(tmp_path, 12)

    bot.handle_update(message_update(111, "/digest"))
    for page in (2, 3, 1):
        bot.handle_update(callback_update(111, f"d:{page}"))

    assert len(calls) == 1, "one evaluation serves every page"


def test_a_changed_scope_recomputes_the_digest(tmp_path: Path, monkeypatch) -> None:
    calls: list[int] = []
    real = app_module.digest_entries

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(app_module, "digest_entries", counting)
    bot = _digest_app(tmp_path, 12)
    store = bot.service  # type: ignore[attr-defined]
    user = store.get_user(111)
    assert user is not None

    bot.handle_update(message_update(111, "/digest"))
    store.save_preferences(user.id, SAVED_SCOPE, fingerprint="a-different-fingerprint")
    bot.handle_update(message_update(111, "/digest"))

    assert len(calls) == 2, "a new scope must not be served from the old answer"


def test_the_digest_reaches_the_bot_api_as_a_valid_keyboard(tmp_path: Path) -> None:
    """A fake API accepts anything; only the real serialiser proves Telegram would accept it."""
    poster = RecordingPoster()
    bot = _digest_app(tmp_path, 12, api=BotApi("123:test-token", poster=poster))

    bot.handle_update(message_update(111, "/digest"))
    bot.handle_update(callback_update(111, "d:2"))

    sent = poster.messages()[-1]["reply_markup"]["inline_keyboard"]
    edited = poster.edits()[-1]["reply_markup"]["inline_keyboard"]
    assert [button["callback_data"] for row in sent for button in row] == ["d:2"]
    assert [button["callback_data"] for row in edited for button in row] == ["d:1", "d:3"]
    for keyboard in (sent, edited):
        for row in keyboard:
            for button in row:
                assert set(button) == {"text", "callback_data"}, button
                assert len(button["callback_data"].encode()) <= 64
