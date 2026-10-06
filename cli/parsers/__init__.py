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
- ``backup`` — scheduled backup operation custody
- ``plugins`` — plugins + skill
- ``mcp`` — mcp + memory initialization, refresh, and search
- ``management`` — config + presets + schedules
"""

from __future__ import annotations

import argparse

from cli.commands.agents.parsers import add_agents_parser
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
from cli.parsers.impersonation import _add_impersonation_parser
from cli.parsers.logs import _add_logs_parser
from cli.parsers.management import (
    _add_config_parser,
    _add_presets_parser,
    _add_schedules_parser,
)
from cli.parsers.mcp import _add_mcp_parser, _add_memory_parser
from cli.parsers.packages import _add_packages_parser
from cli.parsers.plugins import _add_plugins_parser, _add_skill_parser
from cli.parsers.pty import _add_pty_parser


def build_parser() -> argparse.ArgumentParser:
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
    _add_start_parser(sub)
    _add_stop_parser(sub)
    _add_restart_parser(sub)
    _add_status_parser(sub)
    _add_pty_parser(sub)
    _add_converge_parser(sub)
    _add_firewall_parser(sub)
    _add_lgtm_parser(sub)
    _add_cluster_parser(sub)
    _add_computer_parser(sub)
    _add_trace_parser(sub)
    _add_logs_parser(sub)
    _add_backup_parser(sub)
    add_agents_parser(sub)
    _add_impersonation_parser(sub)
    _add_config_parser(sub)
    _add_presets_parser(sub)
    _add_schedules_parser(sub)
    _add_plugins_parser(sub)
    _add_skill_parser(sub)
    _add_mcp_parser(sub)
    _add_memory_parser(sub)
    _add_packages_parser(sub)

    return parser
