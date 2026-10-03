"""Host-local maintenance hold exits: read it, repair it, cancel it."""

from __future__ import annotations

import argparse
from datetime import datetime


def _operation_arg(value: str) -> str:
    """Argparse type for `--operation`: nonempty at the parse layer."""
    if not value.strip():
        raise argparse.ArgumentTypeError("operation must be nonempty")
    return value


def _acquired_at_arg(value: str) -> str:
    """Argparse type for `--acquired-at`: an ISO timestamp with an explicit UTC offset."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO-8601 timestamp: {exc}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError(
            "needs an explicit UTC offset, e.g. '2026-09-20 03:00:00+08'"
        )
    return value


def _handle(args: argparse.Namespace) -> int:
    from cli.commands.lifecycle.maintenance import run

    return run(args)


def _add_maintenance_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        "maintenance",
        help="[host] read this unit's maintenance hold, or end one that cannot finish on its own",
    )
    verbs = parser.add_subparsers(dest="maintenance_cmd", required=True)
    status = verbs.add_parser("status", help="[host] print this unit's maintenance hold as JSON")
    status.set_defaults(func=_handle)
    for verb, summary in (
        ("repair", "release a hold latched on failed receipts after the root cause is fixed"),
        ("cancel", "abandon a drain that has not started stopping; services stay as they are"),
    ):
        command = verbs.add_parser(verb, help=f"[host] {summary}")
        command.set_defaults(func=_handle)
        command.add_argument(
            "--operation",
            required=True,
            type=_operation_arg,
            help="the hold's operation, as `ava maintenance status` prints it",
        )
        command.add_argument(
            "--acquired-at",
            required=True,
            type=_acquired_at_arg,
            help="the hold's timezone-aware acquired_at, as `ava maintenance status` prints it",
        )
        if verb == "repair":
            command.add_argument(
                "--operator",
                default=None,
                help="operator identity recorded in the repair audit (e.g. 'Ava #1234'); "
                "defaults to the OS login identity",
            )
