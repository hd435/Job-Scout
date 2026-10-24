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
