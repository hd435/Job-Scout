from __future__ import annotations

import pytest

from jobscout.matching import RULES_VERSION, match_job
from jobscout.models import Job, MatchOutcome
from jobscout.sources.base import build_job


def make_job(
    title: str,
    *,
    location: str = "London, United Kingdom",
    description: str | None = "A role description.",
    workplace_type: str | None = "hybrid",
) -> Job:
    return build_job(
        source_id="palantir",
        source_job_id="job-1",
        employer="Palantir Technologies",
        title=title,
        location_raw=location,
        url="https://jobs.lever.co/palantir/job-1",
        description=description,
        workplace_type=workplace_type,
    )


@pytest.mark.parametrize(
    "title",
    [
        "Graduate Software Engineer",
        "Software Engineer Intern",
        "Machine Learning Engineer, Internship",
        "Entry Level Data Analyst",
        "Junior Data Analyst",
        "Industrial Placement - Data Science",
        "Software Engineer, New Grad",
        "Graduate Data Engineer",
        "Business Analyst - Graduate Programme",
        "Quantitative Researcher - New Grad",
    ],
)
def test_early_career_roles_in_target_families_match(title):
    decision = match_job(make_job(title))

    assert decision.decision is MatchOutcome.MATCH
    assert decision.rules_version == RULES_VERSION
    assert any(reason.startswith("early_career:") for reason in decision.reasons)


@pytest.mark.parametrize(
    "title",
    [
        "Senior Software Engineer",
        "Snr Data Engineer",
        "Principal Data Scientist",
        "Staff Software Engineer",
        "Head of Data",
        "Director of Engineering",
        "VP Engineering",
        "Vice President, Finance",
        "Engineering Manager",
    ],
)
def test_seniority_exclusions_do_not_match(title):
    decision = match_job(make_job(title))

    assert decision.decision is MatchOutcome.NO_MATCH
    assert any(reason.startswith("exclude:seniority:") for reason in decision.reasons)


def test_manager_is_allowed_only_inside_a_graduate_programme():
    decision = match_job(make_job("Software Engineering Graduate Programme Manager"))

    assert decision.decision is MatchOutcome.MATCH
    assert any("graduate_programme_override" in reason for reason in decision.reasons)

    plain_manager = match_job(make_job("Software Engineering Manager"))
    assert plain_manager.decision is MatchOutcome.NO_MATCH


@pytest.mark.parametrize(
    "title",
    [
        "Account Executive",
        "Business Development Representative",
        "Marketing Coordinator",
        "Human Resources Advisor",
        "Legal Counsel",
        "Audit Associate",
        "Product Designer",
    ],
)
def test_unrelated_families_are_excluded(title):
    decision = match_job(make_job(title))

    assert decision.decision is MatchOutcome.NO_MATCH
    assert any(reason.startswith("exclude:family:") for reason in decision.reasons)


def test_conflicting_family_signals_are_flagged_for_review():
    decision = match_job(make_job("Marketing Data Analyst"))

    assert decision.decision is MatchOutcome.NEEDS_REVIEW
    assert any(reason.startswith("review:family_conflict:") for reason in decision.reasons)


def test_title_without_family_keyword_does_not_match():
    decision = match_job(make_job("Associate, Strategy"))

    assert decision.decision is MatchOutcome.NO_MATCH
    assert "exclude:family:none" in decision.reasons


def test_unlabelled_seniority_is_flagged_for_review():
    decision = match_job(make_job("Software Engineer"))

    assert decision.decision is MatchOutcome.NEEDS_REVIEW
    assert "review:seniority_unclear" in decision.reasons


def test_associate_without_early_career_signal_is_flagged_for_review():
    decision = match_job(make_job("Associate Software Engineer"))

    assert decision.decision is MatchOutcome.NEEDS_REVIEW
    assert "review:seniority_unclear:associate" in decision.reasons


def test_description_only_resolves_explicit_early_career_signals():
    decision = match_job(
        make_job(
            "Software Engineer",
            description="This role sits inside our graduate programme for final year students.",
        )
    )

    assert decision.decision is MatchOutcome.MATCH
    assert any(reason.startswith("early_career:description:") for reason in decision.reasons)


def test_promotional_description_does_not_widen_the_match():
    decision = match_job(
        make_job(
            "Software Engineer",
            description="Join a world class engineering team building amazing products.",
        )
    )

    assert decision.decision is MatchOutcome.NEEDS_REVIEW


def test_phd_requirement_is_excluded_unless_explicitly_early_career():
    assert match_job(make_job("Research Scientist (PhD)")).decision is MatchOutcome.NO_MATCH
    assert match_job(make_job("Research Scientist (PhD) - New Grad")).decision is MatchOutcome.MATCH


def test_non_uk_location_never_matches_even_for_early_career_roles():
    decision = match_job(make_job("Graduate Software Engineer", location="Bengaluru, India"))

    assert decision.decision is MatchOutcome.NO_MATCH
    assert any(reason.startswith("exclude:location:") for reason in decision.reasons)


def test_unresolved_location_is_flagged_for_review():
    decision = match_job(make_job("Graduate Software Engineer", location=""))

    assert decision.decision is MatchOutcome.NEEDS_REVIEW
    assert "review:location:location_missing" in decision.reasons


def test_uk_remote_and_hybrid_locations_match():
    assert (
        match_job(
            make_job("Graduate Software Engineer", location="Remote - United Kingdom")
        ).decision
        is MatchOutcome.MATCH
    )
    assert (
        match_job(make_job("Junior Data Analyst", location="Manchester, United Kingdom")).decision
        is MatchOutcome.MATCH
    )


def test_sponsorship_is_recorded_but_does_not_filter():
    decision = match_job(
        make_job(
            "Graduate Data Engineer",
            description="Visa sponsorship available for this role.",
        )
    )

    assert decision.decision is MatchOutcome.MATCH
    assert any(reason.startswith("note:sponsorship_mentioned") for reason in decision.reasons)


def test_matching_is_deterministic():
    job = make_job("Graduate Software Engineer")

    assert match_job(job) == match_job(job)


# ---------------------------------------------------------------------------
# D-108: engineering, science/R&D and supply chain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Graduate Mechanical Engineer",
        "Electronics Engineer - Graduate Programme 2027",
        "Engineering and Technology Graduate Programme - UK",
        "Manufacturing Engineer - Undergraduate Placement 2027",
        "Graduate Civil Engineer",
        "Junior Mechanical Engineer",
    ],
)
def test_engineering_titles_match_the_engineering_family(title: str) -> None:
    decision = match_job(make_job(title))

    assert decision.decision is MatchOutcome.MATCH
    assert "family:engineering" in decision.reasons


@pytest.mark.parametrize(
    "title",
    [
        "Graduate Research Scientist",
        "Analytical Chemist - Graduate",
        "Graduate Clinical Research Associate",
        "Formulation Science Graduate",
        "Materials Scientist (Graduate)",
    ],
)
def test_science_titles_match_the_science_family(title: str) -> None:
    decision = match_job(make_job(title))

    assert decision.decision is MatchOutcome.MATCH
    assert "family:science" in decision.reasons


@pytest.mark.parametrize(
    "title",
    [
        "Graduate Supply Chain Analyst",
        "Procurement - Undergraduate Placement 2027",
        "Graduate Logistics Planner",
        "Demand Planning Graduate",
    ],
)
def test_supply_chain_titles_match_the_supply_chain_family(title: str) -> None:
    decision = match_job(make_job(title))

    assert decision.decision is MatchOutcome.MATCH
    assert "family:supply_chain" in decision.reasons


def test_people_science_is_not_a_science_family_role() -> None:
    """Measured over-match: a bare `science` stem caught HR "People Science" schemes.

    The stem list now names the sciences rather than the word, so a people-analytics scheme
    stays out of a family that is meant to mean R&D.
    """
    decision = match_job(make_job("People Science Graduate Scheme"))

    assert "family:science" not in decision.reasons


def test_a_commercial_look_alike_is_held_for_review_not_alerted() -> None:
    decision = match_job(make_job("Graduate Sales Engineering Programme"))

    assert decision.decision is MatchOutcome.NEEDS_REVIEW
    assert any(reason.startswith("review:family_conflict") for reason in decision.reasons)


def test_software_roles_still_match_software() -> None:
    """Overlap between families must not disturb the existing decision."""
    decision = match_job(make_job("Graduate Software Engineer"))

    assert decision.decision is MatchOutcome.MATCH
    assert "family:software" in decision.reasons


def test_a_user_who_did_not_select_the_new_families_gets_nothing_new() -> None:
    """No silent widening: a stored scope from before D-108 must keep its behaviour."""
    from jobscout.matching import build_rules

    rules = build_rules(role_families=["software", "data"])

    decision = match_job(make_job("Graduate Mechanical Engineer"), rules)

    assert decision.decision is not MatchOutcome.MATCH


def test_operational_warehouse_titles_are_not_supply_chain_roles() -> None:
    """Measured over-match: a `warehouse` stem alerted on "Trainee Warehouse Operative".

    Supply chain means planning and procurement, not shop-floor operations, so the stem list
    names the professional functions.
    """
    decision = match_job(make_job("Trainee Warehouse Operative"))

    assert "family:supply_chain" not in decision.reasons
    assert decision.decision is MatchOutcome.NO_MATCH
