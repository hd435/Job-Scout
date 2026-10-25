from __future__ import annotations

import pytest

from jobscout.matching import normalize_location


@pytest.mark.parametrize(
    ("raw", "workplace", "reason", "city"),
    [
        ("London, United Kingdom", "hybrid", "london", "London"),
        ("London", None, "london", "London"),
        ("london, uk", None, "london", "London"),
        ("Canary Wharf, London", None, "london", "London"),
        ("Manchester, United Kingdom", "hybrid", "uk_city:manchester", "Manchester"),
        ("Leeds, United Kingdom", "hybrid", "uk_city:leeds", "Leeds"),
        ("Northern Ireland, United Kingdom", None, "uk_country", None),
        ("Remote - United Kingdom", "remote", "uk_remote", None),
        ("UK Remote", "remote", "uk_remote", None),
        ("United Kingdom", "hybrid", "uk_hybrid", None),
        ("London, United Kingdom; New York, United States", None, "london", "London"),
    ],
)
def test_uk_locations_qualify(raw, workplace, reason, city):
    result = normalize_location(raw, workplace)

    assert result.qualifies is True
    assert result.ambiguous is False
    assert result.reason == reason
    assert result.city == city
    assert result.country == "United Kingdom"


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("New York, NY", "non_uk_city:new york"),
        ("Bengaluru, India", "non_uk_city:bengaluru"),
        ("Dublin, Ireland", "non_uk_city:dublin"),
        ("Remote - US", "non_uk_remote:us"),
        ("Toronto, Canada", "non_uk_city:toronto"),
    ],
)
def test_non_uk_locations_do_not_qualify(raw, reason):
    result = normalize_location(raw)

    assert result.qualifies is False
    assert result.ambiguous is False
    assert result.reason == reason


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("", "location_missing"),
        (None, "location_missing"),
        ("Remote", "location_ambiguous:remote_uk_eligibility_unclear"),
        ("Remote-friendly", "location_ambiguous:remote_uk_eligibility_unclear"),
        ("Cambridge, MA", "location_ambiguous:city_country_unclear:cambridge"),
        ("Somewhere", "location_ambiguous:location_unresolved"),
        ("London, United Kingdom; Remote", "london"),
    ],
)
def test_ambiguous_locations_are_never_guessed(raw, reason):
    result = normalize_location(raw)

    if reason == "london":
        assert result.qualifies is True
        return
    assert result.qualifies is False
    assert result.ambiguous is True
    assert result.reason == reason


def test_new_york_is_not_resolved_as_york_uk():
    result = normalize_location("New York, United States")

    assert result.qualifies is False
    assert result.city is None


def test_multi_location_qualifies_when_any_location_is_in_the_uk():
    result = normalize_location("Bengaluru, India; London, United Kingdom")

    assert result.qualifies is True
    assert result.reason == "london"


# UK-employer context (D-065) ----------------------------------------------


@pytest.mark.parametrize(
    ("raw", "reason", "city"),
    [
        ("Manchester", "uk_city:manchester", "Manchester"),
        ("Halifax", "uk_city:halifax", "Halifax"),
        ("Manchester, West Yorkshire", "uk_city:manchester", "Manchester"),
    ],
)
def test_a_uk_employer_resolves_ambiguous_town_names(raw, reason, city):
    result = normalize_location(raw, uk_employer=True)

    assert result.qualifies is True
    assert result.reason == reason
    assert result.city == city
    assert result.ambiguous is False


@pytest.mark.parametrize("raw", ["Manchester", "Halifax"])
def test_the_same_town_names_stay_ambiguous_without_the_uk_employer_context(raw):
    """A board that advertises worldwide keeps the conservative rule."""
    result = normalize_location(raw)

    assert result.qualifies is False
    assert result.ambiguous is True
    assert result.reason.startswith("location_ambiguous:city_country_unclear")


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("Barcelona", "non_uk_city:barcelona"),
        ("Manchester, United States", "location_ambiguous:conflicting_country:manchester"),
        ("Remote", "location_ambiguous:remote_uk_eligibility_unclear"),
    ],
)
def test_the_uk_employer_context_never_makes_a_foreign_location_qualify(raw, reason):
    result = normalize_location(raw, uk_employer=True)

    assert result.qualifies is False
    assert result.reason == reason


def test_the_uk_employer_context_leaves_an_explicit_uk_location_alone():
    result = normalize_location("Remote (UK)", uk_employer=True)

    assert result.qualifies is True
    assert result.reason == "uk_remote"


def test_halifax_is_recognised_as_a_uk_town_name():
    """Regression: 'Halifax' used to be unresolved, so a real role went to review."""
    result = normalize_location("Halifax")

    assert "halifax" in result.reason


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("Stevenage", "uk_city:stevenage"),
        ("Bolton", "uk_city:bolton"),
        ("Solihull", "uk_city:solihull"),
        ("Rotherham", "uk_city:rotherham"),
        ("Stevenage; Bolton", "uk_city:stevenage"),
        ("Derby; Rotherham; Washington; Solihull", "uk_city:derby"),
        ("Barrow-in-Furness", "uk_city:barrow-in-furness"),
        ("Stoke-on-Trent", "uk_city:stoke-on-trent"),
    ],
)
def test_graduate_employer_towns_are_recognised(raw, reason):
    """Multi-site schemes name towns like Stevenage and Solihull.

    They were unresolved, so MBDA's Stevenage graduate software roles were dropped as
    "location_ambiguous" and only the Bristol ones could ever match.
    """
    result = normalize_location(raw)

    assert result.qualifies is True
    assert result.reason == reason


def test_a_town_that_exists_abroad_stays_ambiguous():
    """`Washington` must not become UK just because a UK employer names it."""
    result = normalize_location("Washington")

    assert result.qualifies is False
    assert result.reason == "non_uk_city:washington"
