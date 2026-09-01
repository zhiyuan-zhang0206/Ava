"""`ava config` + `ava presets` + `ava schedules` — gateway-API management verbs.

Builders plus their `_h_*` handlers; `_add_script_args` is the shared
`--script` / `--script-file` pair used by `schedules create` and `update`.
Handlers lazy-import their `cmd_*` implementation from ``cli.commands`` so
parser building never loads Settings (see ``cli.main`` module docstring)."""

from __future__ import annotations

import argparse


def _h_config_get(args: argparse.Namespace) -> int:
    from cli.commands.config import h_config_get

    return h_config_get(args)


def _h_config_set(args: argparse.Namespace) -> int:
    from cli.commands.config import h_config_set

    return h_config_set(args)


def _h_config_unset(args: argparse.Namespace) -> int:
    from cli.commands.config import h_config_unset

    return h_config_unset(args)


def _h_presets_ls(_args: argparse.Namespace) -> int:
    from cli.commands.presets import h_presets_ls

    return h_presets_ls(_args)


def _h_presets_get(args: argparse.Namespace) -> int:
    from cli.commands.presets import h_presets_get

    return h_presets_get(args)


def _h_presets_create(args: argparse.Namespace) -> int:
    from cli.commands.presets import h_presets_create

    return h_presets_create(args)


def _h_presets_update(args: argparse.Namespace) -> int:
    from cli.commands.presets import h_presets_update

    return h_presets_update(args)


def _h_presets_delete(args: argparse.Namespace) -> int:
    from cli.commands.presets import h_presets_delete

    return h_presets_delete(args)


def _h_schedules_ls(args: argparse.Namespace) -> int:
    from cli.commands.schedules import h_schedules_ls

    return h_schedules_ls(args)


def _h_schedules_get(args: argparse.Namespace) -> int:
    from cli.commands.schedules import h_schedules_get

    return h_schedules_get(args)


def _h_schedules_create(args: argparse.Namespace) -> int:
    from cli.commands.schedules import h_schedules_create

    return h_schedules_create(args)


def _h_schedules_update(args: argparse.Namespace) -> int:
    from cli.commands.schedules import h_schedules_update

    return h_schedules_update(args)


def _h_schedules_delete(args: argparse.Namespace) -> int:
    from cli.commands.schedules import h_schedules_delete

    return h_schedules_delete(args)


def _h_schedules_start(args: argparse.Namespace) -> int:
    from cli.commands.schedules import h_schedules_start

    return h_schedules_start(args)


def _h_schedules_stop(args: argparse.Namespace) -> int:
    from cli.commands.schedules import h_schedules_stop

    return h_schedules_stop(args)


def _h_schedules_restart(args: argparse.Namespace) -> int:
    from cli.commands.schedules import h_schedules_restart

    return h_schedules_restart(args)


def _h_schedules_logs(args: argparse.Namespace) -> int:
    from cli.commands.schedules import h_schedules_logs

    return h_schedules_logs(args)


def _h_schedules_runs(args: argparse.Namespace) -> int:
    from cli.commands.schedules import h_schedules_runs

    return h_schedules_runs(args)


def _h_schedules_provision(args: argparse.Namespace) -> int:
    from cli.commands.schedules import h_schedules_provision

    return h_schedules_provision(args)


def _add_config_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import _h_config_get, _h_config_set, _h_config_unset

    # `ava config` — read / set / unset cluster + host config via the gateway. The
    # handlers defer the cli.commands.config import (which loads Settings) so
    # `ava --help` builds the parser without a configured .env.
    config_p = sub.add_parser(
        "config",
        help="read/set/unset cluster + host config (writes the gateway/host .env via the gateway)",
    )
    config_sub = config_p.add_subparsers(dest="config_cmd", required=True)

    get_p = config_sub.add_parser("get", help="print all config fields, or one field's value")
    get_p.add_argument("key", nargs="?", default=None, help="env-var or field name (optional)")
    get_p.add_argument("--machine", default=None, help="target agent-runner (host fields)")
    get_p.add_argument("--local", action="store_true", help="read this unit's .env directly")
    get_p.set_defaults(func=_h_config_get)

    set_p = config_sub.add_parser("set", help="set one or more KEY=VALUE config fields")
    set_p.add_argument(
        "assignments", nargs="+", metavar="KEY=VALUE", help="e.g. DEEPSEEK_API_KEY=sk-..."
    )
    set_p.add_argument("--machine", default=None, help="target agent-runner (host fields)")
    set_p.add_argument("--local", action="store_true", help="edit this unit's .env directly")
    set_p.set_defaults(func=_h_config_set)

    unset_p = config_sub.add_parser(
        "unset", help="revert one or more fields to their default (drop from .env)"
    )
    unset_p.add_argument("keys", nargs="+", help="env-var or field name(s)")
    unset_p.add_argument("--machine", default=None, help="target agent-runner (host fields)")
    unset_p.add_argument("--local", action="store_true", help="edit this unit's .env directly")
    unset_p.set_defaults(func=_h_config_unset)


def _add_presets_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import (
        _h_presets_create,
        _h_presets_delete,
        _h_presets_get,
        _h_presets_ls,
        _h_presets_update,
    )

    # `ava presets` — manage agent config presets (thin client over /api/presets).
    presets_p = sub.add_parser(
        "presets",
        help="manage agent config presets (list / get / create / update / delete)",
    )
    presets_sub = presets_p.add_subparsers(dest="presets_cmd", required=True)

    presets_ls_p = presets_sub.add_parser("ls", help="list all presets")
    presets_ls_p.set_defaults(func=_h_presets_ls)

    presets_get_p = presets_sub.add_parser("get", help="show one preset (by name or id)")
    presets_get_p.add_argument("identifier", help="preset name or numeric id")
    presets_get_p.set_defaults(func=_h_presets_get)

    presets_create_p = presets_sub.add_parser("create", help="create a preset")
    presets_create_p.add_argument("--name", required=True, help="unique preset name (kebab-case)")
    presets_create_p.add_argument("--label", required=True, help="human-readable label")
    presets_create_p.add_argument("--description", default=None, help="what this preset is for")
    presets_create_p.add_argument(
        "--config", default=None, help='config overlay as JSON (e.g. {"llm_model":"..."})'
    )
    presets_create_p.set_defaults(func=_h_presets_create)

    presets_update_p = presets_sub.add_parser(
        "update", help="partial-update a preset (by name or id) — only passed fields change"
    )
    presets_update_p.add_argument("identifier", help="preset name or numeric id")
    presets_update_p.add_argument("--name", default=None, help="new unique name")
    presets_update_p.add_argument("--label", default=None, help="new label")
    presets_update_p.add_argument("--description", default=None, help="new description")
    presets_update_p.add_argument("--config", default=None, help="new config overlay as JSON")
    presets_update_p.set_defaults(func=_h_presets_update)

    presets_delete_p = presets_sub.add_parser("delete", help="delete a preset (by name or id)")
    presets_delete_p.add_argument("identifier", help="preset name or numeric id")
    presets_delete_p.add_argument(
        "--force", "-f", action="store_true", help="skip the confirmation prompt"
    )
    presets_delete_p.set_defaults(func=_h_presets_delete)


def _add_schedules_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import (
        _h_schedules_create,
        _h_schedules_delete,
        _h_schedules_get,
        _h_schedules_logs,
        _h_schedules_ls,
        _h_schedules_provision,
        _h_schedules_restart,
        _h_schedules_runs,
        _h_schedules_start,
        _h_schedules_stop,
        _h_schedules_update,
    )

    # `ava schedules` — manage gateway-supervised schedules (thin client over
    # /api/schedules). Every verb takes a name or a numeric id.
    schedules_p = sub.add_parser(
        "schedules",
        help="manage gateway-supervised schedules "
        "(ls / get / create / update / delete / provision / start / stop / restart / logs / runs)",
    )
    schedules_sub = schedules_p.add_subparsers(dest="schedules_cmd", required=True)

    schedules_ls_p = schedules_sub.add_parser("ls", help="list all schedules")
    schedules_ls_p.set_defaults(func=_h_schedules_ls)

    schedules_get_p = schedules_sub.add_parser(
        "get", help="show one schedule with its script (by name or id)"
    )
    schedules_get_p.add_argument("identifier", help="schedule name or numeric id")
    schedules_get_p.set_defaults(func=_h_schedules_get)

    schedules_provision_p = schedules_sub.add_parser(
        "provision",
        help="create the built-in schedules missing from this cluster (per "
        "schedules/manifest.json; product enabled, operator disabled)",
    )
    schedules_provision_p.set_defaults(func=_h_schedules_provision)

    schedules_create_p = schedules_sub.add_parser("create", help="create a schedule")
    schedules_create_p.add_argument("--name", required=True, help="unique schedule name")
    _add_script_args(schedules_create_p)
    schedules_create_p.add_argument(
        "--command", default=None, help="command that runs the script (default: python schedule.py)"
    )
    schedules_create_p.add_argument("--description", default=None, help="what this schedule does")
    schedules_create_p.add_argument(
        "--disabled",
        action="store_true",
        help="create without launching it (enabled by default)",
    )
    schedules_create_p.set_defaults(func=_h_schedules_create)

    schedules_update_p = schedules_sub.add_parser(
        "update", help="partial-update a schedule (by name or id) — only passed fields change"
    )
    schedules_update_p.add_argument("identifier", help="schedule name or numeric id")
    schedules_update_p.add_argument("--name", default=None, help="new unique name")
    _add_script_args(schedules_update_p)
    schedules_update_p.add_argument("--command", default=None, help="new run command")
    schedules_update_p.add_argument("--description", default=None, help="new description")
    enabled_group = schedules_update_p.add_mutually_exclusive_group()
    enabled_group.add_argument(
        "--enable", action="store_true", help="set enabled=true (does not force a relaunch)"
    )
    enabled_group.add_argument("--disable", action="store_true", help="set enabled=false")
    schedules_update_p.set_defaults(func=_h_schedules_update)

    schedules_delete_p = schedules_sub.add_parser(
        "delete", help="delete a schedule (by name or id)"
    )
    schedules_delete_p.add_argument("identifier", help="schedule name or numeric id")
    schedules_delete_p.add_argument(
        "--force", "-f", action="store_true", help="skip the confirmation prompt"
    )
    schedules_delete_p.set_defaults(func=_h_schedules_delete)

    for verb, helptext, handler in (
        ("start", "enable + launch the schedule now", _h_schedules_start),
        ("stop", "disable + kill the schedule's session now", _h_schedules_stop),
        ("restart", "relaunch the session on the current script", _h_schedules_restart),
    ):
        control_p = schedules_sub.add_parser(verb, help=helptext)
        control_p.add_argument("identifier", help="schedule name or numeric id")
        control_p.set_defaults(func=handler)

    schedules_logs_p = schedules_sub.add_parser(
        "logs", help="recent output (live session scrollback, else the last crash traceback)"
    )
    schedules_logs_p.add_argument("identifier", help="schedule name or numeric id")
    schedules_logs_p.add_argument(
        "--lines", type=int, default=200, help="how many lines to capture (default 200)"
    )
    schedules_logs_p.set_defaults(func=_h_schedules_logs)

    schedules_runs_p = schedules_sub.add_parser("runs", help="run history, newest first")
    schedules_runs_p.add_argument("identifier", help="schedule name or numeric id")
    schedules_runs_p.add_argument(
        "--limit", type=int, default=50, help="how many rows to show (default 50)"
    )
    schedules_runs_p.set_defaults(func=_h_schedules_runs)


def _add_script_args(parser: argparse.ArgumentParser) -> None:
    """The --script / --script-file pair, shared by `schedules create` and
    `update`. Mutually exclusive; `--script-file -` reads stdin (the heredoc form).
    Neither is `required` here — create enforces exactly-one in the command body,
    update treats both-absent as "leave the script alone"."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--script", default=None, help="script body as a literal string")
    group.add_argument(
        "--script-file", default=None, help="read the script body from PATH ('-' = stdin)"
    )
