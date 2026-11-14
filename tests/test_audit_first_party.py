"""The first-party detector.

A board run by a recruitment agency is a legitimate ATS account carrying real vacancies, and it is
still out of scope (D-114): an alert would name the agency, not the employer. The detector is
deliberately blunt, so what matters is that it fires on recruiter language and stays quiet on
employers who merely describe their own clients.
"""

from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

import audit_first_party  # noqa: E402


def test_a_recruitment_business_is_flagged() -> None:
    """The wording is La Fosse's own, which is what the batch-14 audit had to catch."""
    description = (
        "La Fosse is an international talent solutions business across technology, digital and"
        " change, leveraging our recruitment, academy, solutions and executive search offerings."
    )

    assert audit_first_party.signs_in("La Fosse", description) != []


def test_an_employer_describing_its_clients_is_not_flagged_for_that_alone() -> None:
    description = "We provide utility services to our clients across the UK, with 5000 employees."

    assert audit_first_party.signs_in("Lanes Group", description) == []


def test_an_employer_that_recruits_for_others_is_flagged() -> None:
    description = "We recruit for the best startups in London on behalf of our clients."

    assert audit_first_party.signs_in("Acme Talent", description) != []


def test_plain_strips_markup_and_collapses_whitespace() -> None:
    assert audit_first_party.plain("<p>Hello&nbsp;<b>world</b></p>") == "Hello world"
