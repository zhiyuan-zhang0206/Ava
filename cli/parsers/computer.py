"""`ava computer` — computer-use daemon operator verbs.

Builder plus its `_h_*` handler; the handler lazy-imports the implementation
from ``cli.commands`` so parser building never loads Settings (see the
``cli.main`` module docstring).
"""

from __future__ import annotations

import argparse


def _h_computer_release(args: argparse.Namespace) -> int:
    from cli.commands.computer import h_computer_release

    return h_computer_release(args)


def _add_computer_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import _h_computer_release

    # `ava computer` — operator commands for the computer-use daemon (the
    # per-machine MCP service that drives the shared desktop).
    computer_p = sub.add_parser(
        "computer",
        help="computer-use daemon operator commands (release the shared screen)",
    )
    computer_sub = computer_p.add_subparsers(dest="computer_cmd", required=True)

    release_p = computer_sub.add_parser(
        "release",
        help="force-release the screen from its current holder "
        "(operator kick; the next FIFO waiter takes over)",
    )
    release_p.set_defaults(func=_h_computer_release)
