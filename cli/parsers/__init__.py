"""`ava` CLI argparse surface — per-domain parser builders + their `_h_*` handlers.

`cli.main` imports this package at module level (settings-free: nothing here
imports runtime command modules / ``base.config``, so ``ava --help`` builds the tree
on a host with no .env) and calls :func:`build_parser` from ``main()``. Each
builder binds its own module's handler directly (``set_defaults(func=_h_x)``);
a test that fakes a handler patches the parser module that defines it, before
``build_parser()`` runs. Handlers lazy-import their ``cmd_*`` implementation so
the Settings load stays deferred to dispatch time — see the ``cli.main``
module docstring.

One module per domain:

- ``host`` — init/start/stop/restart/status/converge/firewall/trace
- ``cluster`` — the whole-cluster verbs
- ``cli.commands.agents.parsers`` — agents + notices, beside their implementations
- ``cli.commands.agents.impersonation_parsers`` — external sessions and relays
- ``backup`` — WAL-G physical backups
- ``cli.commands.extensions.parsers`` — plugins, skill, MCP, memory and packages
- ``management`` — config + presets + schedules
"""

from __future__ import annotations

import argparse
import subprocess
from typing import cast

from cli.commands.agents.impersonation_parsers import add_impersonation_parser
from cli.commands.agents.parsers import add_agents_parser
from cli.commands.extensions.parsers.mcp import add_mcp_parser, add_memory_parser
from cli.commands.extensions.parsers.packages import add_packages_parser
from cli.commands.extensions.parsers.plugins import add_plugins_parser, add_skill_parser
from cli.parsers.backup import _add_backup_parser
from cli.parsers.cluster import _add_cluster_parser
from cli.parsers.computer import _add_computer_parser
from cli.parsers.host import (
    _add_converge_parser,
    _add_firewall_parser,
    _add_init_parser,
    _add_lgtm_parser,
    _add_restart_parser,
    _add_start_parser,
    _add_status_parser,
    _add_stop_parser,
    _add_trace_parser,
)
from cli.parsers.logs import _add_logs_parser
from cli.parsers.management import (
    _add_config_parser,
    _add_presets_parser,
    _add_schedules_parser,
)
from cli.parsers.pty import _add_pty_parser

__all__ = ["build_parser", "command_options", "parse_args"]


def build_parser(
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> argparse.ArgumentParser:
    """Build the full `ava` argparse tree; each subparser binds ``func=`` here.

    The registration order below IS the `ava --help` listing order — keep it in
    sync with the command surface.
    """
    parser = argparse.ArgumentParser(
        prog="ava",
        description="Ava cluster ops: native pg/redis + service sessions + cron healthchecks.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    _add_init_parser(sub)
    _add_start_parser(sub, retained_children=retained_children)
    _add_stop_parser(sub, retained_children=retained_children)
    _add_restart_parser(sub, retained_children=retained_children)
    _add_status_parser(sub)
    _add_pty_parser(sub)
    _add_converge_parser(sub)
    _add_firewall_parser(sub)
    _add_lgtm_parser(sub, retained_children=retained_children)
    _add_cluster_parser(sub)
    _add_computer_parser(sub)
    _add_trace_parser(sub)
    _add_logs_parser(sub)
    _add_backup_parser(sub)
    add_agents_parser(sub)
    add_impersonation_parser(sub)
    _add_config_parser(sub)
    _add_presets_parser(sub)
    _add_schedules_parser(sub)
    add_plugins_parser(sub)
    add_skill_parser(sub)
    add_mcp_parser(sub)
    add_memory_parser(sub)
    add_packages_parser(sub)

    return parser


def parse_args(
    argv: list[str] | None = None,
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> argparse.Namespace:
    """Parse the operator command without dispatch or Settings construction.

    None reads sys.argv as argparse does. Help and invalid arguments retain
    argparse's output and SystemExit codes. Lifecycle handlers keep the supplied
    child-owner list when the caller later dispatches the returned namespace.
    """
    return build_parser(retained_children=retained_children).parse_args(argv)


def command_options() -> dict[tuple[str, ...], set[str]]:
    """Long options accepted at each command path, including the `ava` root.

    Aliases have their own paths. Options belong only to the parser accepting
    them, so a start option never certifies the same spelling on stop. Return a
    fresh snapshot for documentation checks without loading command runtimes.
    """
    root = build_parser()
    by_path: dict[tuple[str, ...], set[str]] = {}
    pending: list[tuple[tuple[str, ...], argparse.ArgumentParser]] = [(("ava",), root)]
    while pending:
        path, parser = pending.pop()
        by_path[path] = {
            option
            for action in parser._actions
            for option in action.option_strings
            if option.startswith("--")
        }
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                subcommands = cast("argparse._SubParsersAction[argparse.ArgumentParser]", action)
                pending.extend(
                    ((*path, name), child) for name, child in subcommands.choices.items()
                )
    return by_path
