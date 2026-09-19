"""`ava agents` + `ava notices` — agent lifecycle and the notification queue.

Thin clients over the gateway's /api/agents + /api/notices surfaces: builders
plus their `_h_*` handlers. Handlers lazy-import their `cmd_*` implementation
from ``cli.commands.agents`` / ``cli.commands.notices`` so parser building
never loads Settings (see ``cli.main`` module docstring)."""

from __future__ import annotations

import argparse
import sys


def _validated_source(value: str) -> str:
    """Argparse type for `--source`: reject an unknown source before any command runs."""
    from shared.envelope import validate_source

    try:
        validate_source(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


def _h_agents_ls(args: argparse.Namespace) -> int:
    from cli.commands.agents import cmd_agents_ls

    return cmd_agents_ls(
        scope=args.scope, query=args.query, before_id=args.before_id, limit=args.limit
    )


def _h_agents_timeline(args: argparse.Namespace) -> int:
    from cli.commands.agent_timeline import cmd_agents_timeline

    return cmd_agents_timeline(args.agent_id, args.limit, args.before)


def _h_agents_send(args: argparse.Namespace) -> int:
    from cli.commands.agents import ProvenanceError, cmd_agents_send

    try:
        return cmd_agents_send(args.agent_id, args.content, args.source, args.tail_file)
    except ProvenanceError as exc:
        print(f"ava: {exc}", file=sys.stderr)
        return 2


def _h_agents_cancel(args: argparse.Namespace) -> int:
    from cli.commands.agents import cmd_agents_cancel

    return cmd_agents_cancel(args.agent_id)


def _h_agents_restart(args: argparse.Namespace) -> int:
    from cli.commands.agents import ProvenanceError, cmd_agents_restart

    try:
        return cmd_agents_restart(args.agent_id, args.config, source=args.source)
    except ProvenanceError as exc:
        print(f"ava: {exc}", file=sys.stderr)
        return 2


def _h_agents_resurrect(args: argparse.Namespace) -> int:
    from cli.commands.agents import ProvenanceError, cmd_agents_resurrect

    try:
        return cmd_agents_resurrect(args.agent_id, source=args.source)
    except ProvenanceError as exc:
        print(f"ava: {exc}", file=sys.stderr)
        return 2


def _h_agents_resurrect_billing(args: argparse.Namespace) -> int:
    from cli.commands.agents import cmd_agents_resurrect_billing

    return cmd_agents_resurrect_billing(execute=args.execute)


def _h_agents_terminate(args: argparse.Namespace) -> int:
    from cli.commands.agents import ProvenanceError, cmd_agents_terminate

    try:
        return cmd_agents_terminate(args.agent_id, source=args.source, final=args.final)
    except ProvenanceError as exc:
        print(f"ava: {exc}", file=sys.stderr)
        return 2


def _h_agents_kill(args: argparse.Namespace) -> int:
    from cli.commands.agents import ProvenanceError, cmd_agents_kill

    try:
        return cmd_agents_kill(args.agent_id, source=args.source, final=args.final)
    except ProvenanceError as exc:
        print(f"ava: {exc}", file=sys.stderr)
        return 2


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


def _add_timeline_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import _h_agents_timeline

    parser = sub.add_parser(
        "timeline", aliases=["context"], help="read standing context and recent conversation"
    )
    parser.add_argument("agent_id", type=int)
    parser.add_argument(
        "--limit", type=int, default=None, help="recent items, 1..1000 (default: configured window)"
    )
    parser.add_argument("--before", help="exclusive item_id cursor for older history")
    parser.set_defaults(func=_h_agents_timeline)


def _add_list_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import _h_agents_ls

    agents_ls_p = sub.add_parser(
        "ls", help="read one agent directory page (live agents by default)"
    )
    agents_ls_p.add_argument("--scope", choices=("live", "terminated", "all"), default="live")
    agents_ls_p.add_argument("--query", default="", help="search agent IDs and labels")
    agents_ls_p.add_argument("--before-id", type=int, help="exclusive cursor from a prior page")
    agents_ls_p.add_argument("--limit", type=int, default=100, help="page size, 1..200")
    agents_ls_p.set_defaults(func=_h_agents_ls)


def _add_resurrect_billing_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    from cli.main import _h_agents_resurrect_billing

    agents_resurrect_billing_p = sub.add_parser(
        "resurrect-billing",
        help="batch-resurrect the billing-class halt victims once the provider balance recovered "
        "(dry-run unless --execute)",
    )
    agents_resurrect_billing_p.add_argument(
        "--execute",
        action="store_true",
        help="perform the batch; without it only the read-only preview is printed",
    )
    agents_resurrect_billing_p.set_defaults(func=_h_agents_resurrect_billing)


def _add_agents_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import (
        _h_agents_cancel,
        _h_agents_kill,
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

    _add_list_parser(agents_sub)

    _add_timeline_parser(agents_sub)

    agents_send_p = agents_sub.add_parser(
        "send", help="deliver a chat message to an agent (auto-resurrects a terminated target)"
    )
    agents_send_p.add_argument("agent_id", type=int, help="target agent id")
    agents_send_p.add_argument("content", help="message text")
    agents_send_p.add_argument(
        "--source",
        default=None,
        required=True,
        type=_validated_source,
        help="provenance of the message (required). 'user' sends as the human operator; "
        "'shell:N' / 'watcher:N' mark a machine notice; 'schedule:N' a gateway schedule",
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

    _add_resurrect_billing_parser(agents_sub)

    agents_terminate_p = agents_sub.add_parser(
        "terminate", help="stop the agent gracefully (it exits after its current turn)"
    )
    agents_terminate_p.add_argument("agent_id", type=int, help="agent id to terminate")
    agents_terminate_p.add_argument(
        "--final",
        action="store_true",
        help="also close the agent: never auto-resurrect (resurrect reopens it)",
    )
    agents_terminate_p.set_defaults(func=_h_agents_terminate)

    agents_kill_p = agents_sub.add_parser(
        "kill", help="hard-stop a stuck agent (kill the process + mark terminated)"
    )
    agents_kill_p.add_argument("agent_id", type=int, help="agent id to kill")
    agents_kill_p.add_argument(
        "--final",
        action="store_true",
        help="also close the agent: never auto-resurrect (resurrect reopens it)",
    )
    agents_kill_p.set_defaults(func=_h_agents_kill)

    for lifecycle_parser in (
        agents_restart_p,
        agents_resurrect_p,
        agents_terminate_p,
        agents_kill_p,
    ):
        lifecycle_parser.add_argument(
            "--source",
            default=None,
            type=_validated_source,
            help="explicit provenance (or AVA_CALLER_IDENTITY); 'user' = the human operator; "
            "never an authorization grant",
        )

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
