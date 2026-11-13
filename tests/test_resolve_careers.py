"""Resolving a board from an employer's own domain.

The resolver is discovery tooling, so what matters is that its two pure parts are right: the domains
it is willing to try, and the ATS it reads out of a page. Its network loop is exercised with a fake
client, because the interesting behaviour there is *when to stop* — the first fingerprint wins, and
a domain that never answers must not stop the next one being tried.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

import resolve_careers  # noqa: E402

from jobscout.http import HttpError  # noqa: E402


class FakeClient:
    """A client that answers from a dict of URL -> HTML, and fails for anything else."""

    def __init__(self, pages: dict[str, str], missing: set[str] | None = None) -> None:
        self.pages = pages
        self.missing = missing or set()
        self.requested: list[str] = []

    def get_text(self, url: str) -> str:
        self.requested.append(url)
        if url in self.pages:
            return self.pages[url]
        if url in self.missing:
            # A domain that does not exist: the connection never opened, which is what `uncertain`
            # being false means.
            raise HttpError("connection failed", url=url, retryable=True, uncertain=False)
        raise HttpError("request rejected", url=url, status=404)


def test_slug_forms_drops_legal_suffixes_and_the() -> None:
    assert resolve_careers.slug_forms("The Focusrite Group") == ["focusrite"]
    assert resolve_careers.slug_forms("Bae Systems") == ["baesystems", "bae-systems", "bae"]
    assert resolve_careers.slug_forms("Rotork plc") == ["rotork"]


def test_slug_forms_offers_the_first_word_last() -> None:
    """A group's domain often drops the tail of its name: Analog Devices publishes on analog.com."""
    assert resolve_careers.slug_forms("Analog Devices")[-1] == "analog"


def test_slug_forms_prefers_a_flat_domain_without_duplicating_it() -> None:
    assert resolve_careers.slug_forms("Rotork") == ["rotork"]


def test_careers_domains_tries_dot_com_before_a_uk_suffix() -> None:
    """Measured, not assumed: most UK employers with an ATS publish on `.com`."""
    domains = resolve_careers.careers_domains("Rotork")

    assert domains[0] == "https://www.rotork.com"
    assert domains[1] == "https://www.rotork.co.uk"


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (
            '<a href="https://boards.greenhouse.io/acme?gh_src=x">Careers</a>',
            ("greenhouse", "acme"),
        ),
        ('<a href="https://jobs.ashbyhq.com/acme-eng">Jobs</a>', ("ashby", "acme-eng")),
        ('<a href="https://jobs.lever.co/acmelabs">Jobs</a>', ("lever", "acmelabs")),
        ('<a href="https://apply.workable.com/acme/">Jobs</a>', ("workable", "acme")),
        ('<script src="https://acme.recruitee.com/widget.js">', ("recruitee", "acme")),
        ('<a href="https://acme.teamtailor.com/jobs">Jobs</a>', ("teamtailor", "acme")),
        ('<a href="https://jobs.smartrecruiters.com/Acme1">Jobs</a>', ("smartrecruiters", "Acme1")),
    ],
)
def test_fingerprint_reads_the_ats_host(html: str, expected: tuple[str, str]) -> None:
    assert resolve_careers.fingerprint(html) == expected


def test_fingerprint_reads_a_workday_site_as_one_path() -> None:
    html = '<a href="https://acme.wd3.myworkdayjobs.com/en-GB/Acme_Careers">Careers</a>'

    platform, slug = resolve_careers.fingerprint(html)

    assert platform == "workday"
    # probe_boards.py takes a Workday slug as host/tenant/site, so the resolver has to write that.
    assert slug == "acme.wd3.myworkdayjobs.com/acme/Acme_Careers"


def test_fingerprint_ignores_a_page_that_merely_mentions_jobs() -> None:
    assert resolve_careers.fingerprint("<p>We have many jobs. Apply by email.</p>") is None


def test_resolve_stops_at_the_first_ats_it_finds() -> None:
    client = FakeClient(
        {
            "https://www.acme.com": '<a href="https://jobs.lever.co/acme">Careers</a>',
            "https://www.acme.com/careers": '<a href="https://jobs.lever.co/other">Careers</a>',
        }
    )

    record = resolve_careers.resolve(client, "Acme")

    assert (record["platform"], record["slug"]) == ("lever", "acme")
    assert record["source_url"] == "https://www.acme.com"
    assert client.requested == ["https://www.acme.com"]


def test_resolve_tries_the_next_domain_when_one_never_answers() -> None:
    """A dead domain proves nothing about the employer, so the loop must continue past it."""
    client = FakeClient(
        pages={
            "https://www.acme.co.uk/careers": '<a href="https://jobs.ashbyhq.com/acme">Jobs</a>'
        },
        missing={"https://www.acme.com", "https://www.acme.com/careers"},
    )

    record = resolve_careers.resolve(client, "Acme")

    assert (record["platform"], record["slug"]) == ("ashby", "acme")
    assert record["source_url"] == "https://www.acme.co.uk/careers"


def test_resolve_records_a_platform_it_cannot_read_yet() -> None:
    """Forty employers on iCIMS is the argument for an adapter, so the hit is kept with a note."""
    client = FakeClient({"https://www.acme.com": '<a href="https://acme.icims.com/jobs">Jobs</a>'})

    record = resolve_careers.resolve(client, "Acme")

    assert record["platform"] == "icims"
    assert record["note"] == "icims has no adapter yet"


def test_resolve_says_so_when_nothing_answers() -> None:
    missing = {
        f"https://www.{slug}.{suffix}" for slug in ("acme",) for suffix in resolve_careers.SUFFIXES
    }
    client = FakeClient({}, missing=missing)

    record = resolve_careers.resolve(client, "Acme")

    assert record["platform"] == ""
    assert record["note"] == "no domain answered"


def test_resolve_keeps_trying_a_domain_that_failed_mid_flight() -> None:
    """A reset or a timeout says nothing about the domain, so it must not count as a miss.

    Only a connection that never opened (DNS, refused) proves the domain is not the employer's.
    """

    class ResettingClient(FakeClient):
        def get_text(self, url: str) -> str:
            self.requested.append(url)
            if url == "https://www.acme.com":
                raise HttpError(
                    "connection failed during response", url=url, retryable=True, uncertain=True
                )
            if url == "https://www.acme.com/careers":
                return '<a href="https://jobs.lever.co/acme">Jobs</a>'
            raise HttpError("connection failed", url=url, retryable=True, uncertain=False)

    client = ResettingClient({})

    record = resolve_careers.resolve(client, "Acme")

    assert (record["platform"], record["slug"]) == ("lever", "acme")
