"""Explicit host-local maintenance; fleet transport belongs to the operator."""

from __future__ import annotations

import argparse


def _handle(args: argparse.Namespace) -> int:
    from cli.commands._maintenance import run

    return run(args)


def _add_maintenance_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        "maintenance",
        help="[host] hold, drain and stop this unit without force; coordinate other hosts explicitly",
    )
    verbs = parser.add_subparsers(dest="maintenance_cmd", required=True)
    for verb in ("prepare", "status", "drain", "stop", "start", "resume", "stop-data-plane"):
        command = verbs.add_parser(verb, help=f"[host] {verb} this unit's maintenance operation")
        command.set_defaults(func=_handle)
        if verb != "status":
            command.add_argument(
                "--operation", required=True, help="same holder on every participating unit"
            )
            command.add_argument(
                "--acquired-at", required=True, help="same timezone-aware operation timestamp"
            )
        if verb in ("drain", "stop", "stop-data-plane"):
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
                help="explicitly abandon an unfinished drain and restore ordinary lifecycle recovery",
            )
