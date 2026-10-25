"""The setup conversation is a pure state machine, so it is tested without Telegram (D-096)."""

from __future__ import annotations

from jobscout.config import SourceConfig
from jobscout.service import conversation
from jobscout.service.packs import available_packs


def sources(*ids: str) -> tuple[SourceConfig, ...]:
    return tuple(
        SourceConfig(id=source_id, employer=source_id.title(), adapter="lever") for source_id in ids
    )


PACKS = available_packs(
    sources("palantir", "wise", "monzo", "arm", "barclays", "lloyds_graduate", "stripe")
)


def test_the_wizard_starts_at_the_first_step_with_sensible_defaults() -> None:
    step = conversation.begin(PACKS)

    assert step.step == conversation.STEP_FAMILIES
    assert "1/5" in step.reply.text
    assert step.draft.families, "a new user starts with something rather than nothing"
    labels = [button[0] for row in step.reply.buttons for button in row]
    assert any("Software engineering" in label for label in labels)
    assert "Done →" in labels


def test_callback_data_stays_inside_telegrams_limit() -> None:
    step = conversation.begin(PACKS)
    for _row in step.reply.buttons:
        for _label, data in _row:
            assert len(data) <= 64


def test_a_full_run_produces_settings_and_sources() -> None:
    step = conversation.begin(PACKS)
    draft = step.draft

    # families: keep the defaults, move on
    step = conversation.handle(step.step, draft, "f:next", PACKS)
    assert step.step == conversation.STEP_EMPLOYMENT
    draft = step.draft

    step = conversation.handle(step.step, draft, "e:next", PACKS)
    assert step.step == conversation.STEP_LOCATION
    draft = step.draft

    step = conversation.handle(step.step, draft, "l:london_only", PACKS)
    assert step.step == conversation.STEP_EMPLOYERS
    assert step.draft.location == "london_only"
    draft = step.draft

    step = conversation.handle(step.step, draft, "p:researched", PACKS)
    assert step.step == conversation.STEP_REVIEW
    assert "Does this look right?" in step.reply.text
    assert "London only" in step.reply.text
    draft = step.draft

    step = conversation.handle(step.step, draft, "save", PACKS)

    assert step.finished
    assert step.settings is not None
    assert step.settings["locations"] == {
        "mode": "pinned",
        "cities": ["London"],
        "remote_uk": False,
        "hybrid_uk": False,
    }
    assert step.settings["role_families"] == ["software", "data"]
    assert set(step.source_ids) == {
        "palantir",
        "wise",
        "monzo",
        "arm",
        "barclays",
        "lloyds_graduate",
    }


def test_a_toggle_switches_one_family_off_and_on() -> None:
    step = conversation.begin(PACKS)
    draft = step.draft

    step = conversation.handle(step.step, draft, "f:software", PACKS)
    assert "software" not in step.draft.families

    step = conversation.handle(step.step, step.draft, "f:software", PACKS)
    assert "software" in step.draft.families


def test_the_wizard_refuses_to_advance_with_nothing_chosen() -> None:
    step = conversation.begin(PACKS, conversation.Draft(families=(), employment=("internship",)))

    step = conversation.handle(step.step, step.draft, "f:next", PACKS)

    assert step.step == conversation.STEP_FAMILIES
    assert "at least one kind of role" in step.reply.text


def test_cancelling_changes_nothing() -> None:
    step = conversation.begin(PACKS)

    step = conversation.handle(step.step, step.draft, "cancel", PACKS)

    assert step.finished
    assert step.settings is None
    assert "Nothing was changed" in step.reply.text


def test_change_shortcuts_jump_back_to_an_earlier_step() -> None:
    step = conversation.begin(PACKS)

    step = conversation.handle(step.step, step.draft, "goto:employers", PACKS)

    assert step.step == conversation.STEP_EMPLOYERS
    assert "4/5" in step.reply.text


def test_an_unknown_button_re_shows_the_step_rather_than_failing() -> None:
    step = conversation.begin(PACKS)

    step = conversation.handle(step.step, step.draft, "nonsense", PACKS)

    assert step.step == conversation.STEP_FAMILIES
    assert step.reply.text


def test_packs_only_offer_employers_the_service_actually_has() -> None:
    partial = available_packs(sources("palantir", "monzo"))

    keys = [pack.key for pack in partial]
    assert keys == ["all"], "a pack is only offered when every employer in it is available"
    assert partial[0].source_ids == ("monzo", "palantir")


# ---------------------------------------------------------------------------
# Keyboard layout: one choice per row, so long labels never crowd a row (D-102)
# ---------------------------------------------------------------------------

CHOICE_PREFIXES = ("f:", "e:", "l:", "p:")


def _every_reply(packs=PACKS) -> list[conversation.Reply]:
    """Each reply the wizard sends when a user walks it from the first screen to the last."""
    replies: list[conversation.Reply] = []
    step = conversation.begin(packs)
    replies.append(step.reply)
    for data in ("f:next", "e:next", "l:uk", "p:all", "save"):
        step = conversation.handle(step.step, step.draft, data, packs)
        replies.append(step.reply)
    return replies


def test_choice_buttons_each_get_their_own_row() -> None:
    for reply in _every_reply():
        for row in reply.buttons:
            assert len(row) <= 2, [label for label, _data in row]
            if any(data.startswith(CHOICE_PREFIXES) for _label, data in row):
                assert len(row) == 1, [label for label, _data in row]


def test_no_button_label_is_long_enough_to_crowd_a_row_on_a_phone() -> None:
    for reply in _every_reply():
        for row in reply.buttons:
            for label, data in row:
                assert len(label) <= 40, label
                assert len(data.encode()) <= 64, data


def test_a_growing_catalogue_still_gets_one_row_per_employer_pack() -> None:
    many = available_packs(sources(*(f"employer_{index:02d}" for index in range(30))))

    reply = conversation.step_reply(conversation.STEP_EMPLOYERS, conversation.default_draft(), many)

    assert len(reply.buttons) == 1, "only the 'every employer' pack is available here"
    assert len(reply.buttons[0]) == 1
    assert reply.buttons[0][0][0].endswith("Every employer on offer")


# ---------------------------------------------------------------------------
# /settings reopening: the draft mirrors what was saved, not the defaults
# ---------------------------------------------------------------------------

SAVED_SETTINGS = {
    "role_families": ["software", "quant"],
    "employment_types": ["graduate_scheme", "internship"],
    "locations": {"mode": "pinned", "cities": ["London"], "remote_uk": True, "hybrid_uk": True},
}

EVERY_SOURCE = ("arm", "barclays", "lloyds_graduate", "monzo", "palantir", "stripe", "wise")


def test_a_saved_pack_choice_reopens_selected_and_saves_unchanged() -> None:
    draft = conversation.draft_from_state(SAVED_SETTINGS, EVERY_SOURCE, PACKS)

    assert draft.families == ("software", "quant")
    assert draft.employment == ("graduate_scheme", "internship")
    assert draft.location == "london_remote"
    assert draft.pack == "all"

    step = conversation.handle(conversation.STEP_REVIEW, draft, "save", PACKS)
    assert step.settings == SAVED_SETTINGS
    assert set(step.source_ids) == set(EVERY_SOURCE)


def test_a_saved_employer_set_matching_no_pack_is_offered_as_keep_and_kept_verbatim() -> None:
    chosen = ("palantir", "wise")
    draft = conversation.draft_from_state(SAVED_SETTINGS, chosen, PACKS)

    assert draft.pack == conversation.KEEP_PACK
    assert draft.sources == chosen

    employers = conversation.step_reply(conversation.STEP_EMPLOYERS, draft, PACKS)
    labels = [label for row in employers.buttons for label, _data in row]
    assert any(label.startswith("\u2705 Keep the 2 employers") for label in labels), labels

    step = conversation.handle(conversation.STEP_REVIEW, draft, "save", PACKS)
    assert step.source_ids == chosen
    assert step.settings == SAVED_SETTINGS


def test_a_saved_location_matching_no_preset_is_offered_as_keep_and_kept_verbatim() -> None:
    custom = {"mode": "pinned", "cities": ["Manchester"], "remote_uk": False, "hybrid_uk": True}
    settings = {**SAVED_SETTINGS, "locations": custom}

    draft = conversation.draft_from_state(settings, EVERY_SOURCE, PACKS)
    assert draft.location == conversation.KEEP_LOCATION
    assert draft.location_custom == custom

    locations = conversation.step_reply(conversation.STEP_LOCATION, draft, PACKS)
    labels = [label for row in locations.buttons for label, _data in row]
    assert any(label.startswith("\u2705 Keep my current location") for label in labels), labels

    step = conversation.handle(conversation.STEP_REVIEW, draft, "save", PACKS)
    assert step.settings == settings


def test_choosing_a_preset_replaces_a_kept_custom_location() -> None:
    custom = {"mode": "pinned", "cities": ["Manchester"], "remote_uk": False, "hybrid_uk": True}
    draft = conversation.draft_from_state({**SAVED_SETTINGS, "locations": custom}, (), PACKS)

    step = conversation.handle(conversation.STEP_LOCATION, draft, "l:uk", PACKS)

    assert step.draft.location == "uk"
    assert step.draft.location_custom == {}, "an explicit choice wins over the kept value"


def test_choosing_a_pack_replaces_a_kept_employer_set() -> None:
    draft = conversation.draft_from_state(SAVED_SETTINGS, ("palantir", "wise"), PACKS)

    step = conversation.handle(conversation.STEP_EMPLOYERS, draft, "p:all", PACKS)

    assert step.draft.pack == "all"
    assert step.draft.sources == ()


def test_keeping_the_saved_employers_survives_a_round_trip_through_the_session() -> None:
    draft = conversation.draft_from_state(SAVED_SETTINGS, ("palantir", "wise"), PACKS)

    restored = conversation.Draft.from_json(draft.as_json())

    assert restored == draft
    assert restored.sources == ("palantir", "wise")
