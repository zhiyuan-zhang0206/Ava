"""`ava` CLI argparse surface — per-domain parser builders + their `_h_*` handlers.

`cli.main` imports this package at module level (settings-free: nothing here
imports ``cli.commands`` / ``shared.config``, so ``ava --help`` builds the tree
on a host with no .env), re-exports every ``_h_*`` handler as the test patch
seam, and calls :func:`build_parser` from ``main()``. Handlers lazy-import
their ``cmd_*`` implementation so the Settings load stays deferred to dispatch
time — see the ``cli.main`` module docstring.

One module per domain:

- ``host`` — start/stop/restart/status/converge/firewall/trace
- ``cluster`` — the whole-cluster verbs
- ``agents`` — agents + notices
- ``plugins`` — plugins + skill
- ``mcp`` — mcp + memory initialization, refresh, and search
- ``management`` — config + presets + schedules
"""

from __future__ import annotations

import argparse

from cli.parsers.agents import _add_agents_parser
from cli.parsers.cluster import _add_cluster_parser
from cli.parsers.computer import _add_computer_parser
from cli.parsers.host import (
    _add_converge_parser,
    _add_firewall_parser,
    _add_lgtm_parser,
    _add_pause_parser,
    _add_restart_parser,
    _add_start_parser,
    _add_status_parser,
    _add_stop_parser,
    _add_trace_parser,
)
from cli.parsers.impersonation import _add_impersonation_parser
from cli.parsers.logs import _add_logs_parser
from cli.parsers.maintenance import _add_maintenance_parser
from cli.parsers.management import (
    _add_config_parser,
    _add_presets_parser,
    _add_schedules_parser,
)
from cli.parsers.mcp import _add_mcp_parser, _add_memory_parser
from cli.parsers.pitr import _add_pitr_parser
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

    _add_start_parser(sub)
    _add_stop_parser(sub)
    _add_pause_parser(sub)
    _add_maintenance_parser(sub)
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
    _add_pitr_parser(sub)
    _add_agents_parser(sub)
    _add_impersonation_parser(sub)
    _add_config_parser(sub)
    _add_presets_parser(sub)
    _add_schedules_parser(sub)
    _add_plugins_parser(sub)
    _add_skill_parser(sub)
    _add_mcp_parser(sub)
    _add_memory_parser(sub)

    return parser
