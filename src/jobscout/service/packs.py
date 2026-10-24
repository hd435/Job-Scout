"""What the bot offers a user to choose from (D-096).

A chat cannot present a hundred toggles, so employers are grouped into packs and locations into
presets. Both are defined once here so the conversation, the review screen and the tests agree,
and so the operator can change what is offered without touching conversation logic.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..config import SourceConfig

#: Role families a user may pick, in the order the bot shows them.
FAMILIES: tuple[tuple[str, str], ...] = (
    ("software", "Software engineering"),
    ("data", "Data / AI / ML"),
    ("quant", "Quantitative / risk"),
    ("finance", "Finance"),
    ("business_analysis", "Business analysis"),
    ("consulting", "Consulting"),
)

#: Employment classes a user may pick.
EMPLOYMENT: tuple[tuple[str, str], ...] = (
    ("graduate_scheme", "Graduate schemes"),
    ("internship", "Internships"),
    ("placement", "Industrial placements"),
    ("apprenticeship", "Apprenticeships"),
    ("entry_level_permanent", "Entry-level permanent"),
)


@dataclass(frozen=True)
class LocationPreset:
    key: str
    label: str
    mode: str
    cities: tuple[str, ...]
    remote_uk: bool
    hybrid_uk: bool

    def settings(self) -> dict:
        return {
            "mode": self.mode,
            "cities": list(self.cities),
            "remote_uk": self.remote_uk,
            "hybrid_uk": self.hybrid_uk,
        }


#: Three presets, in the order the bot shows them.
LOCATIONS: tuple[LocationPreset, ...] = (
    LocationPreset("uk", "Anywhere in the UK", "uk", (), True, True),
    LocationPreset("london_remote", "London, plus UK remote", "pinned", ("London",), True, True),
    LocationPreset("london_only", "London only (no remote)", "pinned", ("London",), False, False),
)

LOCATION_BY_KEY = {preset.key: preset for preset in LOCATIONS}


@dataclass(frozen=True)
class Pack:
    key: str
    label: str
    description: str
    source_ids: tuple[str, ...]


#: Packs are built from the sources the operator actually offers, so a pack never names a
#: source the service is not allowed to scan (D-094).
PACK_SPECS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "researched",
        "Graduate schemes and big employers",
        "The six employers researched for the MVP: Lloyds Graduate, Barclays, Wise,"
        " Monzo, Palantir and Arm.",
        ("lloyds_graduate", "barclays", "wise", "monzo", "palantir", "arm"),
    ),
    (
        "tech",
        "Tech and startups",
        "Software and data teams that post early-career roles: Stripe, Deliveroo, Graphcore,"
        " Anthropic, Datadog, Elastic, GitLab, GoCardless.",
        (
            "stripe",
            "deliveroo",
            "graphcore",
            "anthropic",
            "datadog",
            "elastic",
            "gitlab",
            "gocardless",
        ),
    ),
)


def available_packs(sources: Sequence[SourceConfig]) -> tuple[Pack, ...]:
    """The packs whose employers are all offered, plus an 'everything' pack."""
    offered = {source.id for source in sources}
    packs: list[Pack] = []
    for key, label, description, source_ids in PACK_SPECS:
        present = tuple(source_id for source_id in source_ids if source_id in offered)
        if present and len(present) == len(source_ids):
            packs.append(Pack(key=key, label=label, description=description, source_ids=present))
    every = tuple(sorted(offered))
    if every:
        packs.append(
            Pack(
                key="all",
                label="Every employer on offer",
                description=(
                    f"All {len(every)} employers this service monitors."
                    " The most useful choice if unsure."
                ),
                source_ids=every,
            )
        )
    return tuple(packs)


def pack_sources(packs: Sequence[Pack], key: str) -> tuple[str, ...]:
    for pack in packs:
        if pack.key == key:
            return pack.source_ids
    return ()


__all__ = [
    "EMPLOYMENT",
    "FAMILIES",
    "LOCATIONS",
    "LOCATION_BY_KEY",
    "LocationPreset",
    "Pack",
    "available_packs",
    "pack_sources",
]
