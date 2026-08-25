"""`ava logs` local log-maintenance parser."""

from __future__ import annotations

import argparse

_FAMILY_DAYS_NAMES = frozenset({"agent", "shell", "gateway", "ops", "watchdog", "other", "default"})


def _positive_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("days must be a positive integer") from exc
    if days <= 0:
        raise argparse.ArgumentTypeError("days must be a positive integer")
    return days


def _family_days(value: str) -> dict[str, int]:
    family_days: dict[str, int] = {}
    for assignment in value.split(","):
        family, separator, days = assignment.partition("=")
        if not separator or not family or not days:
            raise argparse.ArgumentTypeError(
                "family days must use FAMILY=DAYS pairs separated by commas"
            )
        if family not in _FAMILY_DAYS_NAMES:
            raise argparse.ArgumentTypeError(
                "family must be agent, shell, gateway, ops, watchdog, other, or default"
            )
        if family == "default":
            family = "other"
        if family in family_days:
            raise argparse.ArgumentTypeError(f"family {family!r} is repeated")
        family_days[family] = _positive_days(days)
    return family_days


def _h_logs_retention(args: argparse.Namespace) -> int:
    from cli.commands import cmd_logs_retention

    return cmd_logs_retention(
        older_than_days=args.older_than,
        family_days=args.family_days,
        dry_run=args.dry_run,
    )


def _add_logs_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import _h_logs_retention

    logs_p = sub.add_parser("logs", help="manage local log files")
    logs_sub = logs_p.add_subparsers(dest="logs_cmd", required=True)
    retention_p = logs_sub.add_parser(
        "retention",
        help="delete expired agent, shell, and rotated service logs",
        description=(
            "Delete expired managed files from the root of $AVA_HOME/logs while "
            "preserving active open files."
        ),
    )
    age_mode = retention_p.add_mutually_exclusive_group()
    age_mode.add_argument(
        "--older-than",
        type=_positive_days,
        default=None,
        metavar="DAYS",
        help=(
            "positive age threshold in days (default: AVA_LOG_RETENTION_DAYS, otherwise 14 days)"
        ),
    )
    age_mode.add_argument(
        "--family-days",
        type=_family_days,
        default=None,
        metavar="FAMILY=DAYS,...",
        help=(
            "comma-separated retention overrides; mutually exclusive with --older-than. "
            "Tier defaults: agent=15, shell=7, gateway=30, ops=30, watchdog=30, other=3 "
            "days (the no-flag fallback remains AVA_LOG_RETENTION_DAYS, otherwise 14 days)"
        ),
    )
    retention_p.add_argument(
        "--dry-run",
        action="store_true",
        help="list matching expired files and totals without deleting",
    )
    retention_p.set_defaults(func=_h_logs_retention)
