"""Thin cluster-local client for approved external-agent leases.

Only request prints the newly minted credential. All subsequent commands read
AVA_IMPERSONATION_TOKEN; no token, conversation or session files are created.
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


def token_from_env() -> str:
    """Read the explicit external lease credential, without inferring identity."""
    # env-ok: external controller credential handoff, not cluster configuration
    token = os.environ.get("AVA_IMPERSONATION_TOKEN")
    if not token:
        raise ValueError("set AVA_IMPERSONATION_TOKEN to the request's token")
    return token


def _json_value(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, default=_json_value))


def _caller(value: str) -> Any:
    from shared.caller_identity import CallerIdentity

    parts = value.split(":")
    if len(parts) not in (1, 2):
        raise ValueError("--as must be tool[:instance]")
    return CallerIdentity(
        kind="external_agent", subject=parts[0], instance=parts[1] if len(parts) == 2 else None
    )


async def _wait_inbox(lease_id: str, token: str, limit: int, wait: float) -> list[dict[str, Any]]:
    from shared import impersonation as control
    from shared.config import settings
    from shared.redis_listener import RedisInboundListener

    if not math.isfinite(wait) or wait < 0:
        raise ValueError("--wait must be finite and nonnegative")
    lease = await asyncio.to_thread(control.require_active, lease_id, token)
    listener = RedisInboundListener(settings.data_plane.redis_url, lease["agent_id"])
    try:
        if wait:
            await listener.ensure_listening()
        deadline = time.monotonic() + wait
        while True:
            messages = await asyncio.to_thread(control.inbox, lease_id, token, limit=limit)
            remaining = deadline - time.monotonic()
            if messages or remaining <= 0:
                return messages
            await listener.wait_one(min(remaining, 30.0))
    finally:
        await listener.close()


def _run_local(args: argparse.Namespace, token: str) -> int:
    import ava

    code = (
        sys.stdin.read()
        if args.file is None or args.file == "-"
        else Path(args.file).read_text(encoding="utf-8")
    )
    if not code.strip():
        raise ValueError("Python input must be nonempty")
    with ava.external.attach(args.lease_id, token=token):
        exec(
            compile(code, args.file or "<ava-external>", "exec"),
            {"__name__": "__main__", "ava": ava},
        )
    return 0


def _dispatch(args: argparse.Namespace) -> int:
    from shared import impersonation as control

    command = args.impersonation_cmd
    if command == "request":
        _emit(
            control.request(
                args.agent_id, caller=_caller(args.caller), ttl_seconds=args.ttl, reason=args.reason
            )
        )
        return 0
    token = token_from_env()
    if command == "status":
        _emit(control.get(args.lease_id, token))
    elif command == "renew":
        _emit(control.renew(args.lease_id, token, ttl_seconds=args.ttl))
    elif command == "release":
        summary = sys.stdin.read() if args.summary == "-" else args.summary
        _emit(control.release(args.lease_id, token, summary))
    elif command == "inbox":
        _emit(asyncio.run(_wait_inbox(args.lease_id, token, args.limit, args.wait)))
    elif command == "ack":
        control.ack(args.lease_id, token, args.message_ids)
        _emit({"acknowledged": args.message_ids})
    elif command == "exec":
        return _run_local(args, token)
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
