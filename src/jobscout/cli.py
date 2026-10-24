"""Command-line interface.

`scan --dry-run` records findings without contacting anyone. `scan --deliver`
(and the standalone `deliver` command) send queued alerts through the Telegram
Bot API. There is still no scheduler and only the configured Palantir source.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .config import (
    AppConfig,
    ConfigError,
    SourceConfig,
    TelegramConfig,
    load_config,
    load_telegram_config,
)
from .digest import build_digest
from .http import FixtureHttpClient, UrllibHttpClient
from .matching import MatchingRules, preferences_fingerprint, split_pinned_cities
from .models import (
    DeliveryOutcome,
    DeliveryReport,
    MatchOutcome,
    ScanOutcome,
    ScanStatus,
    SourceState,
    StoredJob,
)
from .notices import FailureNoticeSender
from .notifier import Notifier, redact
from .report import DEFAULT_WINDOW_DAYS, build_trial_report, format_trial_report
from .retention import RetentionPolicy
from .scanner import run_scan
from .scheduler import Scheduler, SourceRun
from .sources import AdapterError, get_adapter
from .storage import Storage, utc_now_iso
from .telegram import TelegramTransport
from .validate import (
    ADAPTER_PROFILES,
    DEFAULT_SAMPLE_RECORDS,
    ensure_artifacts_location,
    profile_for,
    run_live_check,
)

EXIT_OK = 0
EXIT_DEGRADED = 1
EXIT_FAILED = 2

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VALIDATION_ARTIFACT_ROOT = PROJECT_ROOT / "data"
DEFAULT_VALIDATION_FIXTURES = {
    "palantir": PROJECT_ROOT / "tests" / "fixtures" / "palantir_jobs.json",
    "monzo": PROJECT_ROOT / "tests" / "fixtures" / "monzo_validation.json",
    "lloyds_graduate": PROJECT_ROOT / "tests" / "fixtures" / "workday_lloyds_graduate.json",
}
VALIDATION_SOURCES = frozenset(DEFAULT_VALIDATION_FIXTURES)


def main(argv: Sequence[str] | None = None) -> int:
    _configure_output()
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            return _cmd_scan(args)
        if args.command == "summary":
            return _cmd_summary(args)
        if args.command == "deliver":
            return _cmd_deliver(args)
        if args.command == "validate":
            return _cmd_validate(args)
        if args.command == "schedule":
            return _cmd_schedule(args)
        if args.command == "prune":
            return _cmd_prune(args)
        if args.command == "report":
            return _cmd_report(args)
        if args.command == "digest":
            return _cmd_digest(args)
        if args.command == "sources":
            return _cmd_sources(args)
        if args.command == "config":
            return _cmd_config(args)
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
        description="Personal UK early-career job monitor (Workday and Lever sources, "
        "Telegram delivery).",
    )
    parser.add_argument("--version", action="version", version=f"jobscout {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="scan one source and persist the result")
    scan.add_argument(
        "--source",
        help="configured source id (required when more than one source is configured)",
    )
    mode = scan.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="record findings without sending notifications",
    )
    mode.add_argument(
        "--deliver",
        action="store_true",
        help="queue and deliver alerts for new matching roles via Telegram",
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

    deliver = subparsers.add_parser(
        "deliver", help="retry delivery of already-queued alerts (no scan)"
    )
    deliver.add_argument("--db", type=Path, help="SQLite database path")
    deliver.add_argument("--config", type=Path, help="TOML configuration file")

    status = subparsers.add_parser("status", help="show source and scan status")
    status.add_argument("--source", help="configured source id (default: all sources)")
    status.add_argument("--db", type=Path, help="SQLite database path")
    status.add_argument("--config", type=Path, help="TOML configuration file")

    validate = subparsers.add_parser(
        "validate",
        help="one bounded live request to compare a source with its fixture (no database)",
    )
    validate.add_argument(
        "--source",
        default="palantir",
        help="configured source id to validate (default: palantir)",
    )
    validate.add_argument(
        "--confirm-live",
        action="store_true",
        help="acknowledge that this command makes real requests to the source",
    )
    validate.add_argument(
        "--fixture",
        type=Path,
        help="recorded payload to compare against (default: the fixture for --source)",
    )
    validate.add_argument(
        "--sample-out",
        type=Path,
        help="directory for the trimmed sample and report (default: data/validation)",
    )
    validate.add_argument(
        "--max-records",
        type=int,
        default=DEFAULT_SAMPLE_RECORDS,
        help=f"records kept in the sample (default: {DEFAULT_SAMPLE_RECORDS})",
    )
    validate.add_argument("--config", type=Path, help="TOML configuration file")

    schedule = subparsers.add_parser(
        "schedule",
        help="run due sources on their cadence (no deployment, no daemon supervision)",
    )
    schedule.add_argument(
        "--once",
        action="store_true",
        help="run the sources that are due now, then exit",
    )
    schedule.add_argument(
        "--max-iterations",
        type=int,
        help="stop the loop after N passes (default: run until interrupted)",
    )
    schedule.add_argument(
        "--jitter-seconds",
        type=float,
        default=0.0,
        help="random extra delay added to each source's next run (default: 0)",
    )
    schedule.add_argument("--db", type=Path, help="SQLite database path")
    schedule.add_argument("--config", type=Path, help="TOML configuration file")

    prune = subparsers.add_parser(
        "prune",
        help="delete or strip historical data according to the retention policy",
    )
    prune.add_argument(
        "--dry-run", action="store_true", help="report what would change, delete nothing"
    )
    prune.add_argument(
        "--closed-content-days",
        type=int,
        default=RetentionPolicy.closed_content_days,
        help="days after closure before description and salary are cleared",
    )
    prune.add_argument(
        "--closed-job-days",
        type=int,
        default=RetentionPolicy.closed_job_days,
        help="days after closure before a closed role's identity is deleted",
    )
    prune.add_argument(
        "--scan-days",
        type=int,
        default=RetentionPolicy.scan_days,
        help="days before unreferenced scan history is deleted",
    )
    prune.add_argument(
        "--notification-error-days",
        type=int,
        default=RetentionPolicy.notification_error_days,
        help="days before stored delivery error text is cleared",
    )
    prune.add_argument("--batch-size", type=int, help="rows handled per batch")
    prune.add_argument("--db", type=Path, help="SQLite database path")
    prune.add_argument("--config", type=Path, help="TOML configuration file")

    trial = subparsers.add_parser(
        "report",
        help="offline trial scorecard: scans, gaps, failures and delivery outcomes (read-only)",
    )
    trial.add_argument(
        "--days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help="measurement window in days (default: 7)",
    )
    trial.add_argument("--source", help="limit the report to one configured source id")
    trial.add_argument("--db", type=Path, help="SQLite database path")
    trial.add_argument("--config", type=Path, help="TOML configuration file")

    digest = subparsers.add_parser(
        "digest",
        help="send the roles that already match, once (read-only; not the alert pipeline)",
    )
    digest.add_argument("--source", help="limit the digest to one configured source id")
    digest.add_argument(
        "--dry-run", action="store_true", help="print the messages instead of sending them"
    )
    digest.add_argument("--db", type=Path, help="SQLite database path")
    digest.add_argument("--config", type=Path, help="TOML configuration file")

    sources = subparsers.add_parser(
        "sources",
        help="list the configured sources with their adapter and state (read-only)",
    )
    source_commands = sources.add_subparsers(dest="sources_command", required=True)
    sources_list = source_commands.add_parser(
        "list", help="every configured source, its adapter, and its last scan"
    )
    sources_list.add_argument("--db", type=Path, help="SQLite database path")
    sources_list.add_argument("--config", type=Path, help="TOML configuration file")

    config = subparsers.add_parser(
        "config",
        help="inspect the effective configuration and preferences (read-only)",
    )
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_show = config_commands.add_parser(
        "show", help="print the effective settings, sources and preferences"
    )
    config_show.add_argument("--config", type=Path, help="TOML configuration file")
    config_validate = config_commands.add_parser(
        "validate", help="load the configuration and report every problem found"
    )
    config_validate.add_argument("--config", type=Path, help="TOML configuration file")

    return parser


def _load(config_path: Path | None) -> AppConfig:
    """Load configuration, surfacing anything the user should know but need not fix now."""
    app = load_config(config_path)
    for warning in app.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return app


def _build_notifier(telegram: TelegramConfig) -> Notifier:
    """Single seam for tests: the only place a real Telegram transport is created."""
    transport = TelegramTransport(telegram, poster=UrllibHttpClient())
    return Notifier(transport, secret_values=(telegram.token,))


def _cmd_scan(args: argparse.Namespace) -> int:
    if not args.dry_run and not args.deliver:
        print(
            "error: choose --dry-run to record findings or --deliver to send alerts",
            file=sys.stderr,
        )
        return EXIT_FAILED

    telegram = load_telegram_config() if args.deliver else None
    app = _load(args.config)
    source = _resolve_source(app, args.source)
    adapter = get_adapter(source.adapter)
    http_client = FixtureHttpClient(args.fixture) if args.fixture else UrllibHttpClient()
    db_path = args.db or app.database_path
    notifier = _build_notifier(telegram) if telegram else None

    report = None
    with Storage.open(db_path) as storage:
        outcome = run_scan(
            storage,
            adapter,
            source,
            http_client,
            accept_empty=args.accept_empty,
            notify_chat_id=telegram.chat_id if telegram else None,
            rules=app.matching,
        )
        if notifier and telegram:
            report = notifier.deliver_pending(storage, telegram.chat_id)

    _print_scan_outcome(outcome, source, db_path, delivering=telegram is not None)
    if report is not None:
        _print_delivery_report(report)

    if outcome.status is ScanStatus.FAILED:
        return EXIT_FAILED
    if outcome.status is ScanStatus.PARTIAL:
        return EXIT_DEGRADED
    if report is not None and (report.failed or report.ambiguous):
        return EXIT_DEGRADED
    return EXIT_OK


def _cmd_deliver(args: argparse.Namespace) -> int:
    telegram = load_telegram_config()
    app = _load(args.config)
    db_path = args.db or app.database_path

    with Storage.open(db_path) as storage:
        report = _build_notifier(telegram).deliver_pending(storage, telegram.chat_id)

    _print_delivery_report(report)
    return EXIT_DEGRADED if report.failed or report.ambiguous else EXIT_OK


def _print_delivery_report(report: DeliveryReport) -> None:
    print(
        f"Delivery: sent={report.sent} failed={report.failed}"
        f" ambiguous={report.ambiguous} retrying={report.retrying}"
    )
    if report.failed:
        print("  Some alerts failed permanently; see: jobscout status")
    if report.ambiguous:
        print("  Some delivery outcomes were ambiguous and are not retried automatically.")
    if report.retrying:
        print("  Transient failures remain queued; run: jobscout deliver")


def _cmd_validate(args: argparse.Namespace) -> int:
    """One bounded live check of one configured source; never touches Telegram or scans."""
    if not args.confirm_live:
        print(
            "error: validate makes real requests to the source."
            " Re-run with --confirm-live to acknowledge it.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    if args.max_records < 1:
        print("error: --max-records must be at least 1", file=sys.stderr)
        return EXIT_FAILED

    app = _load(args.config)
    try:
        source = app.source(args.source)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED

    profile = profile_for(source)
    if profile is None:
        print(
            f"error: no validation profile for '{source.id}': its adapter"
            f" '{source.adapter}' is not supported; known adapters:"
            f" {', '.join(sorted(ADAPTER_PROFILES))}",
            file=sys.stderr,
        )
        return EXIT_FAILED

    fixture_path = args.fixture or DEFAULT_VALIDATION_FIXTURES.get(source.id)
    fixture_payload = None
    if fixture_path is not None:
        try:
            fixture_payload = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"error: cannot read fixture {fixture_path}: {exc}", file=sys.stderr)
            return EXIT_FAILED
    elif profile.payload_kind == "json":
        print(
            f"error: no recorded fixture for '{source.id}'."
            " Pass --fixture <path> with a recorded response so the shape can be compared.",
            file=sys.stderr,
        )
        return EXIT_FAILED

    sample_dir = args.sample_out or VALIDATION_ARTIFACT_ROOT / "validation"
    try:
        ensure_artifacts_location(sample_dir, VALIDATION_ARTIFACT_ROOT)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    client = UrllibHttpClient(max_attempts=1)

    try:
        outcome = run_live_check(
            source,
            fixture_payload=fixture_payload,
            client=client,
            sample_dir=sample_dir,
            allowed_root=VALIDATION_ARTIFACT_ROOT,
            max_records=args.max_records,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED

    print(f"Live validation: {source.id} ({source.employer}) - adapter {source.adapter}")
    print(f"  Budget: {profile.request_budget} request(s)")
    for line in outcome.lines:
        print(f"  {line}")
    if outcome.sample_path is not None:
        print(f"  Sample: {outcome.sample_path}")
    if outcome.report_path is not None:
        print(f"  Report: {outcome.report_path}")
    print("  Database: not opened")
    return EXIT_OK if outcome.ok else EXIT_DEGRADED


def _cmd_schedule(args: argparse.Namespace) -> int:
    """Run due sources once or on a loop. Delivery is optional, scanning is not."""
    app = _load(args.config)
    db_path = args.db or app.database_path
    telegram = _optional_telegram()
    notifier = _build_notifier(telegram) if telegram else None
    notice_sender = _build_notice_sender(telegram) if telegram else None

    if telegram is None:
        print("Telegram is not configured: scans run without delivery or failure notices.")

    http_client = UrllibHttpClient()
    with Storage.open(db_path) as storage:
        scheduler = Scheduler(
            storage,
            app,
            http_client=http_client,
            notifier=notifier,
            notice_sender=notice_sender,
            chat_id=telegram.chat_id if telegram else None,
            jitter_seconds=args.jitter_seconds,
            sleep=time.sleep,
            rules=app.matching,
        )
        print(f"Database: {db_path}")
        if args.once:
            runs = scheduler.run_once()
            _print_source_runs(runs)
            return EXIT_OK

        def report(runs: Sequence[SourceRun]) -> None:
            _print_source_runs(runs)

        scheduler.run_forever(max_iterations=args.max_iterations, on_run=report)
    return EXIT_OK


def _print_source_runs(runs: Sequence[SourceRun]) -> None:
    if not runs:
        print("No source is due.")
        return
    for run in runs:
        if not run.scanned:
            reason = run.error or run.skipped_reason or "not attempted"
            print(f"{run.source_id}: skipped ({reason})")
        else:
            print(
                f"{run.source_id}: {run.status}"
                f" seen={run.outcome.records_seen if run.outcome else 0}"
                f" new={run.outcome.new_jobs if run.outcome else 0}"
                f" alerts={run.outcome.notifications_queued if run.outcome else 0}"
            )
        if run.post_run_error:
            print(f"  warning: {run.post_run_error}")
        if run.delivery is not None:
            print(
                f"  delivery: sent={run.delivery.sent} failed={run.delivery.failed}"
                f" ambiguous={run.delivery.ambiguous} retrying={run.delivery.retrying}"
            )
        if run.notice is not None and run.notice.attempted:
            print(f"  failure notice: {run.notice.outcome}")
        if run.next_run_at:
            print(f"  next run: {run.next_run_at}")


def _optional_telegram() -> TelegramConfig | None:
    """Scheduling works without Telegram; alerts and notices then stay off."""
    try:
        return load_telegram_config()
    except ConfigError:
        return None


def _build_notice_sender(telegram: TelegramConfig) -> FailureNoticeSender:
    """Single seam for tests: the only place a real notice transport is created."""
    transport = TelegramTransport(telegram, poster=UrllibHttpClient())
    return FailureNoticeSender(transport, secret_values=(telegram.token,))


def _cmd_prune(args: argparse.Namespace) -> int:
    """Explicit, bounded cleanup. Never called from scans or the scheduler."""
    try:
        policy = RetentionPolicy(
            closed_content_days=args.closed_content_days,
            closed_job_days=args.closed_job_days,
            scan_days=args.scan_days,
            notification_error_days=args.notification_error_days,
            **({"batch_size": args.batch_size} if args.batch_size else {}),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED

    app = _load(args.config)
    db_path = args.db or app.database_path
    with Storage.open(db_path) as storage:
        report = storage.prune(policy, now=utc_now_iso(), dry_run=args.dry_run)

    prefix = "Would" if report.dry_run else "Pruned"
    print(f"Database: {db_path}")
    print(
        f"Policy: closed content {policy.closed_content_days}d,"
        f" closed identity {policy.closed_job_days}d,"
        f" scans {policy.scan_days}d,"
        f" notification errors {policy.notification_error_days}d"
    )
    print(f"{prefix} clear {report.cleared_job_content} closed role(s) of description/salary")
    print(f"{prefix} clear {report.cleared_notification_errors} delivery error message(s)")
    print(f"{prefix} delete {report.deleted_notifications} notification(s)")
    print(f"{prefix} delete {report.deleted_jobs} closed role(s)")
    print(f"{prefix} delete {report.deleted_scans} scan record(s)")
    if report.truncated:
        print("More historical data remains; run prune again to continue.")
    if report.dry_run:
        print("Dry run: nothing was changed.")
    return EXIT_OK


def _cmd_digest(args: argparse.Namespace) -> int:
    """Send the roles that already match, once. Reads only; never queues a notification."""
    app = _load(args.config)
    db_path = args.db or app.database_path
    if not db_path.exists():
        print(f"error: database not found: {db_path}", file=sys.stderr)
        return EXIT_FAILED

    sources = (app.source(args.source),) if args.source else app.enabled_sources()
    generated_at = utc_now_iso()
    with Storage.open(db_path) as storage:
        entries = [
            (
                source.id,
                source.employer,
                storage.list_jobs_by_decision(source.id, MatchOutcome.MATCH),
            )
            for source in sources
        ]

    messages = build_digest(entries, generated_at=generated_at)
    for source_id, employer, jobs in entries:
        if not jobs:
            print(f"{source_id}: no matching roles to send ({employer})")
    if not messages:
        print("Nothing to send.")
        return EXIT_OK

    total = sum(message.role_count for message in messages)
    print(f"Digest: {len(messages)} message(s) covering {total} role(s)")
    if args.dry_run:
        for message in messages:
            print(f"\n--- {message.source_id} ---")
            print(message.text)
        print("\nDry run: nothing was sent and nothing was written.")
        return EXIT_OK

    telegram = load_telegram_config()
    transport = TelegramTransport(telegram, poster=UrllibHttpClient())
    failed = 0
    for message in messages:
        result = transport.send(telegram.chat_id, message.text)
        if result.outcome is DeliveryOutcome.SENT:
            print(f"{message.source_id}: sent ({message.role_count} role(s))")
        else:
            failed += 1
            print(
                f"{message.source_id}: not sent ({result.outcome})"
                f" {redact(result.error, secret_values=(telegram.token,)) or ''}".rstrip(),
                file=sys.stderr,
            )
    print("The digest does not queue alerts; new roles still arrive through normal scans.")
    return EXIT_OK if failed == 0 else EXIT_DEGRADED


def _cmd_report(args: argparse.Namespace) -> int:
    """Read-only trial scorecard: no data changes, no network.

    Opening the database still applies pending schema migrations, so an older file is
    upgraded on first read; nothing else is written.
    """
    if args.days < 1:
        print("error: days must be at least 1", file=sys.stderr)
        return EXIT_FAILED
    app = _load(args.config)
    db_path = args.db or app.database_path
    if not db_path.exists():
        # Never create an empty database just to print a report about it.
        print(f"error: database not found: {db_path}", file=sys.stderr)
        return EXIT_FAILED

    with Storage.open(db_path) as storage:
        report = build_trial_report(
            storage, days=args.days, source_id=args.source, now=utc_now_iso()
        )

    print(f"Database: {db_path}")
    print(format_trial_report(report))
    return EXIT_OK


def _cmd_summary(args: argparse.Namespace) -> int:
    app = _load(args.config)
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
    app = _load(args.config)
    sources = _selected_sources(app, args.source)
    db_path = args.db or app.database_path

    with Storage.open(db_path) as storage:
        print(f"Database: {db_path}")
        print(f"Matching rules: {_describe_matching(app.matching)}")
        for source in sources:
            state = storage.get_source_state(source.id)
            _print_status(source, state, storage)
    return EXIT_OK


def _describe_matching(rules: MatchingRules) -> str:
    if not rules.has_overrides:
        return f"{rules.version} (built-in defaults)"
    terms = sum(len(value) for value in rules.extra_family_terms.values())
    if not terms and not rules.extra_exclude_terms:
        return f"{rules.version} (preferences differ from the defaults)"
    return (
        f"{rules.version} (config overrides: +{terms} family term(s),"
        f" +{len(rules.extra_exclude_terms)} exclusion(s))"
    )


def _cmd_sources(args: argparse.Namespace) -> int:
    """Every configured source with its adapter and, when the database exists, its state."""
    app = _load(args.config)
    db_path = args.db or app.database_path
    enabled = {source.id for source in app.enabled_sources()}

    print(f"Config: {app.config_path if app.config_path else 'none (built-in defaults)'}")
    print(f"Sources: {len(app.sources)} configured, {len(enabled)} enabled")
    if not Path(db_path).exists():
        print(f"Database: {db_path} (not created yet)")
    print()

    rows: list[tuple[str, ...]] = [
        ("id", "employer", "adapter", "every", "state", "last success", "matches")
    ]
    with contextlib.ExitStack() as stack:
        storage = stack.enter_context(Storage.open(db_path)) if Path(db_path).exists() else None
        for source in app.sources:
            state = storage.get_source_state(source.id) if storage else None
            rows.append(
                (
                    source.id,
                    source.employer,
                    source.adapter,
                    f"{source.cadence_minutes}m",
                    _source_state_text(source, state, enabled),
                    (state.last_success_at if state and state.last_success_at else "-"),
                    _source_match_text(storage, source.id),
                )
            )
    _print_table(rows)
    if storage is None:
        print(f"\nState is unknown until the first scan writes {db_path}.")
    return EXIT_OK


def _source_state_text(source: SourceConfig, state: SourceState | None, enabled: set[str]) -> str:
    if source.id not in enabled:
        return "disabled"
    if state is None or not state.last_scan_status:
        return "enabled, not yet scanned"
    completeness = "complete" if state.last_scan_complete else "incomplete"
    return f"{state.last_scan_status}/{completeness}"


def _source_match_text(storage: Storage | None, source_id: str) -> str:
    if storage is None or storage.get_source_state(source_id) is None:
        return "-"
    matches = len(storage.list_jobs_by_decision(source_id, MatchOutcome.MATCH))
    reviews = len(storage.list_jobs_by_decision(source_id, MatchOutcome.NEEDS_REVIEW))
    return f"{matches} (+{reviews} review)"


def _print_table(rows: Sequence[tuple[str, ...]]) -> None:
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    for row in rows:
        cells = (cell.ljust(width) for cell, width in zip(row, widths, strict=True))
        print("  ".join(cells).rstrip())


def _cmd_config(args: argparse.Namespace) -> int:
    if args.config_command == "validate":
        return _config_validate(args)
    return _config_show(args)


def _config_show(args: argparse.Namespace) -> int:
    app = _load(args.config)
    rules = app.matching

    print(f"Config: {app.config_path if app.config_path else 'none (built-in defaults)'}")
    print(f"Database: {app.database_path}")
    print(f"Rules: {_describe_matching(rules)}")
    print(f"Preference fingerprint: {preferences_fingerprint(rules)}")
    print(f"Sources: {len(app.sources)} configured, {len(app.enabled_sources())} enabled")
    for source in app.sources:
        disabled = "" if source.enabled else "  [disabled]"
        print(
            f"  - {source.id} ({source.employer}) via {source.adapter}"
            f", every {source.cadence_minutes}m{disabled}"
        )
        for key, value in sorted(source.options.items()):
            print(f"      {key} = {value!r}")

    print("Preferences:")
    print(f"  Role families: {', '.join(rules.alerting_families)}")
    print(f"  Employment types: {', '.join(rules.alerting_employment_types)}")
    print(f"  Seniority exclusions: {', '.join(rules.seniority_exclusions)}")
    exclusions = rules.extra_exclude_terms
    print(f"  Title exclusions: {', '.join(exclusions) if exclusions else 'none'}")
    locations = rules.locations
    print(f"  Locations: mode = {locations.mode}")
    if locations.cities:
        print(f"    cities: {', '.join(locations.cities)}")
        known, patterns = split_pinned_cities(locations.cities)
        if patterns:
            print(
                "    text match only (not in the city list, so only an exact name in the"
                f" raw location counts): {', '.join(patterns)}"
            )
        if known:
            print(f"    resolved through the city list: {', '.join(known)}")
    print(f"    UK remote included: {'yes' if locations.remote_uk else 'no'}")
    print(f"    UK hybrid included: {'yes' if locations.hybrid_uk else 'no'}")
    return EXIT_OK


def _config_validate(args: argparse.Namespace) -> int:
    try:
        app = _load(args.config)
    except ConfigError as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return EXIT_FAILED

    config_path = app.config_path if app.config_path else "none (built-in defaults)"
    print(f"Config: {config_path}")
    print(f"Sources: {len(app.sources)} configured, {len(app.enabled_sources())} enabled")

    problems = _config_problems(app)
    if not problems:
        print("Result: OK - the configuration loaded and every value can take effect")
        return EXIT_OK
    for line in problems:
        print(f"warning: {line}", file=sys.stderr)
    print(f"Result: OK with {len(problems)} warning(s)", file=sys.stderr)
    return EXIT_OK


def _config_problems(app: AppConfig) -> list[str]:
    """Checks that a successful load cannot catch: values that would never take effect."""
    problems: list[str] = []
    for source in app.sources:
        try:
            get_adapter(source.adapter)
        except AdapterError as exc:
            problems.append(f"source '{source.id}': {exc}")
    employers: dict[str, str] = {}
    for source in app.enabled_sources():
        other = employers.setdefault(source.employer.casefold(), source.id)
        if other != source.id:
            problems.append(
                f"sources '{other}' and '{source.id}' both monitor '{source.employer}';"
                " the same role can then alert twice"
            )
    _, patterns = split_pinned_cities(app.matching.locations.cities)
    for city in patterns:
        problems.append(
            f"pinned city '{city}' is not in the location list, so it only matches when the"
            " source spells it exactly; see docs/configuration.md"
        )
    return problems


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


def _print_scan_outcome(
    outcome: ScanOutcome,
    source: SourceConfig,
    db_path: Path,
    *,
    delivering: bool = False,
) -> None:
    status_line = "complete" if outcome.complete else "incomplete"
    mode = "delivering via Telegram" if delivering else "dry run, no notifications"
    print(f"Scan #{outcome.scan_id} - {source.id} ({source.employer}) - {mode}")
    print(f"Database: {db_path}")
    print(
        f"Status: {outcome.status} ({status_line})"
        f"  records: seen={outcome.records_seen} parsed={outcome.records_parsed}"
        f" skipped={outcome.skipped_count}"
    )
    if outcome.error_summary:
        print(f"Note: {outcome.error_summary}")
    if outcome.observed_only_jobs:
        print(f"Unchanged roles: {outcome.observed_only_jobs} observed without re-reading details")

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
    if state.next_run_at:
        print(f"  Next run: {state.next_run_at}")
    if state.failure_notice_at:
        attempts = (
            f", attempt {state.failure_notice_attempts}"
            if state.failure_notice_attempts > 1
            else ""
        )
        print(
            f"  Failure notice: {state.failure_notice_outcome or 'pending'}"
            f" at {state.failure_notice_at}{attempts}"
        )
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
