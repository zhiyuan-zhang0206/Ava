"""`ava packages` — the per-machine package refresh surface.

Builders plus their `_h_*` handlers. Handlers lazy-import their `cmd_*`
implementation from ``cli.commands`` so parser building never loads Settings
(see ``cli.main`` module docstring)."""

from __future__ import annotations

import argparse


def _h_packages_status(args: argparse.Namespace) -> int:
    from cli.commands import cmd_packages_status

    return cmd_packages_status(json_output=args.json_output)


def _add_packages_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import _h_packages_status

    packages_p = sub.add_parser(
        "packages",
        help="inspect per-package update policy, channels, and refresh state (this machine)",
    )
    packages_sub = packages_p.add_subparsers(dest="packages_cmd", required=True)

    status_p = packages_sub.add_parser(
        "status",
        help="host version + per-package channel / policy / applied rev / last result",
    )
    status_p.add_argument(
        "--json", action="store_true", dest="json_output", help="machine-readable output"
    )
    status_p.set_defaults(func=_h_packages_status)
