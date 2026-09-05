"""External controller requests, explicit leases, inboxes and local SDK execution."""

from __future__ import annotations

import argparse
import math
from functools import partial


def _integer_range(value: str, *, maximum: int) -> int:
    message = f"must be an integer from 1 through {maximum}"
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(message) from exc
    if not 1 <= parsed <= maximum:
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


def _h_impersonate(args: argparse.Namespace) -> int:
    from cli.commands.impersonation import cmd_impersonate

    return cmd_impersonate(args)


def _h_impersonate_relay(args: argparse.Namespace) -> int:
    from cli.commands.impersonation_relay import cmd_relay

    return cmd_relay(args)


def _add_impersonation_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import _h_impersonate, _h_impersonate_relay

    group = sub.add_parser("impersonate", help="request and use an agent-approved external lease")
    commands = group.add_subparsers(dest="impersonation_cmd", required=True)
    request = commands.add_parser("request", help="ask an agent to lend its identity")
    request.add_argument("--agent", dest="agent_id", required=True, type=int)
    request.add_argument(
        "--as", dest="caller", required=True, help="codex[:instance] or claude[:instance]"
    )
    request.add_argument(
        "--ttl",
        type=partial(_integer_range, maximum=86400),
        default=3600,
        help="lease lifetime in seconds, 1..86400",
    )
    request.add_argument("--reason", default="", help="what the external agent will do")
    request.set_defaults(func=_h_impersonate)
    parsers: dict[str, argparse.ArgumentParser] = {}
    for name in ("status", "renew", "release", "inbox", "ack", "exec"):
        parser = commands.add_parser(name)
        parser.add_argument(
            "lease_id", help="lease UUID; credential comes from AVA_IMPERSONATION_TOKEN"
        )
        parser.set_defaults(func=_h_impersonate)
        parsers[name] = parser
    parsers["renew"].add_argument(
        "--ttl",
        type=partial(_integer_range, maximum=86400),
        default=None,
        help="new lifetime in seconds, 1..86400",
    )
    parsers["release"].add_argument(
        "--summary", required=True, help="handoff summary; '-' reads stdin"
    )
    parsers["inbox"].add_argument(
        "--limit",
        type=partial(_integer_range, maximum=1000),
        default=100,
        help="maximum inbox rows, 1..1000",
    )
    parsers["inbox"].add_argument(
        "--wait",
        type=_seconds_range,
        default=0,
        help="finite, nonnegative seconds to wait for new input",
    )
    parsers["ack"].add_argument(
        "message_ids", nargs="+", type=int, help="inbound IDs already processed"
    )
    parsers["exec"].add_argument("--file", help="local Python file; omitted or '-' reads stdin")
    relay = commands.add_parser("relay", help="forward inbound wake hints to an external session")
    relay.add_argument("agent_id", type=int)
    relay.add_argument("--lease-id", required=True)
    relay.add_argument("--provider", choices=("codex", "claude"), required=True)
    relay.add_argument("--thread-id")
    relay.add_argument(
        "--codex-remote",
        help="Codex app-server endpoint used by the session, e.g. unix:///private/tmp/codex.sock",
    )
    relay.add_argument(
        "--debounce",
        type=partial(_seconds_range, maximum=30),
        default=0.5,
        help="seconds to coalesce inbound hints, 0..30",
    )
    relay.set_defaults(func=_h_impersonate_relay)
