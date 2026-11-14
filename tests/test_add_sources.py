"""Generating catalogue files from a probe's enrol verdicts.

This is the step that makes expansion cheap, so the rules that stop it doing damage matter more than
the formatting: a board already monitored must not be added twice, an id must collide with nothing,
and a board with roles but no matches is storage without value.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

import add_sources  # noqa: E402

RECORD = {
    "adapter": "workable",
    "slug": "acme-london",
    "employer": "Acme London Ltd",
    "records_parsed": 40,
    "uk_roles": 38,
    "early_family_roles": 2,
    "matches": 2,
    "early_titles": ["Graduate Data Analyst"],
    "verdict": "enrol",
}


def write_probe(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "probe.json"
    path.write_text(json.dumps({"records": records}), encoding="utf-8")
    return path


def run(tmp_path: Path, probe: Path, *extra: str) -> tuple[int, str]:
    import contextlib
    import io

    argv = sys.argv
    sys.argv = ["add_sources.py", "--probe", str(probe), "--catalogue", str(tmp_path), *extra]
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            code = add_sources.main()
    finally:
        sys.argv = argv
    return code, out.getvalue()


def test_slugify_makes_a_stable_short_id() -> None:
    assert add_sources.slugify("Fuse Energy") == "fuse_energy"
    assert add_sources.slugify("The Focusrite Group") == "focusrite_group"


def test_render_writes_a_file_that_reads_like_a_hand_written_one() -> None:
    text = add_sources.render(RECORD, "acme_london", "2026-11-14")

    assert 'id = "acme_london"' in text
    assert 'adapter = "workable"' in text
    assert 'account = "acme-london"' in text
    assert "access_note" in text
    assert "# Example: Graduate Data Analyst" in text
    assert "uk_employer = true" in text


def test_render_writes_a_workday_site_as_three_options() -> None:
    record = dict(RECORD, adapter="workday", slug="acme.wd3.myworkdayjobs.com/acme/External")

    text = add_sources.render(record, "acme", "2026-11-14")

    assert 'host = "acme.wd3.myworkdayjobs.com"' in text
    assert 'tenant = "acme"' in text
    assert 'site = "External"' in text


def test_render_leaves_uk_employer_off_a_board_that_is_mostly_not_uk() -> None:
    record = dict(RECORD, records_parsed=100, uk_roles=20)

    text = add_sources.render(record, "acme", "2026-11-14")

    assert "uk_employer" not in text


def test_a_board_with_no_matches_is_skipped(tmp_path: Path) -> None:
    probe = write_probe(tmp_path, [dict(RECORD, matches=0)])

    code, out = run(tmp_path, probe)

    assert code == 0
    assert not list(tmp_path.glob("acme*.toml"))
    assert "0 matches" in out


def test_a_board_with_no_matches_is_written_when_it_is_accepted(tmp_path: Path) -> None:
    """An employer with early-career family roles but no match today can still be worth watching."""
    probe = write_probe(tmp_path, [dict(RECORD, matches=0)])

    run(tmp_path, probe, "--accept-unmeasured")

    assert (tmp_path / "acme_london_ltd.toml").exists()


def test_a_monitored_board_is_not_added_twice(tmp_path: Path) -> None:
    probe = write_probe(tmp_path, [dict(RECORD, slug="focusrite", employer="Focusrite Group")])

    code, out = run(tmp_path, probe)

    assert code == 0
    assert "already monitored" in out
    assert not list(tmp_path.glob("focusrite*.toml"))


def test_a_defer_verdict_is_never_written(tmp_path: Path) -> None:
    probe = write_probe(tmp_path, [dict(RECORD, verdict="defer")])

    run(tmp_path, probe)

    assert not list(tmp_path.glob("acme*.toml"))


def test_an_enrol_verdict_is_written(tmp_path: Path) -> None:
    probe = write_probe(tmp_path, [RECORD])

    run(tmp_path, probe)

    assert (tmp_path / "acme_london_ltd.toml").exists()
