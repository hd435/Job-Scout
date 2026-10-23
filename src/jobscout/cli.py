"""Command-line interface.

M1 supports local dry runs only: no Telegram delivery, no scheduling, and no
source other than the configured Palantir Lever board.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .config import AppConfig, ConfigError, SourceConfig, load_config
from .http import FixtureHttpClient, UrllibHttpClient
from .models import MatchOutcome, ScanOutcome, ScanStatus, SourceState, StoredJob
from .scanner import run_scan
from .sources import AdapterError, get_adapter
from .storage import Storage

EXIT_OK = 0
EXIT_DEGRADED = 1
EXIT_FAILED = 2


def main(argv: Sequence[str] | None = None) -> int:
    _configure_output()
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            return _cmd_scan(args)
        if args.command == "summary":
            return _cmd_summary(args)
        return _cmd_status(args)
    except (ConfigError, AdapterError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED


def _configure_output() -> None:
    """Keep redirected output (logs, CI, pipes) valid UTF-8 on every platform."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobscout",
        description="Personal UK early-career job monitor (M1: local Palantir dry run).",
    )
    parser.add_argument("--version", action="version", version=f"jobscout {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="scan one source and persist the result")
    scan.add_argument("--source", help="configured source id (default: the only source)")
    scan.add_argument(
        "--dry-run",
        action="store_true",
        help="required in M1: record findings without sending notifications",
    )
    scan.add_argument("--db", type=Path, help="SQLite database path")
    scan.add_argument("--config", type=Path, help="TOML configuration file")
    scan.add_argument(
        "--fixture",
        type=Path,
        help="read the source payload from a local JSON file instead of the network",
    )
    scan.add_argument(
        "--accept-empty",
        action="store_true",
        help="treat an empty result from a previously non-empty source as complete",
    )

    summary = subparsers.add_parser("summary", help="list currently matching roles (read-only)")
    summary.add_argument(
        "--current",
        action="store_true",
        help="show roles that are currently open and matching (the default)",
    )
    summary.add_argument("--source", help="configured source id (default: all sources)")
    summary.add_argument("--db", type=Path, help="SQLite database path")
    summary.add_argument("--config", type=Path, help="TOML configuration file")

    status = subparsers.add_parser("status", help="show source and scan status")
    status.add_argument("--source", help="configured source id (default: all sources)")
    status.add_argument("--db", type=Path, help="SQLite database path")
    status.add_argument("--config", type=Path, help="TOML configuration file")

    return parser


def _cmd_scan(args: argparse.Namespace) -> int:
    if not args.dry_run:
        print(
            "error: M1 has no notification delivery; re-run with --dry-run",
            file=sys.stderr,
        )
        return EXIT_FAILED

    app = load_config(args.config)
    source = _resolve_source(app, args.source)
    adapter = get_adapter(source.adapter)
    http_client = FixtureHttpClient(args.fixture) if args.fixture else UrllibHttpClient()
    db_path = args.db or app.database_path

    with Storage.open(db_path) as storage:
        outcome = run_scan(storage, adapter, source, http_client, accept_empty=args.accept_empty)

    _print_scan_outcome(outcome, source, db_path)
    if outcome.status is ScanStatus.FAILED:
        return EXIT_FAILED
    if outcome.status is ScanStatus.PARTIAL:
        return EXIT_DEGRADED
    return EXIT_OK


def _cmd_summary(args: argparse.Namespace) -> int:
    app = load_config(args.config)
    sources = _selected_sources(app, args.source)
    db_path = args.db or app.database_path

    with Storage.open(db_path) as storage:
        printed = False
        for source in sources:
            state = storage.get_source_state(source.id)
            if state is None:
                continue
            printed = True
            matches = storage.list_jobs_by_decision(source.id, MatchOutcome.MATCH)
            reviews = storage.list_jobs_by_decision(source.id, MatchOutcome.NEEDS_REVIEW)
            _print_summary(source, state, matches, reviews)
        if not printed:
            print("No scans recorded yet. Run: jobscout scan --source palantir --dry-run")
    return EXIT_OK


def _cmd_status(args: argparse.Namespace) -> int:
    app = load_config(args.config)
    sources = _selected_sources(app, args.source)
    db_path = args.db or app.database_path

    with Storage.open(db_path) as storage:
        print(f"Database: {db_path}")
        for source in sources:
            state = storage.get_source_state(source.id)
            _print_status(source, state, storage)
    return EXIT_OK


def _resolve_source(app: AppConfig, requested: str | None) -> SourceConfig:
    if requested:
        source = app.source(requested)
    else:
        enabled = app.enabled_sources()
        if len(enabled) != 1:
            names = ", ".join(source.id for source in enabled)
            raise ConfigError(f"--source is required (configured: {names})")
        source = enabled[0]
    if not source.enabled:
        raise ConfigError(f"source '{source.id}' is disabled in configuration")
    return source


def _selected_sources(app: AppConfig, requested: str | None) -> tuple[SourceConfig, ...]:
    return (app.source(requested),) if requested else app.sources


def _print_scan_outcome(outcome: ScanOutcome, source: SourceConfig, db_path: Path) -> None:
    status_line = "complete" if outcome.complete else "incomplete"
    print(f"Scan #{outcome.scan_id} - {source.id} ({source.employer}) - dry run, no notifications")
    print(f"Database: {db_path}")
    print(
        f"Status: {outcome.status} ({status_line})"
        f"  records: seen={outcome.records_seen} parsed={outcome.records_parsed}"
        f" skipped={outcome.skipped_count}"
    )
    if outcome.error_summary:
        print(f"Note: {outcome.error_summary}")

    if outcome.is_baseline:
        print(
            f"Baseline established: {outcome.records_parsed} role(s) recorded silently;"
            " no individual alerts for existing roles."
        )
    print(
        f"New roles: {outcome.new_jobs}  updated: {outcome.updated_jobs}"
        f"  reopened: {outcome.reopened_jobs}  closed: {outcome.closed_jobs}"
        f"  needs review (this scan): {outcome.needs_review}"
    )
    if outcome.notifications_queued:
        print(f"Notifications queued: {outcome.notifications_queued}")

    if outcome.candidates:
        print()
        print(f"Alert candidates ({len(outcome.candidates)}):")
        for index, candidate in enumerate(outcome.candidates, start=1):
            details = [candidate.location_raw or "unknown location"]
            if candidate.salary_text:
                details.append(candidate.salary_text)
            if candidate.closing_at:
                details.append(f"closes {candidate.closing_at}")
            print(f"  {index}. {candidate.title} - {candidate.employer}")
            print(f"     {' | '.join(details)}")
            print(f"     {candidate.url}")
            print(f"     reasons: {candidate.decision.reason_text()}")
    else:
        print("Alert candidates: none")

    if outcome.skipped:
        print()
        print(f"Skipped records ({outcome.skipped_count}):")
        for skip in outcome.skipped:
            print(f"  - {skip.describe()}")


def _print_summary(
    source: SourceConfig,
    state: SourceState,
    matches: Sequence[StoredJob],
    reviews: Sequence[StoredJob],
) -> None:
    print(f"Current matching roles for {source.id} ({source.employer}) - read-only")
    if not state.has_baseline:
        print("  No complete baseline scan yet; current matches are not final.")
    print(f"  Matches: {len(matches)}   Needs review: {len(reviews)}")
    for stored in matches:
        job = stored.job
        details = [job.location_raw or "unknown location"]
        if job.salary_text:
            details.append(job.salary_text)
        print(f"  - {job.title} | {' | '.join(details)}")
        print(f"    {job.url}")
    if reviews:
        print("  Needs review (not alert-eligible):")
        for stored in reviews:
            reason = stored.match.reason_text() if stored.match else "no decision"
            print(f"  - {stored.job.title} | {reason}")


def _print_status(source: SourceConfig, state: SourceState | None, storage: Storage) -> None:
    print()
    print(
        f"{source.id} ({source.employer})  adapter={source.adapter}"
        f"  enabled={'yes' if source.enabled else 'no'}"
        f"  cadence={source.cadence_minutes}m"
    )
    if state is None:
        print("  No scans recorded yet.")
        return
    baseline = (
        f"{state.baseline_completed_at} (baseline scan #{state.baseline_scan_id})"
        if state.has_baseline
        else "not established"
    )
    print(f"  Baseline: {baseline}")
    completeness = (
        "complete"
        if state.last_scan_complete
        else "incomplete"
        if state.last_scan_complete is not None
        else "unknown"
    )
    print(
        f"  Last scan: {state.last_scan_status or 'none'} ({completeness})"
        f"  records: seen={state.records_seen if state.records_seen is not None else '-'}"
        f" parsed={state.records_parsed if state.records_parsed is not None else '-'}"
        f" skipped={state.skipped_count if state.skipped_count is not None else '-'}"
    )
    print(f"  Last success: {state.last_success_at or 'none'}")
    print(f"  Consecutive failures: {state.consecutive_failures}")
    if state.last_error_summary:
        print(f"  Last error: {state.last_error_at} - {state.last_error_summary}")
    else:
        print("  Last error: none")
    print(
        f"  Open roles: matches={storage.count_by_decision(source.id, MatchOutcome.MATCH)}"
        f" needs_review={storage.count_by_decision(source.id, MatchOutcome.NEEDS_REVIEW)}"
    )
    counts = storage.notification_status_counts()
    if counts:
        summary = ", ".join(f"{status}={total}" for status, total in sorted(counts.items()))
        print(f"  Notifications: {summary}")
    else:
        print("  Notifications: none recorded")
    last_error = storage.last_notification_error()
    if last_error:
        print(f"  Last delivery error: {last_error[0]} - {last_error[1]}")
    recent = storage.recent_scans(source.id, limit=3)
    if recent:
        summary = ", ".join(f"#{row['id']} {row['status']}" for row in recent)
        print(f"  Recent scans ({storage.count_scans(source.id)} total): {summary}")
