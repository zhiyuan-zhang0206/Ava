"""External controller requests, explicit leases, inboxes and local SDK execution."""

from __future__ import annotations

import argparse
import math
import sys
from functools import partial


def _integer_range(value: str, *, maximum: int, minimum: int = 1) -> int:
    message = f"must be an integer from {minimum} through {maximum}"
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(message) from exc
    if not minimum <= parsed <= maximum:
        raise argparse.ArgumentTypeError(message)
    return parsed


def _seconds_range(value: str, *, maximum: float | None = None) -> float:
    message = (
        "must be finite and nonnegative"
        if maximum is None
        else f"must be finite and from 0 through {maximum:g} seconds"
    )
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(message) from exc
    if not math.isfinite(parsed) or parsed < 0 or (maximum is not None and parsed > maximum):
        raise argparse.ArgumentTypeError(message)
    return parsed


def _nonempty(value: str) -> str:
    """Argparse type: a display name that is not blank."""
    if not value.strip():
        raise argparse.ArgumentTypeError("must be a non-empty name")
    return value


def _relay_spec_problem(
    provider: str, thread_id: str | None, codex_remote: str | None
) -> str | None:
    """CLI-boundary mirror of `shared._impersonation_store.validate_relay_spec`.

    Fails before any command code runs (usage error); the shared validator
    stays in place as the server-side defense.
    """
    if provider == "codex":
        if not thread_id:
            return (
                "the codex relay needs --thread-id — the existing Codex session UUID "
                "it delivers into (optionally with a --codex-remote unix:// or ws:// endpoint)"
            )
        if codex_remote is not None and not codex_remote.startswith(("unix://", "ws://")):
            return "codex remote must be a unix:// or ws:// endpoint"
    elif thread_id is not None or codex_remote is not None:
        return "the claude relay routes to its owner; drop --thread-id/--codex-remote"
    return None


def _h_impersonate(args: argparse.Namespace) -> int:
    from cli.commands.impersonation import cmd_impersonate

    problem: str | None = None
    if args.impersonation_cmd == "request":
        problem = _relay_spec_problem(
            args.relay_provider, args.relay_thread_id, args.relay_codex_remote
        )
    if problem is not None:
        print(f"ava: {problem}", file=sys.stderr)
        return 2
    return cmd_impersonate(args)


def _h_impersonate_relay(args: argparse.Namespace) -> int:
    from cli.commands.impersonation_relay import cmd_relay

    problem = _relay_spec_problem(args.provider, args.thread_id, args.codex_remote)
    if problem is not None:
        print(f"ava: {problem}", file=sys.stderr)
        return 2
    return cmd_relay(args)


def _add_send_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """The attested send verb: one message to another agent as the leased identity
    (task #4102) — the CLI form of the borrow the SDK attachment already stamps."""
    from cli.main import _h_impersonate

    sender = commands.add_parser(
        "send",
        help="send a message to another agent as the leased identity",
    )
    sender.add_argument(
        "session_id",
        type=partial(_integer_range, minimum=0, maximum=2**63 - 1),
        help="active impersonation session id",
    )
    sender.add_argument(
        "--agent",
        dest="agent_id",
        required=True,
        type=int,
        help="the Ava agent this lease replaces (the delivered identity)",
    )
    sender.add_argument(
        "--to", dest="target_agent_id", required=True, type=int, help="target agent id"
    )
    sender.add_argument("--content", required=True, help="message text; '-' reads stdin")
    sender.set_defaults(func=_h_impersonate)


def _add_impersonation_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import _h_impersonate, _h_impersonate_relay

    group = sub.add_parser("impersonate", help="start and use named external sessions")
    commands = group.add_subparsers(dest="impersonation_cmd", required=True)
    request = commands.add_parser(
        "request",
        help="prepare a trusted external takeover",
        description="Request identity authority; start and verify a host relay for inbox wake-up.",
    )
    request.add_argument("--agent", dest="agent_id", required=True, type=int)
    request.add_argument(
        "--as",
        dest="caller",
        required=True,
        type=_nonempty,
        help="free executor name, e.g. Codex: database-work",
    )
    request.add_argument(
        "--name",
        required=True,
        type=_nonempty,
        help="name of this impersonation session",
    )
    request.add_argument(
        "--ttl",
        required=True,
        type=partial(_integer_range, maximum=86400),
        help="lease lifetime in seconds, 1..86400; estimate it short — the recovery deadline",
    )
    request.add_argument("--reason", default="", help="what the external agent will do")
    request.add_argument(
        "--provider",
        dest="relay_provider",
        required=True,
        choices=("codex", "claude"),
        help="relay host for automatic inbox wake-up: codex or claude",
    )
    request.add_argument(
        "--thread-id",
        dest="relay_thread_id",
        help="existing Codex session UUID the relay delivers into (codex only)",
    )
    request.add_argument(
        "--codex-remote",
        dest="relay_codex_remote",
        help="Codex app-server endpoint owning that session, e.g. unix:///private/tmp/codex.sock",
    )
    request.add_argument(
        "--batch-window",
        dest="relay_batch_window_seconds",
        required=True,
        type=partial(_integer_range, minimum=0, maximum=300),
        help="relay merge window in seconds, 0..300; pass 0 to deliver immediately "
        "(routine arrivals — not user chats, not cancels — coalesce into one "
        "hint per window when set)",
    )
    request.set_defaults(func=_h_impersonate)
    parsers: dict[str, argparse.ArgumentParser] = {}
    listing = commands.add_parser("list", help="page permanent session history")
    listing.add_argument("--agent", dest="agent_id", required=True, type=int)
    listing.add_argument("--before", type=int)
    # Page matches the controller-read default; 1000 = the service's validated
    # ceiling (task #3696 exception inventory). The default is an approved
    # display-only exception (task #4102): a page size is presentation, one
    # obvious value (100) exists, and the explicit-parameter ruling allows it.
    listing.add_argument("--limit", type=partial(_integer_range, maximum=1000), default=100)
    listing.set_defaults(func=_h_impersonate)
    for name in ("status", "renew", "release", "inbox", "ack", "exec", "say"):
        parser = commands.add_parser(name)
        parser.add_argument(
            "session_id",
            type=partial(_integer_range, minimum=0, maximum=2**63 - 1),
            help="per-agent session number; run from the session's controller process tree",
        )
        parser.add_argument("--agent", dest="agent_id", required=True, type=int)
        parser.set_defaults(func=_h_impersonate)
        parsers[name] = parser
    parsers["say"].add_argument("content", help="user-visible message; '-' reads stdin")
    parsers["say"].add_argument("--key", required=True, help="stable key for retry deduplication")
    # Enum display semantics: commentary is the in-progress reply and the one
    # routine value (approved exception, task #4102); final is a deliberate close.
    parsers["say"].add_argument("--phase", choices=("commentary", "final"), default="commentary")
    parsers["renew"].add_argument(
        "--ttl",
        required=True,
        type=partial(_integer_range, maximum=86400),
        help="new lifetime in seconds, 1..86400; extend deliberately, never on a timer",
    )
    parsers["release"].add_argument(
        "--summary", required=True, help="handoff summary; '-' reads stdin"
    )
    # Same page bounds as the controller read (task #3696 exception inventory).
    # Page-size default: approved display-only exception (task #4102).
    parsers["inbox"].add_argument(
        "--limit",
        type=partial(_integer_range, maximum=1000),
        default=100,
        help="maximum inbox rows, 1..1000",
    )
    # `--wait 0` reads as "return immediately" — the unique off value
    # (approved display-only exception, task #4102).
    parsers["inbox"].add_argument(
        "--wait",
        type=_seconds_range,
        default=0,
        help="finite, nonnegative seconds to wait for new input (0 = return immediately)",
    )
    parsers["ack"].add_argument(
        "message_ids", nargs="+", type=int, help="inbound IDs already processed"
    )
    parsers["exec"].add_argument("--file", help="local Python file; omitted or '-' reads stdin")
    _add_send_parser(commands)
    relay = commands.add_parser("relay", help="forward inbound wake hints to an external session")
    relay.add_argument("agent_id", type=int)
    handle = relay.add_mutually_exclusive_group(required=True)
    handle.add_argument("--lease-id", help=argparse.SUPPRESS)
    handle.add_argument("--session", dest="session_id", type=int)
    relay.add_argument("--provider", choices=("codex", "claude"), required=True)
    relay.add_argument("--thread-id")
    relay.add_argument(
        "--codex-remote",
        help="Codex app-server endpoint used by the session, e.g. unix:///private/tmp/codex.sock",
    )
    # Internal host machinery, not an operator parameter (approved exception,
    # task #4102); 0.5s is the coalescing default the relay has always used.
    relay.add_argument(
        "--debounce",
        type=partial(_seconds_range, maximum=30),
        default=0.5,
        help="seconds to coalesce inbound hints, 0..30",
    )
    relay.add_argument(
        "--token-stdin",
        action="store_true",
        help="read the relay credential from the first stdin line (native spawn handoff)",
    )
    relay.set_defaults(func=_h_impersonate_relay)
