"""`ava mcp` + `ava memory` — MCP server management and memory-pool operations.

Builders plus their `_h_*` handlers. Handlers lazy-import their `cmd_*`
implementation from ``cli.commands`` / ``cli.mcp_server`` (which pulls in the
mcp SDK no other verb needs) so parser building never loads Settings (see
``cli.main`` module docstring)."""

from __future__ import annotations

import argparse


def _h_mcp_add(args: argparse.Namespace) -> int:
    from cli.commands import cmd_mcp_add

    return cmd_mcp_add(
        name=args.name,
        json_spec=args.json,
        command=args.command,
        args=args.arg,
        env_pairs=args.env,
    )


def _h_mcp_list(_args: argparse.Namespace) -> int:
    from cli.commands import cmd_mcp_list

    return cmd_mcp_list()


def _h_mcp_remove(args: argparse.Namespace) -> int:
    from cli.commands import cmd_mcp_remove

    return cmd_mcp_remove(name=args.name)


def _h_mcp_enable(args: argparse.Namespace) -> int:
    from cli.commands import cmd_mcp_enable

    return cmd_mcp_enable(name=args.name)


def _h_mcp_disable(args: argparse.Namespace) -> int:
    from cli.commands import cmd_mcp_disable

    return cmd_mcp_disable(name=args.name)


def _h_mcp_install(args: argparse.Namespace) -> int:
    from cli.commands import cmd_mcp_install

    return cmd_mcp_install(source=args.source, ref=args.ref, path=args.path, env_pairs=args.env)


def _h_mcp_uninstall(args: argparse.Namespace) -> int:
    from cli.commands import cmd_mcp_uninstall

    return cmd_mcp_uninstall(name=args.name)


def _h_mcp_upgrade(args: argparse.Namespace) -> int:
    from cli.commands import cmd_mcp_upgrade

    return cmd_mcp_upgrade(args.name, force=args.force)


def _h_mcp_serve(_args: argparse.Namespace) -> int:
    # Imported here, like every other handler: `cli.mcp_server` pulls in the mcp
    # SDK, which no other verb needs.
    from cli.mcp_server import cmd_mcp_serve

    return cmd_mcp_serve()


def _h_memory_refresh(_args: argparse.Namespace) -> int:
    from cli.commands.memory import cmd_memory_refresh

    return cmd_memory_refresh()


def _h_memory_init(_args: argparse.Namespace) -> int:
    from cli.commands.memory import cmd_memory_init

    return cmd_memory_init()


def _h_memory_search(args: argparse.Namespace) -> int:
    from cli.commands.memory import cmd_memory_search

    return cmd_memory_search(args.query, limit=args.limit, json_output=args.json)


def _nonempty_memory_query(value: str) -> str:
    if not value:
        raise argparse.ArgumentTypeError("query must be nonempty")
    return value


def _memory_search_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be an integer from 1 to 100") from exc
    if not 1 <= limit <= 100:
        raise argparse.ArgumentTypeError("limit must be from 1 to 100")
    return limit


def _add_mcp_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import (
        _h_mcp_add,
        _h_mcp_disable,
        _h_mcp_enable,
        _h_mcp_install,
        _h_mcp_list,
        _h_mcp_remove,
        _h_mcp_serve,
        _h_mcp_uninstall,
        _h_mcp_upgrade,
    )

    # `ava mcp` — manage MCP servers: install/uninstall out-of-core packages
    # (under $AVA_HOME/mcps/) and edit the machine config ($AVA_HOME/mcp.json,
    # the cross-vendor `mcpServers` shape Claude Code / Codex also consume).
    mcp_p = sub.add_parser("mcp", help="manage MCP servers")
    mcp_sub = mcp_p.add_subparsers(dest="mcp_cmd", required=True)

    # `serve` runs the other direction from the verbs below: those configure
    # servers Ava's agents call out to, this one exposes Ava itself as a server
    # an external agent (Claude Code / Codex) calls in to.
    mcp_serve_p = mcp_sub.add_parser(
        "serve",
        help="run this cluster's control plane as an MCP server (stdio; deprecated "
        "in favor of the gateway /mcp Streamable HTTP endpoint — design task #1212, "
        "behavior unchanged until retirement)",
    )
    mcp_serve_p.set_defaults(func=_h_mcp_serve)

    mcp_install_p = mcp_sub.add_parser(
        "install", help="install a standalone MCP package from a git URL or local dir"
    )
    mcp_install_p.add_argument("source", help="git URL or local directory of the MCP package")
    mcp_install_p.add_argument(
        "--path", default=None, help="subdirectory of the source holding the package"
    )
    mcp_install_p.add_argument(
        "--ref", default=None, help="git tag/commit/branch to pin (git sources)"
    )
    mcp_install_p.add_argument(
        "--env",
        action="append",
        default=[],
        help="KEY=VALUE injected into the installed copy's env, e.g. a bot token "
        "the package itself must not ship (repeatable)",
    )
    mcp_install_p.set_defaults(func=_h_mcp_install)

    mcp_uninstall_p = mcp_sub.add_parser("uninstall", help="remove an installed MCP package")
    mcp_uninstall_p.add_argument("name", help="installed MCP server name to remove")
    mcp_uninstall_p.set_defaults(func=_h_mcp_uninstall)

    mcp_upgrade_p = mcp_sub.add_parser("upgrade", help="re-fetch an installed MCP package")
    mcp_upgrade_p.add_argument("name", help="installed MCP server name to upgrade")
    mcp_upgrade_p.add_argument(
        "--force",
        action="store_true",
        help="overwrite a locally modified copy instead of refusing",
    )
    mcp_upgrade_p.set_defaults(func=_h_mcp_upgrade)

    mcp_add_p = mcp_sub.add_parser(
        "add",
        help="add/replace an MCP server (paste --json '<spec>' from a vendor README, "
        "or build it from --command/--arg/--env)",
    )
    mcp_add_p.add_argument("name", help="server name (key under mcpServers)")
    mcp_add_p.add_argument(
        "--json", default=None, help='server object as JSON, e.g. \'{"command": "npx", ...}\''
    )
    mcp_add_p.add_argument(
        "--command", default=None, help="stdio server command (alternative to --json)"
    )
    mcp_add_p.add_argument(
        "--arg", action="append", default=[], help="argument for --command (repeatable)"
    )
    mcp_add_p.add_argument(
        "--env", action="append", default=[], help="KEY=VALUE env var for --command (repeatable)"
    )
    mcp_add_p.set_defaults(func=_h_mcp_add)

    mcp_list_p = mcp_sub.add_parser("list", aliases=["ls"], help="list the merged MCP server set")
    mcp_list_p.set_defaults(func=_h_mcp_list)

    mcp_remove_p = mcp_sub.add_parser("remove", help="remove a machine-config MCP server")
    mcp_remove_p.add_argument("name", help="server name to remove")
    mcp_remove_p.set_defaults(func=_h_mcp_remove)

    mcp_enable_p = mcp_sub.add_parser("enable", help="enable an MCP server on this machine")
    mcp_enable_p.add_argument("name", help="server name to enable")
    mcp_enable_p.set_defaults(func=_h_mcp_enable)

    mcp_disable_p = mcp_sub.add_parser("disable", help="disable an MCP server on this machine")
    mcp_disable_p.add_argument("name", help="server name to disable")
    mcp_disable_p.set_defaults(func=_h_mcp_disable)


def _add_memory_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """`ava memory` — memory pool operations.

    init          Initialize the memory pool and plugin-owned templates
    refresh       Trigger gateway memory index refresh
    search        Search indexed memory with relative-path results
    """
    from cli.main import _h_memory_init, _h_memory_refresh, _h_memory_search

    memory_p = sub.add_parser("memory", help="memory pool operations")
    memory_sub = memory_p.add_subparsers(dest="memory_cmd", required=True)

    init_p = memory_sub.add_parser("init", help="initialize memory pool resources")
    init_p.set_defaults(func=_h_memory_init)

    refresh_p = memory_sub.add_parser("refresh", help="trigger gateway memory index refresh")
    refresh_p.set_defaults(func=_h_memory_refresh)

    search_p = memory_sub.add_parser("search", help="search indexed memory")
    search_p.add_argument("query", type=_nonempty_memory_query, help="nonempty search query")
    search_p.add_argument(
        "--limit",
        type=_memory_search_limit,
        default=5,
        metavar="K",
        help="maximum result count from 1 to 100 (default: 5)",
    )
    search_p.add_argument(
        "--json",
        action="store_true",
        help="emit the gateway results list as JSON",
    )
    search_p.set_defaults(func=_h_memory_search)
