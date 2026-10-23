from __future__ import annotations

import pytest

from jobscout.matching import DEFAULT_RULES, RULES_VERSION, build_rules, match_job
from jobscout.models import Job, MatchOutcome
from jobscout.sources.base import build_job
from jobscout.storage import Storage


def make_job(
    title: str, *, location: str = "London, United Kingdom", description: str = "x"
) -> Job:
    return build_job(
        source_id="palantir",
        source_job_id="job-1",
        employer="Palantir Technologies",
        title=title,
        location_raw=location,
        url="https://jobs.lever.co/palantir/job-1",
        description=description,
    )


# construction and validation ----------------------------------------------


def test_no_overrides_returns_the_built_in_rules() -> None:
    assert build_rules() is DEFAULT_RULES
    assert build_rules(extra_family_terms={}, extra_exclude_terms=[]) is DEFAULT_RULES
    assert DEFAULT_RULES.has_overrides is False
    assert DEFAULT_RULES.version == RULES_VERSION


def test_overrides_are_normalised_and_deduplicated() -> None:
    rules = build_rules(
        extra_family_terms={"data": ["  Bioinformatics   Analyst ", "bioinformatics analyst"]}
    )

    assert rules.extra_family_terms["data"] == ("bioinformatics analyst",)
    assert rules.has_overrides is True


def test_unknown_family_is_rejected() -> None:
    with pytest.raises(ValueError) as exc:
        build_rules(extra_family_terms={"astrology": ["horoscope analyst"]})

    message = str(exc.value)
    assert "unknown role family 'astrology'" in message
    assert "data" in message and "consulting" in message


@pytest.mark.parametrize("bad_terms", ["bioinformatics analyst", 5, {"a": "b"}])
def test_family_terms_must_be_an_array(bad_terms) -> None:
    with pytest.raises(ValueError, match="must be an array"):
        build_rules(extra_family_terms={"data": bad_terms})


def test_empty_or_non_string_terms_are_rejected() -> None:
    with pytest.raises(ValueError, match="empty term"):
        build_rules(extra_family_terms={"data": ["   "]})
    with pytest.raises(ValueError, match="must contain strings"):
        build_rules(extra_exclude_terms=[1])


def test_version_tracks_the_override_content() -> None:
    a = build_rules(extra_family_terms={"data": ["bioinformatics analyst"]})
    b = build_rules(extra_family_terms={"data": ["bioinformatics analyst"]})
    c = build_rules(extra_family_terms={"data": ["clinical analyst"]})

    assert a.version.startswith(f"{RULES_VERSION}+")
    assert a.version == b.version
    assert a.version != c.version
    assert match_job(make_job("Graduate Bioinformatics Analyst"), a).rules_version == a.version


# matching behaviour -------------------------------------------------------


def test_extra_family_term_adds_a_match_inside_an_approved_family() -> None:
    job = make_job("Graduate Bioinformatics Analyst")

    assert match_job(job).decision is MatchOutcome.NO_MATCH

    rules = build_rules(extra_family_terms={"data": ["bioinformatics analyst"]})
    decision = match_job(job, rules)

    assert decision.decision is MatchOutcome.MATCH
    assert "family:data" in decision.reasons
    assert "early_career:graduate" in decision.reasons


def test_extra_family_term_does_not_bypass_the_early_career_requirement() -> None:
    rules = build_rules(extra_family_terms={"data": ["bioinformatics analyst"]})

    decision = match_job(make_job("Bioinformatics Analyst"), rules)

    assert decision.decision is MatchOutcome.NEEDS_REVIEW
    assert "review:seniority_unclear" in decision.reasons


def test_extra_exclusion_removes_a_role_that_would_otherwise_match() -> None:
    job = make_job("Graduate Data Engineer - Contract")

    assert match_job(job).decision is MatchOutcome.MATCH

    rules = build_rules(extra_exclude_terms=["contract"])
    decision = match_job(job, rules)

    assert decision.decision is MatchOutcome.NO_MATCH
    assert "exclude:custom:contract" in decision.reasons


def test_added_terms_cannot_neutralise_an_approved_family_exclusion() -> None:
    rules = build_rules(extra_family_terms={"data": ["sales analyst"]})

    decision = match_job(make_job("Graduate Sales Analyst"), rules)

    assert decision.decision is MatchOutcome.NEEDS_REVIEW
    assert any(reason.startswith("review:family_conflict:sales") for reason in decision.reasons)


def test_added_terms_cannot_neutralise_a_seniority_exclusion() -> None:
    rules = build_rules(extra_family_terms={"data": ["bioinformatics analyst"]})

    decision = match_job(make_job("Senior Bioinformatics Analyst"), rules)

    assert decision.decision is MatchOutcome.NO_MATCH
    assert any(reason.startswith("exclude:seniority:") for reason in decision.reasons)


def test_added_terms_cannot_neutralise_a_location_exclusion() -> None:
    rules = build_rules(extra_family_terms={"data": ["bioinformatics analyst"]})

    decision = match_job(
        make_job("Graduate Bioinformatics Analyst", location="Bengaluru, India"), rules
    )

    assert decision.decision is MatchOutcome.NO_MATCH
    assert any(reason.startswith("exclude:location:") for reason in decision.reasons)


def test_added_terms_respect_the_phd_exclusion() -> None:
    rules = build_rules(extra_family_terms={"data": ["bioinformatics analyst"]})

    assert (
        match_job(make_job("Bioinformatics Analyst (PhD)"), rules).decision is MatchOutcome.NO_MATCH
    )
    assert (
        match_job(make_job("Bioinformatics Analyst (PhD) - New Grad"), rules).decision
        is MatchOutcome.MATCH
    )


# scanner integration ------------------------------------------------------


def test_scan_uses_the_configured_rules(tmp_path, scan, record):
    payload_baseline = [record("a", "Graduate Data Engineer")]
    payload_new = payload_baseline + [record("b", "Graduate Bioinformatics Analyst")]
    rules = build_rules(extra_family_terms={"data": ["bioinformatics analyst"]})

    # Default rules: the new title is not a target family.
    scan(payload_baseline)
    assert scan(payload_new).candidates == ()

    # Overrides: same snapshot on a fresh database inside the same scan session.
    override_store = Storage.open(tmp_path / "override.db")
    try:
        scan(payload_baseline, storage=override_store)
        custom_outcome = scan(payload_new, rules=rules, storage=override_store)
        stored = override_store.get_job("palantir", "b")
    finally:
        override_store.close()

    assert [candidate.title for candidate in custom_outcome.candidates] == [
        "Graduate Bioinformatics Analyst"
    ]
    assert stored.match is not None
    assert stored.match.decision is MatchOutcome.MATCH
    assert stored.match.rules_version == rules.version
