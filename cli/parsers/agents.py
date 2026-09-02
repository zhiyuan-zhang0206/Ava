"""`ava agents` + `ava notices` — agent lifecycle and the notification queue.

Thin clients over the gateway's /api/agents + /api/notices surfaces: builders
plus their `_h_*` handlers. Handlers lazy-import their `cmd_*` implementation
from ``cli.commands.agents`` / ``cli.commands.notices`` so parser building
never loads Settings (see ``cli.main`` module docstring)."""

from __future__ import annotations

import argparse


def _h_agents_ls(_args: argparse.Namespace) -> int:
    from cli.commands.agents import cmd_agents_ls

    return cmd_agents_ls()


def _h_agents_send(args: argparse.Namespace) -> int:
    from cli.commands.agents import cmd_agents_send

    return cmd_agents_send(args.agent_id, args.content, args.source, args.tail_file)


def _h_agents_cancel(args: argparse.Namespace) -> int:
    from cli.commands.agents import cmd_agents_cancel

    return cmd_agents_cancel(args.agent_id)


def _h_agents_restart(args: argparse.Namespace) -> int:
    from cli.commands.agents import cmd_agents_restart

    return cmd_agents_restart(args.agent_id, args.config)


def _h_agents_resurrect(args: argparse.Namespace) -> int:
    from cli.commands.agents import cmd_agents_resurrect

    return cmd_agents_resurrect(args.agent_id)


def _h_agents_terminate(args: argparse.Namespace) -> int:
    from cli.commands.agents import cmd_agents_terminate

    return cmd_agents_terminate(args.agent_id)


def _h_agents_kill(args: argparse.Namespace) -> int:
    from cli.commands.agents import cmd_agents_kill

    return cmd_agents_kill(args.agent_id)


def _h_notices_list(args: argparse.Namespace) -> int:
    from cli.commands.notices import cmd_notices_list

    return cmd_notices_list(
        agent_id=args.agent, priority=args.priority, type_filter=args.type, stale=args.stale
    )


def _h_notices_resolve(args: argparse.Namespace) -> int:
    from cli.commands.notices import cmd_notices_resolve

    return cmd_notices_resolve(
        notice_id=args.notice_id,
        agent_id=args.agent,
        action=args.action,
        reply=args.reply,
    )


def _h_notices_clear(args: argparse.Namespace) -> int:
    from cli.commands.notices import cmd_notices_clear

    return cmd_notices_clear(agent_id=args.agent, force=args.force, stale=args.stale)


def _add_agents_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import (
        _h_agents_cancel,
        _h_agents_kill,
        _h_agents_ls,
        _h_agents_restart,
        _h_agents_resurrect,
        _h_agents_send,
        _h_agents_terminate,
        _h_notices_clear,
        _h_notices_list,
        _h_notices_resolve,
    )

    # `ava agents` — operator lifecycle ops (thin client over the gateway's
    # /api/agents + /api/cancel routes). Handlers defer the cli.commands import so
    # `ava --help` builds the parser without a configured .env. Verbs are ordered
    # by escalating force (cancel < restart < terminate < kill) plus ls.
    agents_p = sub.add_parser(
        "agents",
        help="observe + control agents: ls / cancel / restart / terminate / kill",
    )
    agents_sub = agents_p.add_subparsers(dest="agents_cmd", required=True)

    agents_ls_p = agents_sub.add_parser(
        "ls", help="list all agents (id / status / label) via GET /api/agents"
    )
    agents_ls_p.set_defaults(func=_h_agents_ls)

    agents_send_p = agents_sub.add_parser(
        "send", help="deliver a chat message to an agent (auto-resurrects a terminated target)"
    )
    agents_send_p.add_argument("agent_id", type=int, help="target agent id")
    agents_send_p.add_argument("content", help="message text")
    agents_send_p.add_argument(
        "--source",
        required=True,
        help="message provenance (required, no default): shell:N / watcher:N for "
        "generated notices, user for a human operator",
    )
    agents_send_p.add_argument(
        "--tail-file",
        default=None,
        help="append the tail of this file to the message (completion notices "
        "carry the end of the command's output this way)",
    )
    agents_send_p.set_defaults(func=_h_agents_send)

    agents_cancel_p = agents_sub.add_parser(
        "cancel", help="halt the agent's current action; it stays alive (resumable)"
    )
    agents_cancel_p.add_argument("agent_id", type=int, help="agent id to cancel")
    agents_cancel_p.set_defaults(func=_h_agents_cancel)

    agents_restart_p = agents_sub.add_parser(
        "restart", help="restart the agent in place (history preserved)"
    )
    agents_restart_p.add_argument("agent_id", type=int, help="agent id to restart")
    agents_restart_p.add_argument(
        "--config", default=None, help='config overlay as JSON (e.g. {"llm_model":"gpt-5.6-sol"})'
    )
    agents_restart_p.set_defaults(func=_h_agents_restart)

    agents_resurrect_p = agents_sub.add_parser(
        "resurrect", help="bring a terminated agent back (history preserved)"
    )
    agents_resurrect_p.add_argument("agent_id", type=int, help="agent id to resurrect")
    agents_resurrect_p.set_defaults(func=_h_agents_resurrect)

    agents_terminate_p = agents_sub.add_parser(
        "terminate", help="stop the agent gracefully (it exits after its current turn)"
    )
    agents_terminate_p.add_argument("agent_id", type=int, help="agent id to terminate")
    agents_terminate_p.set_defaults(func=_h_agents_terminate)

    agents_kill_p = agents_sub.add_parser(
        "kill", help="hard-stop a stuck agent (kill the process + mark terminated)"
    )
    agents_kill_p.add_argument("agent_id", type=int, help="agent id to kill")
    agents_kill_p.set_defaults(func=_h_agents_kill)

    notices_p = sub.add_parser(
        "notices",
        help="inspect and resolve the agent notification queue (Task #949)",
    )
    notices_sub = notices_p.add_subparsers(dest="notices_cmd", required=True)

    notices_list_p = notices_sub.add_parser(
        "list",
        help="list open notices (both kinds); filter with --agent/--priority/--type",
    )
    notices_list_p.add_argument("--agent", type=int, default=None, help="agent id")
    notices_list_p.add_argument("--priority", default=None, help="P0|P1|P2|P3")
    notices_list_p.add_argument("--type", choices=("fyi", "decision"), default=None)
    notices_list_p.add_argument(
        "--stale", action="store_true", help="only notices of terminated agents"
    )
    notices_list_p.set_defaults(func=_h_notices_list)

    notices_resolve_p = notices_sub.add_parser(
        "resolve",
        help="resolve one notice (answer|read|dismiss)",
    )
    notices_resolve_p.add_argument("notice_id", type=int)
    notices_resolve_p.add_argument("--agent", type=int, required=True)
    notices_resolve_p.add_argument("--action", choices=("answer", "read", "dismiss"), required=True)
    notices_resolve_p.add_argument("--reply", default=None)
    notices_resolve_p.set_defaults(func=_h_notices_resolve)

    notices_clear_p = notices_sub.add_parser(
        "clear",
        help="resolve every open notice of one agent (FYI->read, decision->dismiss)",
    )
    notices_clear_p.add_argument("--agent", type=int, required=True)
    notices_clear_p.add_argument("--force", action="store_true")
    notices_clear_p.add_argument(
        "--stale",
        action="store_true",
        help="clear open notices of terminated agents (not --agent)",
    )
    notices_clear_p.set_defaults(func=_h_notices_clear)
