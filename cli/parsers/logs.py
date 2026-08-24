"""`ava logs` local log-maintenance parser."""

from __future__ import annotations

import argparse


def _positive_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("days must be a positive integer") from exc
    if days <= 0:
        raise argparse.ArgumentTypeError("days must be a positive integer")
    return days


def _h_logs_retention(args: argparse.Namespace) -> int:
    from cli.commands import cmd_logs_retention

    return cmd_logs_retention(older_than_days=args.older_than, dry_run=args.dry_run)


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
    retention_p.add_argument(
        "--older-than",
        type=_positive_days,
        default=None,
        metavar="DAYS",
        help=(
            "positive age threshold in days (default: AVA_LOG_RETENTION_DAYS, otherwise 14 days)"
        ),
    )
    retention_p.add_argument(
        "--dry-run",
        action="store_true",
        help="list matching expired files and totals without deleting",
    )
    retention_p.set_defaults(func=_h_logs_retention)
