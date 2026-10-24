"""The setup conversation, as a pure state machine (D-091, D-096).

Nothing here sends a message or reads a database: given the current step, the draft answers, and
a callback string, it returns the next step and the exact reply to show. That makes the whole
wizard testable without Telegram, which matters because the conversation *is* the product for a
hosted user.

Five steps: role families → employment types → location → employers → review.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from .packs import EMPLOYMENT, FAMILIES, LOCATION_BY_KEY, LOCATIONS, Pack

STEP_FAMILIES = "families"
STEP_EMPLOYMENT = "employment"
STEP_LOCATION = "location"
STEP_EMPLOYERS = "employers"
STEP_REVIEW = "review"

STEPS = (STEP_FAMILIES, STEP_EMPLOYMENT, STEP_LOCATION, STEP_EMPLOYERS, STEP_REVIEW)

Rows = tuple[tuple[tuple[str, str], ...], ...]


@dataclass(frozen=True)
class Reply:
    text: str
    buttons: Rows = ()
    markdown: bool = False


@dataclass(frozen=True)
class Draft:
    families: tuple[str, ...] = ()
    employment: tuple[str, ...] = ()
    location: str = "london_remote"
    pack: str = "all"

    def as_json(self) -> dict:
        return {
            "families": list(self.families),
            "employment": list(self.employment),
            "location": self.location,
            "pack": self.pack,
        }

    @classmethod
    def from_json(cls, data: dict) -> Draft:
        return cls(
            families=tuple(str(item) for item in data.get("families", ())),
            employment=tuple(str(item) for item in data.get("employment", ())),
            location=str(data.get("location", "london_remote")),
            pack=str(data.get("pack", "all")),
        )


@dataclass(frozen=True)
class Step:
    """Where the conversation is, and what to say next."""

    step: str
    draft: Draft
    reply: Reply
    finished: bool = False
    settings: dict | None = None
    source_ids: tuple[str, ...] = ()


def default_draft() -> Draft:
    return Draft(
        families=("software", "data"),
        employment=("graduate_scheme", "internship", "placement", "entry_level_permanent"),
        location="london_remote",
        pack="all",
    )


def _families_reply(draft: Draft) -> Reply:
    rows: list[tuple[tuple[str, str], ...]] = []
    for index in range(0, len(FAMILIES), 2):
        rows.append(
            tuple(
                (f"{'✅' if key in draft.families else '▫️'} {label}", f"f:{key}")
                for key, label in FAMILIES[index : index + 2]
            )
        )
    rows.append((("Done →", "f:next"),))
    return Reply(
        "1/5 · Which kinds of role should we tell you about?\n"
        "Tap to switch each one on or off, then Done.",
        tuple(rows),
    )


def _employment_reply(draft: Draft) -> Reply:
    rows = tuple(
        (("✅ " if key in draft.employment else "▫️ ") + label, f"e:{key}")
        for key, label in EMPLOYMENT
    )
    return Reply(
        "2/5 · Which of these count as early-career for you?",
        (rows, (("Done →", "e:next"),)),
    )


def _location_reply(draft: Draft) -> Reply:
    rows = tuple(
        (("✅ " if draft.location == preset.key else "") + preset.label, f"l:{preset.key}")
        for preset in LOCATIONS
    )
    return Reply(
        "3/5 · Where would you work?",
        (rows,),
    )


def _employers_reply(draft: Draft, packs: Sequence[Pack]) -> Reply:
    rows = tuple(
        (("✅ " if draft.pack == pack.key else "") + pack.label, f"p:{pack.key}") for pack in packs
    )
    return Reply(
        "4/5 · Which employers should we watch?\nYou can change this later with /settings.",
        (rows,),
    )


def _summary(draft: Draft, packs: Sequence[Pack]) -> str:
    family_labels = {key: label for key, label in FAMILIES}
    employment_labels = {key: label for key, label in EMPLOYMENT}
    families = ", ".join(family_labels.get(key, key) for key in draft.families) or "none"
    employment = ", ".join(employment_labels.get(key, key) for key in draft.employment) or "none"
    location = LOCATION_BY_KEY.get(draft.location)
    location_text = location.label if location else draft.location
    pack = next((item for item in packs if item.key == draft.pack), None)
    employers = f"{pack.label} ({len(pack.source_ids)})" if pack else "none"
    return (
        f"Roles: {families}\n"
        f"Early career: {employment}\n"
        f"Location: {location_text}\n"
        f"Employers: {employers}"
    )


def _review_reply(draft: Draft, packs: Sequence[Pack]) -> Reply:
    return Reply(
        "5/5 · Does this look right?\n\n"
        f"{_summary(draft, packs)}\n\n"
        "Saving will not alert you about roles that are already open — you can ask for those "
        "with /digest at any time.",
        (
            (("✅ Save and start monitoring", "save"),),
            (("Change roles", "goto:families"), ("Change employers", "goto:employers")),
            (("Cancel", "cancel"),),
        ),
    )


def begin(packs: Sequence[Pack], draft: Draft | None = None) -> Step:
    """Start (or restart) the wizard at the first step."""
    chosen = draft or default_draft()
    return Step(STEP_FAMILIES, chosen, _families_reply(chosen))


def step_reply(step: str, draft: Draft, packs: Sequence[Pack]) -> Reply:
    if step == STEP_FAMILIES:
        return _families_reply(draft)
    if step == STEP_EMPLOYMENT:
        return _employment_reply(draft)
    if step == STEP_LOCATION:
        return _location_reply(draft)
    if step == STEP_EMPLOYERS:
        return _employers_reply(draft, packs)
    return _review_reply(draft, packs)


def _toggle(values: tuple[str, ...], key: str) -> tuple[str, ...]:
    return tuple(item for item in values if item != key) if key in values else (*values, key)


def handle(step: str, draft: Draft, data: str, packs: Sequence[Pack]) -> Step:
    """Apply one button press. Returns the next step, with the reply already built."""
    if data == "cancel":
        return Step(
            step,
            draft,
            Reply("Cancelled. Nothing was changed. Send /start when you want to try again."),
            finished=True,
        )

    if data.startswith("goto:"):
        target = data.split(":", 1)[1]
        return Step(target, draft, step_reply(target, draft, packs))

    if data.startswith("f:"):
        key = data.split(":", 1)[1]
        if key == "next":
            if not draft.families:
                return Step(
                    STEP_FAMILIES,
                    draft,
                    Reply(
                        "Pick at least one kind of role, otherwise nothing can match.\n\n"
                        + _families_reply(draft).text,
                        _families_reply(draft).buttons,
                    ),
                )
            return Step(STEP_EMPLOYMENT, draft, _employment_reply(draft))
        updated = replace(draft, families=_toggle(draft.families, key))
        return Step(STEP_FAMILIES, updated, _families_reply(updated))

    if data.startswith("e:"):
        key = data.split(":", 1)[1]
        if key == "next":
            if not draft.employment:
                reply = _employment_reply(draft)
                return Step(
                    STEP_EMPLOYMENT,
                    draft,
                    Reply(
                        "Pick at least one early-career type, otherwise nothing can match.\n\n"
                        + reply.text,
                        reply.buttons,
                    ),
                )
            return Step(STEP_LOCATION, draft, _location_reply(draft))
        updated = replace(draft, employment=_toggle(draft.employment, key))
        return Step(STEP_EMPLOYMENT, updated, _employment_reply(updated))

    if data.startswith("l:"):
        updated = replace(draft, location=data.split(":", 1)[1])
        return Step(STEP_EMPLOYERS, updated, _employers_reply(updated, packs))

    if data.startswith("p:"):
        updated = replace(draft, pack=data.split(":", 1)[1])
        return Step(STEP_REVIEW, updated, _review_reply(updated, packs))

    if data == "save":
        return _finish(draft, packs)

    # Unknown callback: re-show the current step rather than silently doing nothing.
    return Step(step, draft, step_reply(step, draft, packs))


def _finish(draft: Draft, packs: Sequence[Pack]) -> Step:
    pack = next((item for item in packs if item.key == draft.pack), None)
    source_ids = pack.source_ids if pack else ()
    location = LOCATION_BY_KEY.get(draft.location) or LOCATION_BY_KEY["uk"]
    settings = {
        "role_families": list(draft.families),
        "employment_types": list(draft.employment),
        "locations": location.settings(),
    }
    return Step(
        STEP_REVIEW,
        draft,
        Reply(
            "Saved. I will message you when a matching role appears.\n\n"
            f"{_summary(draft, packs)}\n\n"
            "Useful commands:\n"
            "· /digest — roles that already match right now\n"
            "· /status — is monitoring working?\n"
            "· /pause and /resume\n"
            "· /settings — change any of the above\n"
            "· /delete — remove my data"
        ),
        finished=True,
        settings=settings,
        source_ids=source_ids,
    )


__all__ = [
    "Draft",
    "Reply",
    "STEP_EMPLOYERS",
    "STEP_EMPLOYMENT",
    "STEP_FAMILIES",
    "STEP_LOCATION",
    "STEP_REVIEW",
    "STEPS",
    "Step",
    "begin",
    "default_draft",
    "handle",
    "step_reply",
]
