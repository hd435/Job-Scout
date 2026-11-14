"""Workable's public job search as a discovery engine.

The search returns vacancies, not employers, and it does not reveal which Workable account carries
a posting — so the handle worth keeping is the employer's own website, which resolves to whatever
ATS it actually uses. What must be exactly right here is the aggregation: one row per employer,
recognised however its name is spelled, with postings counted honestly.
"""

from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

import harvest_workable  # noqa: E402


def job(
    title: str,
    company: str,
    *,
    company_id: str = "c1",
    website: str = "",
    city: str = "",
) -> dict:
    return {
        "id": title,
        "title": title,
        "url": "https://jobs.workable.com/view/abc/anything",
        "company": {"id": company_id, "title": company, "website": website},
        "location": {"city": city, "countryName": "United Kingdom"},
    }


def test_summarise_keeps_the_employers_own_website() -> None:
    """The website is the handle: resolving a board from it finds any ATS, not just Workable."""
    rows = harvest_workable.summarise(
        [job("Graduate Surveyor", "Acme", website="https://www.acme.com")], known=set()
    )

    assert rows[0]["website"] == "https://www.acme.com"


def test_summarise_aggregates_postings_by_employer() -> None:
    rows = harvest_workable.summarise(
        [
            job("Graduate Building Surveyor", "Acme", website="https://acme.com"),
            job("Graduate Quantity Surveyor", "Acme", website="https://acme.com"),
            job("Trainee Engineer", "Other Ltd", company_id="c2", website="https://other.com"),
        ],
        known=set(),
    )

    assert [row["employer"] for row in rows] == ["Acme", "Other Ltd"]
    assert rows[0]["postings"] == 2
    assert rows[0]["example"] == "Graduate Building Surveyor"
    assert rows[1]["postings"] == 1


def test_summarise_skips_an_employer_already_judged() -> None:
    """Re-researching a known employer is exactly what the registry exists to stop."""
    rows = harvest_workable.summarise(
        [job("Graduate Engineer", "Workman LLP")], known={"workman_llp"}
    )

    assert rows == []


def test_summarise_recognises_a_name_matched_on_its_id_form() -> None:
    rows = harvest_workable.summarise(
        [job("Graduate Engineer", "The Focusrite Group")], known={"focusrite_group"}
    )

    assert rows == []


def test_summarise_does_not_count_the_same_title_twice_for_one_employer() -> None:
    """Two cities can publish the same role: one posting's worth of evidence, not two."""
    rows = harvest_workable.summarise(
        [
            job("Graduate Engineer", "Acme", city="London"),
            job("Graduate Engineer", "Acme", city="Leeds"),
        ],
        known=set(),
    )

    assert rows[0]["postings"] == 1
    assert rows[0]["cities"] == ["Leeds", "London"]


def test_summarise_ignores_a_record_with_no_company_identity() -> None:
    rows = harvest_workable.summarise(
        [{"id": "x", "title": "Graduate Engineer", "company": {}, "url": ""}], known=set()
    )

    assert rows == []
