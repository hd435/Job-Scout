"""The chat's employer packs must stay honest as the catalogue grows.

Packs are the only way a user narrows employers in a 4096-character message (D-096), and a pack
that names an employer the catalogue does not have simply never appears — a silent hole. These
tests fail instead, so adding or renaming an employer is a deliberate act.
"""

from __future__ import annotations

from jobscout.config import bundled_catalogue
from jobscout.service.packs import FAMILIES, LOCATIONS, PACK_SPECS, available_packs


def test_every_pack_names_only_shipped_employers() -> None:
    catalogue = {source.id for source in bundled_catalogue()}

    unknown = {
        (key, source_id)
        for key, _label, _description, source_ids in PACK_SPECS
        for source_id in source_ids
        if source_id not in catalogue
    }

    assert not unknown, f"packs name employers that are not in the catalogue: {sorted(unknown)}"


def test_pack_keys_are_unique() -> None:
    keys = [key for key, *_rest in PACK_SPECS]

    assert len(keys) == len(set(keys))


def test_the_catalogue_is_large_enough_for_the_packs_to_mean_something() -> None:
    catalogue = bundled_catalogue()

    packs = available_packs(catalogue)

    assert len(catalogue) >= 50
    assert len(packs) >= 4, [pack.key for pack in packs]
    assert packs[-1].key == "all"
    assert len(packs[-1].source_ids) == len(catalogue)


def test_the_wizard_offers_every_role_family_and_location_preset() -> None:
    assert [key for key, _label in FAMILIES] == [
        "software",
        "data",
        "quant",
        "finance",
        "business_analysis",
        "consulting",
    ]
    assert [preset.key for preset in LOCATIONS] == ["uk", "london_remote", "london_only"]
