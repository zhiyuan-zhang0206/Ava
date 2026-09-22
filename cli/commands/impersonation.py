"""Cluster-local client for trusted named impersonation sessions.

Only request prints the scoped relay credential (claude). Controller commands
run under the session id with caller-presence attestation: no controller
credential is minted, delivered, or stored. Session history is retained by the
shared service.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID


def relay_token_from_env() -> str:
    """Read the scoped relay credential the controller received at request time."""
    # env-ok: external relay credential handoff, not cluster configuration
    token = os.environ.get("AVA_IMPERSONATION_RELAY_TOKEN")
    if not token:
        raise ValueError(
            "set AVA_IMPERSONATION_RELAY_TOKEN to the request's relay token "
            "(printed once in the request response)"
        )
    return token


def relay_token_from_stdin() -> str:
    """Read the scoped relay credential the accepting runtime piped over stdin.

    The native spawn handoff keeps the credential out of argv, the environment
    and any file.
    """
    line = sys.stdin.readline()
    if not line:
        raise ValueError("no relay token on stdin (use --token-stdin with a piped credential)")
    return line.rstrip("\r\n")


def _json_value(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, default=_json_value))


async def _wait_inbox(
    lease_id: str, caller: dict[str, Any], limit: int, wait: float
) -> list[dict[str, Any]]:
    from shared import impersonation as control
    from shared.config import settings
    from shared.redis_listener import RedisInboundListener

    if not math.isfinite(wait) or wait < 0:
        raise ValueError("--wait must be finite and nonnegative")
    lease = await asyncio.to_thread(control.require_active, lease_id, caller)
    listener = RedisInboundListener(settings.data_plane.redis_url, lease["agent_id"])
    try:
        if wait:
            await listener.ensure_listening()
        deadline = time.monotonic() + wait
        while True:
            messages = await asyncio.to_thread(control.inbox, lease_id, caller, limit=limit)
            remaining = deadline - time.monotonic()
            if messages or remaining <= 0:
                return messages
            await listener.wait_one(min(remaining, 30.0))
    finally:
        await listener.close()


def _run_local(args: argparse.Namespace) -> int:
    import ava

    code = (
        sys.stdin.read()
        if args.file is None or args.file == "-"
        else Path(args.file).read_text(encoding="utf-8")
    )
    if not code.strip():
        raise ValueError("Python input must be nonempty")
    with ava.external.attach(args.session_id, agent_id=args.agent_id):
        exec(
            compile(code, args.file or "<ava-external>", "exec"),
            {"__name__": "__main__", "ava": ava},
        )
    return 0


def _send(args: argparse.Namespace) -> int:
    """`impersonate send` — one message to another agent as the leased identity.

    Attested through the session's caller-presence rule; the delivered source is
    ``agent:<the leased agent>`` — the borrowed identity the SDK attachment
    stamps for ``ava.agents.send_message`` (task #4102).
    """
    from cli.commands.agents import send_agent_message
    from shared import impersonation as control
    from shared import impersonation_sessions as sessions
    from shared.proc_tree import process_metadata

    content = sys.stdin.read() if args.content == "-" else args.content
    caller = process_metadata()
    lease_id = sessions.private_id(args.agent_id, args.session_id)
    lease = control.require_active(lease_id, caller)
    source = f"agent:{lease['agent_id']}"
    status = send_agent_message(args.target_agent_id, content, source=source)
    _emit({"status": status, "to": args.target_agent_id, "source": source})
    return 0


def _dispatch(args: argparse.Namespace) -> int:
    from shared import impersonation as control
    from shared import impersonation_sessions as sessions
    from shared.impersonation_history import public_session, say
    from shared.proc_tree import process_metadata

    command = args.impersonation_cmd
    if command == "request":
        from cli.commands.codex_app_server import require_control_endpoint

        endpoint = args.relay_codex_remote
        if args.relay_provider == "codex":
            endpoint = require_control_endpoint(endpoint)
        _emit(
            sessions.request(
                args.agent_id,
                name=args.name,
                executor_name=args.caller,
                process_metadata=process_metadata(),
                ttl_seconds=args.ttl,
                reason=args.reason,
                provider=args.relay_provider,
                thread_id=args.relay_thread_id,
                codex_remote=endpoint,
                batch_window_seconds=args.relay_batch_window_seconds,
            )
        )
        if args.relay_provider == "codex":
            print(
                "The runtime starts the codex relay automatically at activation; "
                "no relay process starts here. When your session runs an explicit app "
                "server, pass --codex-remote with its endpoint so the relay reaches the "
                "same server the session uses (Steer delivery, no Pending fallback; "
                "see the host conventions). A relay "
                "that cannot start rolls the takeover back loudly (status becomes "
                "rejected with the reason).",
                file=sys.stderr,
            )
        else:
            print(
                "Start the claude relay (ava impersonate relay) inside the controller "
                "session immediately, with AVA_IMPERSONATION_RELAY_TOKEN set to the "
                "relay token printed above; arm it as a Monitor watch with timeout_ms "
                "1800000 and re-arm on each expiry notice (see the host conventions). "
                "Preparation fails without its heartbeat.",
                file=sys.stderr,
            )
        return 0
    if command == "list":
        _emit(sessions.list_sessions(args.agent_id, before=args.before, limit=args.limit))
        return 0
    if command == "send":
        return _send(args)
    caller = process_metadata()
    args.lease_id = sessions.private_id(args.agent_id, args.session_id)
    if command == "status":
        _emit(public_session(control.get(args.lease_id, caller)))
    elif command == "renew":
        _emit(public_session(control.renew(args.lease_id, caller, ttl_seconds=args.ttl)))
    elif command == "release":
        summary = sys.stdin.read() if args.summary == "-" else args.summary
        _emit(public_session(control.release(args.lease_id, caller, summary)))
    elif command == "inbox":
        _emit(asyncio.run(_wait_inbox(args.lease_id, caller, args.limit, args.wait)))
    elif command == "ack":
        control.ack(args.lease_id, caller, args.message_ids)
        _emit({"acknowledged": args.message_ids})
    elif command == "say":
        content = sys.stdin.read() if args.content == "-" else args.content
        _emit({"seq": say(args.lease_id, caller, content, phase=args.phase, message_key=args.key)})
    elif command == "exec":
        return _run_local(args)
    else:
        raise ValueError(f"unknown impersonation command: {command}")
    return 0


def cmd_impersonate(args: argparse.Namespace) -> int:
    """Run one command, with operational errors confined to stderr."""
    try:
        return _dispatch(args)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"impersonation: {exc}", file=sys.stderr)
        return 1
