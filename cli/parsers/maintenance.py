"""Explicit host-local maintenance; fleet transport belongs to the operator."""

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
    from cli.commands._maintenance import run

    return run(args)


def _add_maintenance_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        "maintenance",
        help="[host] hold, drain and stop this unit without force; coordinate other hosts explicitly",
    )
    verbs = parser.add_subparsers(dest="maintenance_cmd", required=True)
    for verb in (
        "prepare",
        "status",
        "drain",
        "stop",
        "start",
        "resume",
        "repair",
        "stop-data-plane",
    ):
        command = verbs.add_parser(verb, help=f"[host] {verb} this unit's maintenance operation")
        command.set_defaults(func=_handle)
        if verb != "status":
            command.add_argument(
                "--operation",
                required=True,
                type=_operation_arg,
                help="same holder on every participating unit",
            )
            command.add_argument(
                "--acquired-at",
                required=True,
                type=_acquired_at_arg,
                help="same timezone-aware operation timestamp",
            )
        if verb in ("drain", "stop", "stop-data-plane"):
            # task #4092 cli-default inventory: total wait — the wait never forces,
            # and a slow drain raises it per invocation instead of parking forever.
            command.add_argument(
                "--timeout",
                type=float,
                default=300,
                help="total wait; timeout retains hold and never forces",
            )
        if verb in ("stop", "stop-data-plane"):
            command.add_argument(
                "--keep-terminals",
                action="store_true",
                help="preserve terminals; operator must separately verify their business work has stopped",
            )
            command.add_argument(
                "--gateway-last",
                action="store_true",
                help="operator assertion that all remote units were verified stopped; does not probe them",
            )
        if verb == "resume":
            command.add_argument(
                "--cancel",
                action="store_true",
                help="explicitly abandon an unfinished drain and restore ordinary lifecycle "
                "recovery; restarts already issued are not retracted and still complete "
                "on next admission",
            )
        if verb == "repair":
            command.add_argument(
                "--operator",
                default=None,
                help="operator identity recorded in the repair audit (e.g. 'Ava #1234'); "
                "defaults to the OS login identity",
            )
