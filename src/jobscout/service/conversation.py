"""The setup conversation, as a pure state machine (D-091, D-096).

Nothing here sends a message or reads a database: given the current step, the draft answers, and
a callback string, it returns the next step and the exact reply to show. That makes the whole
wizard testable without Telegram, which matters because the conversation *is* the product for a
hosted user.

Five steps: role families → employment types → location → employers → review.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from .packs import EMPLOYMENT, FAMILIES, LOCATION_BY_KEY, LOCATIONS, Pack, matching_pack

STEP_FAMILIES = "families"
STEP_EMPLOYMENT = "employment"
STEP_LOCATION = "location"
STEP_EMPLOYERS = "employers"
STEP_REVIEW = "review"

STEPS = (STEP_FAMILIES, STEP_EMPLOYMENT, STEP_LOCATION, STEP_EMPLOYERS, STEP_REVIEW)

#: Draft markers for "the saved value cannot be expressed as a preset, so keep it as it is".
#: They exist so reopening /settings can never quietly narrow a scope it cannot display.
KEEP_PACK = "__keep__"
KEEP_LOCATION = "__keep__"
KEEP_PACK_ROW = f"p:{KEEP_PACK}"
KEEP_LOCATION_ROW = f"l:{KEEP_LOCATION}"

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
    #: Employer scope to keep verbatim. Only used when `pack` is KEEP_PACK.
    sources: tuple[str, ...] = ()
    #: Location settings to keep verbatim. Only used when `location` is KEEP_LOCATION.
    location_custom: dict = field(default_factory=dict)

    def as_json(self) -> dict:
        return {
            "families": list(self.families),
            "employment": list(self.employment),
            "location": self.location,
            "pack": self.pack,
            "sources": list(self.sources),
            "location_custom": self.location_custom,
        }

    @classmethod
    def from_json(cls, data: dict) -> Draft:
        custom = data.get("location_custom")
        return cls(
            families=tuple(str(item) for item in data.get("families", ())),
            employment=tuple(str(item) for item in data.get("employment", ())),
            location=str(data.get("location", "london_remote")),
            pack=str(data.get("pack", "all")),
            sources=tuple(str(item) for item in data.get("sources", ())),
            location_custom=dict(custom) if isinstance(custom, dict) else {},
        )


def draft_from_state(
    settings: Mapping[str, object] | None,
    subscriptions: Sequence[str],
    packs: Sequence[Pack],
) -> Draft:
    """Rebuild the wizard's draft from what was actually saved, without guessing.

    The saved preference schema (`role_families`, `employment_types`, `locations`) is the CLI's
    configuration schema, not the draft's shape — mapping it here is what makes /settings show
    the user's real answers instead of defaults. A saved employer set that matches no current
    pack, or a saved location that matches no preset, is carried through verbatim.
    """
    saved = settings or {}
    families = tuple(str(item) for item in saved.get("role_families", ()) or ())
    employment = tuple(str(item) for item in saved.get("employment_types", ()) or ())

    stored_location = saved.get("locations")
    location_key = KEEP_LOCATION
    location_custom: dict = {}
    if isinstance(stored_location, Mapping):
        candidate = dict(stored_location)
        for preset in LOCATIONS:
            if preset.settings() == candidate:
                location_key = preset.key
                break
        else:
            location_custom = candidate

    chosen = tuple(str(item) for item in subscriptions)
    pack = matching_pack(packs, chosen)
    if pack is not None:
        return Draft(
            families=families,
            employment=employment,
            location=location_key,
            pack=pack.key,
            location_custom=location_custom,
        )
    if chosen:
        return Draft(
            families=families,
            employment=employment,
            location=location_key,
            pack=KEEP_PACK,
            sources=chosen,
            location_custom=location_custom,
        )
    return Draft(
        families=families,
        employment=employment,
        location=location_key,
        pack="all",
        location_custom=location_custom,
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
    # One button per row: "✅ Business analysis" plus a second long label does not fit a phone
    # keyboard, and Telegram truncates rather than wrapping.
    rows = tuple(
        ((f"{'✅' if key in draft.families else '▫️'} {label}", f"f:{key}"),)
        for key, label in FAMILIES
    )
    return Reply(
        "1/5 · Which kinds of role should we tell you about?\n"
        "Tap to switch each one on or off, then Done.",
        (*rows, (("Done →", "f:next"),)),
    )


def _employment_reply(draft: Draft) -> Reply:
    rows = tuple(
        ((("✅ " if key in draft.employment else "▫️ ") + label, f"e:{key}"),)
        for key, label in EMPLOYMENT
    )
    return Reply(
        "2/5 · Which of these count as early-career for you?",
        (*rows, (("Done →", "e:next"),)),
    )


def _location_reply(draft: Draft) -> Reply:
    rows: list[tuple[tuple[str, str], ...]] = []
    if draft.location == KEEP_LOCATION and draft.location_custom:
        # The saved scope matches no preset (hand-edited or older data). Offer to keep it rather
        # than silently replacing it with the nearest preset.
        rows.append((("✅ Keep my current location settings", KEEP_LOCATION_ROW),))
    rows.extend(
        ((("✅ " if draft.location == preset.key else "") + preset.label, f"l:{preset.key}"),)
        for preset in LOCATIONS
    )
    return Reply("3/5 · Where would you work?", tuple(rows))


def _employers_reply(draft: Draft, packs: Sequence[Pack]) -> Reply:
    rows: list[tuple[tuple[str, str], ...]] = []
    if draft.pack == KEEP_PACK:
        count = len(draft.sources)
        noun = "employer" if count == 1 else "employers"
        rows.append(((f"✅ Keep the {count} {noun} I chose", KEEP_PACK_ROW),))
    rows.extend(
        ((("✅ " if draft.pack == pack.key else "") + pack.label, f"p:{pack.key}"),)
        for pack in packs
    )
    return Reply(
        "4/5 · Which employers should we watch?\nYou can change this later with /settings.",
        tuple(rows),
    )


def _summary(draft: Draft, packs: Sequence[Pack]) -> str:
    family_labels = {key: label for key, label in FAMILIES}
    employment_labels = {key: label for key, label in EMPLOYMENT}
    families = ", ".join(family_labels.get(key, key) for key in draft.families) or "none"
    employment = ", ".join(employment_labels.get(key, key) for key in draft.employment) or "none"
    location = LOCATION_BY_KEY.get(draft.location)
    if location is not None:
        location_text = location.label
    elif draft.location_custom:
        location_text = "your current location settings (unchanged)"
    else:
        location_text = draft.location
    if draft.pack == KEEP_PACK:
        count = len(draft.sources)
        employers = f"the {count} employer{'' if count == 1 else 's'} you chose (unchanged)"
    else:
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
        key = data.split(":", 1)[1]
        # Tapping a preset replaces a kept custom location; leaving both set would make the
        # review screen describe the wrong one. The keep row leaves the draft alone.
        updated = (
            draft if data == KEEP_LOCATION_ROW else replace(draft, location=key, location_custom={})
        )
        return Step(STEP_EMPLOYERS, updated, _employers_reply(updated, packs))

    if data.startswith("p:"):
        key = data.split(":", 1)[1]
        updated = draft if data == KEEP_PACK_ROW else replace(draft, pack=key, sources=())
        return Step(STEP_REVIEW, updated, _review_reply(updated, packs))

    if data == "save":
        return _finish(draft, packs)

    # Unknown callback: re-show the current step rather than silently doing nothing.
    return Step(step, draft, step_reply(step, draft, packs))


def _finish(draft: Draft, packs: Sequence[Pack]) -> Step:
    if draft.pack == KEEP_PACK:
        source_ids = draft.sources
    else:
        pack = next((item for item in packs if item.key == draft.pack), None)
        source_ids = pack.source_ids if pack else ()
    if draft.location == KEEP_LOCATION and draft.location_custom:
        location_settings = dict(draft.location_custom)
    else:
        preset = LOCATION_BY_KEY.get(draft.location) or LOCATION_BY_KEY["uk"]
        location_settings = preset.settings()
    settings = {
        "role_families": list(draft.families),
        "employment_types": list(draft.employment),
        "locations": location_settings,
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
