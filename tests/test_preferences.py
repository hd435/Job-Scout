"""The preferences model: what a user may choose, and what those choices do.

The first section is the contract that matters most: a configuration that names only the
shipped defaults must behave exactly like no configuration at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobscout.config import ConfigError, load_config
from jobscout.matching import (
    DEFAULT_EMPLOYMENT_TYPES,
    DEFAULT_LOCATION_PREFERENCES,
    DEFAULT_RULES,
    DESCRIPTION_EARLY_CAREER_SIGNALS,
    EARLY_CAREER_STEMS,
    EARLY_CAREER_WORDS,
    RULES_VERSION,
    UNKNOWN_EMPLOYMENT_TYPE,
    LocationPreferences,
    build_rules,
    classify_employment_type,
    match_job,
    normalize_text,
    preferences_fingerprint,
    split_pinned_cities,
)
from jobscout.models import Job, MatchOutcome
from jobscout.sources.base import build_job

ALL_FAMILIES = ["data", "software", "quant", "finance", "consulting", "business_analysis"]


def make_job(
    title: str,
    *,
    location: str = "London, United Kingdom",
    description: str = "x",
    workplace_type: str | None = None,
) -> Job:
    return build_job(
        source_id="monzo",
        source_job_id="job-1",
        employer="Monzo",
        title=title,
        location_raw=location,
        url="https://job-boards.greenhouse.io/monzo/jobs/1",
        description=description,
        workplace_type=workplace_type,
    )


def explicit_defaults(**overrides) -> object:
    defaults = dict(
        role_families=ALL_FAMILIES,
        seniority_exclusions=list(DEFAULT_RULES.seniority_exclusions),
        employment_types=list(DEFAULT_EMPLOYMENT_TYPES),
        locations=LocationPreferences(),
    )
    defaults.update(overrides)
    return build_rules(**defaults)


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


# defaults reproduce today's behaviour -------------------------------------

TITLES = [
    ("Graduate Software Engineer", "London, United Kingdom", "match"),
    ("Software Engineer Graduate Scheme (Leeds)", "Leeds", "match"),
    ("Associate Data Scientist - Intern", "London", "match"),
    ("Senior Data Scientist", "London", "no_match"),
    ("Data Analyst", "Manchester", "needs_review"),
    ("Graduate Data Analyst", "Bengaluru, India", "no_match"),
    ("Account Executive", "London", "no_match"),
    ("Business Analyst", "United Kingdom", "needs_review"),
    ("Graduate Finance Analyst", "Remote, United Kingdom", "match"),
    # A placement year is not recognised by the approved early-career keywords yet, so
    # it lands in review rather than matching. Left as-is: widening the keyword list
    # is tuning, not configuration.
    ("Year in Industry - Data Engineer", "Bristol", "needs_review"),
]


@pytest.mark.parametrize(("title", "location", "expected"), TITLES)
def test_naming_the_defaults_changes_nothing(title: str, location: str, expected: str) -> None:
    job = make_job(title, location=location)

    baseline = match_job(job, DEFAULT_RULES)
    configured = match_job(job, explicit_defaults())

    assert baseline.decision.value == expected
    assert (configured.decision, configured.reasons) == (baseline.decision, baseline.reasons)
    assert configured.rules_version == RULES_VERSION


def test_a_default_preference_set_keeps_the_built_in_identifier() -> None:
    assert explicit_defaults().version == RULES_VERSION
    assert explicit_defaults().has_overrides is False
    assert preferences_fingerprint(DEFAULT_RULES) == preferences_fingerprint(explicit_defaults())


def test_every_early_career_signal_maps_to_an_employment_type() -> None:
    """`unknown` must never be reachable for a role that would otherwise alert."""
    for signal in (*EARLY_CAREER_STEMS, *EARLY_CAREER_WORDS):
        classified = classify_employment_type(signal, "")
        assert classified != UNKNOWN_EMPLOYMENT_TYPE, signal
    for signal in DESCRIPTION_EARLY_CAREER_SIGNALS:
        assert classify_employment_type("Data Analyst", signal) != UNKNOWN_EMPLOYMENT_TYPE


# role families ------------------------------------------------------------


def test_a_narrowed_family_list_excludes_the_others() -> None:
    rules = build_rules(role_families=["data"])

    decision = match_job(make_job("Graduate Software Engineer"), rules)

    assert decision.decision is MatchOutcome.NO_MATCH
    assert "exclude:family:not_selected:software" in decision.reasons


def test_a_role_matching_one_allowed_family_still_matches() -> None:
    rules = build_rules(role_families=["data"])

    decision = match_job(make_job("Graduate Data Engineer"), rules)

    assert decision.decision is MatchOutcome.MATCH
    assert "family:data" in decision.reasons


def test_an_empty_family_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="role_families cannot be empty"):
        build_rules(role_families=[])


def test_an_unknown_family_is_rejected_naming_the_value() -> None:
    with pytest.raises(ValueError, match="unknown role family 'legals'"):
        build_rules(role_families=["data", "legals"])


def test_families_are_reported_in_a_stable_order_for_the_fingerprint() -> None:
    first = build_rules(role_families=["data", "software"])
    second = build_rules(role_families=["data", "software"])

    assert preferences_fingerprint(first) == preferences_fingerprint(second)


# seniority ----------------------------------------------------------------


def test_seniority_exclusions_are_configurable() -> None:
    rules = build_rules(seniority_exclusions=list(DEFAULT_RULES.seniority_exclusions) + ["iii"])

    decision = match_job(make_job("Graduate Software Engineer III"), rules)

    assert decision.decision is MatchOutcome.NO_MATCH
    assert "exclude:seniority:iii" in decision.reasons


def test_seniority_can_be_relaxed_to_nothing() -> None:
    rules = build_rules(seniority_exclusions=[])

    # Omitting the key keeps the approved list; asking for an empty one relaxes it, so a
    # user who does want senior roles can say so instead of editing code.
    assert rules.seniority_exclusions == ()
    assert match_job(make_job("Senior Data Scientist"), rules).decision is not (
        MatchOutcome.NO_MATCH
    )
    assert match_job(make_job("Senior Data Scientist")).decision is MatchOutcome.NO_MATCH


# employment types ---------------------------------------------------------


def test_a_disabled_early_career_type_is_excluded() -> None:
    rules = build_rules(employment_types=["graduate_scheme", "entry_level_permanent"])

    decision = match_job(make_job("Data Science Intern"), rules)

    assert decision.decision is MatchOutcome.NO_MATCH
    assert "exclude:employment_type:internship" in decision.reasons


def test_an_enabled_type_records_the_class_in_the_decision() -> None:
    decision = match_job(make_job("Data Science Intern"))

    assert decision.decision is MatchOutcome.MATCH
    assert "employment_type:internship" in decision.reasons


def test_experienced_hire_is_reviewed_rather_than_alerted() -> None:
    job = make_job(
        "Data Scientist",
        description="Recent graduate welcome. You will need 3+ years of experience in Python.",
    )

    decision = match_job(job)

    assert decision.decision is MatchOutcome.NEEDS_REVIEW
    assert "review:employment_type:experienced_hire" in decision.reasons


def test_experienced_hire_can_be_enabled_explicitly() -> None:
    rules = build_rules(employment_types=[*DEFAULT_EMPLOYMENT_TYPES, "experienced_hire"])
    job = make_job(
        "Data Scientist",
        description="Recent graduate welcome. You will need 3+ years of experience in Python.",
    )

    decision = match_job(job, rules)

    assert decision.decision is MatchOutcome.MATCH
    assert "employment_type:experienced_hire" in decision.reasons


def test_unknown_cannot_be_chosen() -> None:
    with pytest.raises(ValueError, match="cannot include 'unknown'"):
        build_rules(employment_types=["graduate_scheme", "unknown"])


def test_an_empty_type_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="employment_types cannot be empty"):
        build_rules(employment_types=[])


def test_the_employment_classifier_prefers_the_narrower_label() -> None:
    assert classify_employment_type("Graduate Data Science Internship") == "internship"
    assert classify_employment_type("Graduate Apprentice - Data") == "apprenticeship"
    assert classify_employment_type("Industrial Placement - Software") == "placement"
    assert classify_employment_type("Graduate Software Engineer") == "graduate_scheme"
    assert classify_employment_type("Junior Business Analyst") == "entry_level_permanent"
    assert classify_employment_type("Data Scientist", "minimum of 5 years") == "experienced_hire"
    assert classify_employment_type("Data Scientist", "our five-year plan") == (
        UNKNOWN_EMPLOYMENT_TYPE
    )


# locations ----------------------------------------------------------------


def test_pinned_mode_keeps_only_the_listed_cities() -> None:
    rules = build_rules(
        locations=LocationPreferences(mode="pinned", cities=["London"]),
    )

    london = match_job(make_job("Graduate Data Analyst", location="London"), rules)
    leeds = match_job(make_job("Graduate Data Analyst", location="Leeds"), rules)

    assert london.decision is MatchOutcome.MATCH
    assert leeds.decision is MatchOutcome.NO_MATCH
    assert "exclude:location:not_pinned" in leeds.reasons


def test_pinned_mode_keeps_uk_remote_and_can_switch_it_off() -> None:
    job = make_job("Graduate Data Analyst", location="Remote, United Kingdom")
    keeping = build_rules(locations=LocationPreferences(mode="pinned", cities=["London"]))
    dropping = build_rules(
        locations=LocationPreferences(mode="pinned", cities=["London"], remote_uk=False)
    )

    assert match_job(job, keeping).decision is MatchOutcome.MATCH
    refused = match_job(job, dropping)
    assert refused.decision is MatchOutcome.NO_MATCH
    assert "exclude:location:remote_not_selected" in refused.reasons


def test_uk_hybrid_can_be_switched_off() -> None:
    job = make_job("Graduate Data Analyst", location="United Kingdom", workplace_type="hybrid")
    rules = build_rules(locations=LocationPreferences(mode="uk", hybrid_uk=False))

    decision = match_job(job, rules)

    assert decision.decision is MatchOutcome.NO_MATCH
    assert "exclude:location:hybrid_not_selected" in decision.reasons


def test_uk_plus_pinned_admits_a_pinned_city_outside_the_uk() -> None:
    rules = build_rules(locations=LocationPreferences(mode="uk_plus_pinned", cities=["Dublin"]))

    dublin = match_job(make_job("Graduate Data Analyst", location="Dublin, Ireland"), rules)
    toronto = match_job(make_job("Graduate Data Analyst", location="Toronto, Canada"), rules)

    assert dublin.decision is MatchOutcome.MATCH
    assert "location:pinned_city" in dublin.reasons
    assert toronto.decision is MatchOutcome.NO_MATCH


def test_an_unknown_pinned_city_matches_on_the_raw_location() -> None:
    rules = build_rules(locations=LocationPreferences(mode="pinned", cities=["Bournemouth"]))

    decision = match_job(make_job("Graduate Data Analyst", location="Bournemouth"), rules)

    assert decision.decision is MatchOutcome.MATCH
    assert "location:pinned_city" in decision.reasons


def test_pinning_an_ambiguous_city_does_not_override_the_collision_check() -> None:
    rules = build_rules(locations=LocationPreferences(mode="pinned", cities=["Manchester"]))

    decision = match_job(make_job("Graduate Data Analyst", location="Manchester"), rules)

    assert decision.decision is MatchOutcome.NEEDS_REVIEW
    assert any(
        reason.startswith("review:location:location_ambiguous") for reason in decision.reasons
    )


def test_the_default_location_preferences_are_the_shipped_uk_scope() -> None:
    assert DEFAULT_LOCATION_PREFERENCES.mode == "uk"
    assert DEFAULT_LOCATION_PREFERENCES.cities == ()
    assert DEFAULT_LOCATION_PREFERENCES.remote_uk is True
    assert DEFAULT_LOCATION_PREFERENCES.hybrid_uk is True


def test_split_pinned_cities_separates_resolved_names_from_text() -> None:
    known, patterns = split_pinned_cities(["london", "LONDON", "Bournemouth", ""])

    assert known == ("London",)
    assert patterns == ("bournemouth",)


def test_an_unknown_location_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown location mode 'nearby'"):
        build_rules(locations=LocationPreferences(mode="nearby"))


# configuration parsing ----------------------------------------------------


def test_preferences_load_from_toml(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
[preferences]
role_families = ["data", "software"]
employment_types = ["graduate_scheme"]
title_exclusions = ["contract"]

[preferences.locations]
mode = "pinned"
cities = ["London"]
hybrid_uk = false
""",
    )

    app = load_config(path)

    assert app.matching.alerting_families == ("data", "software")
    assert app.matching.alerting_employment_types == ("graduate_scheme",)
    assert app.matching.extra_exclude_terms == ("contract",)
    assert app.matching.locations == LocationPreferences(
        mode="pinned", cities=("London",), remote_uk=True, hybrid_uk=False
    )
    assert app.matching.version.startswith(f"{RULES_VERSION}+")


def test_an_unknown_preference_key_is_rejected_naming_it(tmp_path: Path) -> None:
    path = write_config(tmp_path, '[preferences]\nrole_familys = ["data"]\n')

    with pytest.raises(ConfigError, match="unknown \\[preferences\\] key\\(s\\): role_familys"):
        load_config(path)


def test_an_unknown_location_key_is_rejected_naming_it(tmp_path: Path) -> None:
    path = write_config(tmp_path, '[preferences.locations]\ncity = ["London"]\n')

    with pytest.raises(ConfigError, match="unknown \\[preferences.locations\\] key\\(s\\): city"):
        load_config(path)


def test_pinned_mode_without_cities_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path, '[preferences.locations]\nmode = "pinned"\n')

    with pytest.raises(ConfigError, match="needs at least one entry"):
        load_config(path)


def test_an_invalid_mode_names_the_value(tmp_path: Path) -> None:
    path = write_config(tmp_path, '[preferences.locations]\nmode = "london"\n')

    with pytest.raises(ConfigError, match="'london' is not one of pinned, uk, uk_plus_pinned"):
        load_config(path)


def test_a_non_boolean_toggle_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path, '[preferences.locations]\nremote_uk = "yes"\n')

    with pytest.raises(ConfigError, match="remote_uk must be true or false"):
        load_config(path)


def test_a_preference_value_is_validated_through_the_ruleset(tmp_path: Path) -> None:
    path = write_config(tmp_path, '[preferences]\nrole_families = ["data", "legals"]\n')

    with pytest.raises(ConfigError, match="invalid \\[preferences\\] configuration"):
        load_config(path)


def test_an_invalid_preference_type_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path, '[preferences]\nemployment_types = "graduate_scheme"\n')

    with pytest.raises(ConfigError, match="employment_types must be an array of strings"):
        load_config(path)


def test_preferences_and_matching_additions_combine(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
[preferences]
role_families = ["data"]

[preferences.locations]
mode = "pinned"
cities = ["London"]

[matching.extra_family_terms]
data = ["bioinformatics analyst"]
""",
    )

    app = load_config(path)

    assert app.matching.family_stems("data")[-1] == "bioinformatics analyst"
    assert app.matching.locations.mode == "pinned"


def test_the_fingerprint_changes_when_a_preference_changes() -> None:
    first = build_rules(role_families=["data", "software"])
    second = build_rules(role_families=["data"])

    assert preferences_fingerprint(first) != preferences_fingerprint(second)
    assert first.version != second.version


def test_normalize_text_is_used_for_pinned_city_comparison() -> None:
    rules = build_rules(locations=LocationPreferences(mode="pinned", cities=["Milton Keynes"]))

    decision = match_job(make_job("Graduate Data Analyst", location="milton keynes"), rules)

    assert decision.decision is MatchOutcome.MATCH
    assert normalize_text("Milton Keynes") == "milton keynes"


def test_a_scan_records_the_preference_fingerprint(store, scan, record) -> None:
    """A stored decision must be traceable to the scope that produced it."""
    rules = build_rules(locations=LocationPreferences(mode="pinned", cities=["London"]))

    scan([record("a", "Graduate Software Engineer")], rules=rules)

    stored = store.get_job("palantir", "a")
    assert stored.match.rules_version == rules.version
    assert rules.version.startswith(f"{RULES_VERSION}+")


def test_a_pinned_scan_makes_excluded_roles_visible_as_exclusions(store, scan, record) -> None:
    rules = build_rules(locations=LocationPreferences(mode="pinned", cities=["London"]))

    scan(
        [record("a", "Graduate Software Engineer", location="Leeds")],
        rules=rules,
    )

    stored = store.get_job("palantir", "a")
    assert stored.match.decision is MatchOutcome.NO_MATCH
    assert "exclude:location:not_pinned" in stored.match.reasons
