"""Push durable AVA inbox hints into an existing external agent session.

Redis is the wake signal; the database owns pending messages. This process keeps
only a deduplication set in memory, never ACKs a message and never renews a lease.
Restarting it therefore replays unacknowledged hints without losing work.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import shlex
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

import shared.proc
import shared.redis_listener
from shared.config import settings

_CATCHUP_SECONDS = 30.0
_QUEUE_TIMEOUT_SECONDS = 10.0
_MIN_HINT_INTERVAL_SECONDS = 2.0
_TERMINAL = frozenset({"released", "rejected", "expired"})
type LeaseStatus = Literal["requested", "accepted", "active", "released", "rejected", "expired"]


class _Lease(BaseModel):
    id: UUID
    agent_id: int
    status: LeaseStatus
    expires_at: datetime


@dataclass(frozen=True)
class InboxSnapshot:
    """Only message identities cross the host wake boundary, never their bodies."""

    message_ids: frozenset[int]
    expires_at: datetime
    status: LeaseStatus = "active"

    @property
    def active(self) -> bool:
        return self.status == "active"


class WakeListener(Protocol):
    async def ensure_listening(self) -> None: ...

    async def wait_one(self, timeout: float) -> None: ...

    async def close(self) -> None: ...


def inbox_hint(agent_id: int, lease_id: UUID, pending: frozenset[int]) -> str:
    """A short, content-free line that fits Claude Monitor's output limit."""
    # A bare `ava` on PATH can name production while this relay belongs to a
    # worktree cluster. Keep the receiving agent on this exact interpreter.
    command = shlex.join([sys.executable, "-m", "cli", "impersonate", "inbox", str(lease_id)])
    return (
        f"AVA inbox ready: agent={agent_id} lease={lease_id} "
        f"pending_page={len(pending)} newest_id={max(pending, default=0)}. "
        f"Run {command}; "
        "process and explicitly ACK message IDs, draining pages until empty. "
        "This hint is not a processing ACK."
    )


def activation_hint(agent_id: int, lease_id: UUID, pending: frozenset[int]) -> str:
    prefix = [sys.executable, "-m", "cli", "impersonate"]
    inbox = shlex.join([*prefix, "inbox", str(lease_id)])
    ack = shlex.join([*prefix, "ack", str(lease_id)])
    return (
        f"AVA control active: agent={agent_id} "
        f"pending_page={len(pending)} newest_id={max(pending, default=0)}. "
        f"Read missing context as needed. Inbox: {inbox}. "
        f"After processing, explicitly ACK: {ack} ID...; drain pages until empty."
    )


def _ended(
    snapshot: InboxSnapshot, agent_id: int, lease_id: UUID, emit: Callable[[str], None]
) -> bool:
    if snapshot.status not in _TERMINAL:
        return False
    if snapshot.status != "released":
        emit(
            f"AVA control {snapshot.status}: agent={agent_id} lease={lease_id}. "
            "Do not use this identity. The native agent can continue its workflow. "
            "The relay did not ACK messages or renew the lease."
        )
    return True


def queue_codex(thread_id: UUID, message: str, *, remote: str | None = None) -> None:
    """Queue on the explicitly selected existing Codex session; never spawn one."""
    argv = ["codex", "queue", "--thread", str(thread_id), "--message", message]
    if remote is not None:
        # Queue into the server holding this thread. A different server can
        # persist the hint but leave delivery to Codex's 10-second DB watcher.
        argv.extend(["--remote", remote])
    result = shared.proc.run_bounded(
        argv,
        timeout=_QUEUE_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        # Do not forward arbitrary provider output (which can contain credentials).
        raise RuntimeError(f"codex queue failed with exit code {result.returncode}")


def monitor_claude(message: str) -> None:
    """Each flushed stdout line is a same-session Claude Monitor event."""
    print(message, flush=True)


def host_emitter(
    provider: str, thread_id: str | None, *, codex_remote: str | None = None
) -> Callable[[str], None]:
    """Resolve an explicit host destination before opening the inbox relay."""
    if provider == "codex":
        if thread_id is None:
            raise ValueError("codex relay requires --thread-id for an existing session")
        target = UUID(thread_id)
        return lambda message: queue_codex(target, message, remote=codex_remote)
    if provider == "claude":
        if codex_remote is not None:
            raise ValueError("Claude Monitor does not use --codex-remote")
        if thread_id is not None:
            raise ValueError("Claude Monitor routes to its owner; omit --thread-id")
        return monitor_claude
    raise ValueError(f"Unknown relay provider: {provider}")


def _read_inbox(agent_id: int, lease_id: UUID, token: str) -> InboxSnapshot:
    from shared import impersonation

    lease = _Lease.model_validate(impersonation.get(str(lease_id), token))
    if lease.agent_id != agent_id or lease.id != lease_id:
        raise ValueError("The impersonation lease does not belong to the requested agent")
    if lease.status != "active":
        return InboxSnapshot(frozenset(), lease.expires_at, lease.status)
    try:
        # inbox validates same-machine active authority in its own transaction.
        rows = impersonation.inbox(str(lease_id), token)
    except impersonation.ImpersonationError:
        latest = _Lease.model_validate(impersonation.get(str(lease_id), token))
        if latest.status in _TERMINAL:
            return InboxSnapshot(frozenset(), latest.expires_at, latest.status)
        raise
    return InboxSnapshot(frozenset(row["id"] for row in rows), lease.expires_at)


def _seconds_left(snapshot: InboxSnapshot) -> float:
    if snapshot.expires_at.tzinfo is None:
        raise ValueError("Lease expiry must include a timezone")
    return (snapshot.expires_at - datetime.now(UTC)).total_seconds()


async def relay_inbox(
    agent_id: int,
    lease_id: UUID,
    *,
    read_inbox: Callable[[], Awaitable[InboxSnapshot]],
    listener: WakeListener,
    emit: Callable[[str], None],
    debounce: float = 0.5,
    catchup_seconds: float = _CATCHUP_SECONDS,
) -> None:
    """Wait natively for consent, then deliver lifecycle and durable inbox hints.

    Periodic native DB catchup repairs a dropped Redis publish, without running
    an LLM. Delivered IDs are not a processing cursor: no message is marked done.
    An emission failure exits so a restart can replay all pending messages.
    """
    if not math.isfinite(debounce) or not 0 <= debounce <= _CATCHUP_SECONDS:
        raise ValueError(f"debounce must be between 0 and {_CATCHUP_SECONDS:g} seconds")
    if not math.isfinite(catchup_seconds) or catchup_seconds <= 0:
        raise ValueError("catchup_seconds must be finite and positive")
    delivered: set[int] = set()
    announced_active = False
    last_emit = float("-inf")
    try:
        # Validate the capability before touching this agent's Redis channel.
        initial = await read_inbox()
        if _ended(initial, agent_id, lease_id, emit):
            return
        await listener.ensure_listening()
        while True:
            snapshot = await read_inbox()
            if _ended(snapshot, agent_id, lease_id, emit):
                return
            delivered.intersection_update(snapshot.message_ids)
            if snapshot.active and (not announced_active or snapshot.message_ids - delivered):
                # Re-read after a bounded debounce to merge bursts and catch a
                # concurrent release/expiry before attempting host delivery.
                # Claude Monitor replenishes one output-event allowance per
                # two seconds. Sustained overload can stop its subprocess.
                interval = max(
                    debounce,
                    last_emit + _MIN_HINT_INTERVAL_SECONDS - asyncio.get_running_loop().time(),
                )
                await asyncio.sleep(interval)
                snapshot = await read_inbox()
                if _ended(snapshot, agent_id, lease_id, emit):
                    return
                delivered.intersection_update(snapshot.message_ids)
                if snapshot.active and (not announced_active or snapshot.message_ids - delivered):
                    hint = inbox_hint if announced_active else activation_hint
                    emit(hint(agent_id, lease_id, snapshot.message_ids))
                    announced_active = True
                    last_emit = asyncio.get_running_loop().time()
                    delivered.update(snapshot.message_ids)
            # The DB clock grants/revokes authority. Local time only shortens a
            # wait before its deadline; clock skew must not revoke a live lease
            # or spin on a past local timestamp while the DB still grants it.
            await listener.wait_one(max(0.5, min(catchup_seconds, _seconds_left(snapshot))))
    finally:
        await listener.close()


def cmd_relay(args: argparse.Namespace) -> int:
    """Run a native relay until lease release/expiry, failure, or interruption."""
    from cli.commands import impersonation

    try:
        lease_id = UUID(args.lease_id)
        token = impersonation.token_from_env()
        emit = host_emitter(args.provider, args.thread_id, codex_remote=args.codex_remote)

        async def run() -> None:
            async def read_inbox() -> InboxSnapshot:
                return await asyncio.to_thread(_read_inbox, args.agent_id, lease_id, token)

            listener = shared.redis_listener.RedisInboundListener(
                settings.data_plane.redis_url, args.agent_id
            )
            await relay_inbox(
                args.agent_id,
                lease_id,
                read_inbox=read_inbox,
                listener=listener,
                emit=emit,
                debounce=args.debounce,
            )

        asyncio.run(run())
    except KeyboardInterrupt:
        print(
            "Relay stopped; pending messages are unchanged and the lease is not renewed.",
            file=sys.stderr,
        )
        return 130
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"Impersonation relay stopped: {exc}", file=sys.stderr)
        return 1
    return 0
