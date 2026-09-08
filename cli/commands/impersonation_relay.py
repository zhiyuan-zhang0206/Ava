"""Push durable AVA inbox hints into an existing external agent session.

Redis is the wake signal; the database owns pending messages. This process runs
under the lease's scoped relay credential (read inbox + heartbeat only), keeps
a single-outstanding set in memory, never ACKs a message and never renews a
lease. It heartbeats the lease row so the accepting runtime and the wake path
can observe its liveness. Restarting it replays unacknowledged hints without
losing work; inbox emptiness never ends it — only a terminal lease does.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import select
import shlex
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from pydantic import BaseModel

import shared.proc
import shared.redis_listener
from shared.config import settings
from shared.impersonation import RELAY_HEARTBEAT_SECONDS

_CATCHUP_SECONDS = 30.0
_QUEUE_TIMEOUT_SECONDS = 10.0
_MIN_HINT_INTERVAL_SECONDS = 2.0
_HINT_UNPROCESSED_ALERT_SECONDS = 300.0
_RPC_TIMEOUT_SECONDS = 20.0
_RPC_POLL_SECONDS = 0.5
_TERMINAL = frozenset({"released", "rejected", "expired"})
# Every text this relay queues starts with one of these; the invalidation pass
# matches only its own submissions, never real user messages.
_HINT_TEXT_MARKERS = ("AVA inbox ready:", "AVA control")
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

    lease = _Lease.model_validate(impersonation.relay_get(str(lease_id), token))
    if lease.agent_id != agent_id or lease.id != lease_id:
        raise ValueError("The impersonation lease does not belong to the requested agent")
    if lease.status != "active":
        return InboxSnapshot(frozenset(), lease.expires_at, lease.status)
    try:
        # relay_inbox validates same-machine active authority in its own transaction.
        rows = impersonation.relay_inbox(str(lease_id), token)
    except impersonation.ImpersonationError:
        latest = _Lease.model_validate(impersonation.relay_get(str(lease_id), token))
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
    invalidate: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Wait natively for consent, then deliver lifecycle and durable inbox hints.

    Single-outstanding delivery with no replay: at most one unprocessed hint
    per lease is ever queued, and a delivered hint is never re-emitted. A hint
    is emitted only when pending work exists that the host has not been told
    about — nothing outstanding, or new ids after the outstanding page was
    ACKed. ACK observation shrinks the outstanding set (delivered associations
    invalidate), and new messages arriving under an outstanding hint queue
    nothing: the hint already instructs the host to drain until empty, so a
    busy host cannot accumulate a wake storm of stale hints. A hint the host
    never processes is not replayed; an outstanding page stuck unprocessed
    past the stall threshold is logged loudly instead.

    `invalidate`, when given (the codex RPC pass), runs at startup, whenever
    the outstanding page has been fully ACKed, and before a terminal control
    notice — the official host-side deletion of this relay's stale queued
    hints, so even the one residual stale hint cannot wake an empty turn.

    Periodic native DB catchup repairs a dropped Redis publish, without running
    an LLM. Delivered IDs are not a processing cursor: no message is marked done.
    An emission failure exits so a restart can replay all pending messages.
    Inbox emptiness or subtask completion never ends delivery: only a terminal
    lease status does.
    """
    if not math.isfinite(debounce) or not 0 <= debounce <= _CATCHUP_SECONDS:
        raise ValueError(f"debounce must be between 0 and {_CATCHUP_SECONDS:g} seconds")
    if not math.isfinite(catchup_seconds) or catchup_seconds <= 0:
        raise ValueError("catchup_seconds must be finite and positive")
    outstanding: set[int] = set()
    announced_active = False
    last_emit = float("-inf")
    stall_alerted = False

    def due(snapshot: InboxSnapshot) -> bool:
        return snapshot.active and (
            not announced_active or (bool(snapshot.message_ids) and not outstanding)
        )

    try:
        # Validate the capability before touching this agent's Redis channel.
        initial = await read_inbox()
        if initial.status in _TERMINAL and invalidate is not None:
            await invalidate()
        if _ended(initial, agent_id, lease_id, emit):
            return
        if invalidate is not None:
            # A previous relay incarnation may have left stale hints queued.
            await invalidate()
        await listener.ensure_listening()
        while True:
            snapshot = await read_inbox()
            if snapshot.status in _TERMINAL and invalidate is not None:
                await invalidate()
            if _ended(snapshot, agent_id, lease_id, emit):
                return
            had_outstanding = bool(outstanding)
            outstanding.intersection_update(snapshot.message_ids)
            if had_outstanding and not outstanding and invalidate is not None:
                # The outstanding page is fully ACKed: its queued hint is stale.
                await invalidate()
            if not outstanding:
                stall_alerted = False  # a fresh page can only be hinted once
            if due(snapshot):
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
                outstanding.intersection_update(snapshot.message_ids)
                if due(snapshot):
                    hint = inbox_hint if announced_active else activation_hint
                    emit(hint(agent_id, lease_id, snapshot.message_ids))
                    announced_active = True
                    last_emit = asyncio.get_running_loop().time()
                    outstanding.update(snapshot.message_ids)
                    stall_alerted = False
            elif (
                announced_active
                and outstanding
                and not stall_alerted
                and (
                    asyncio.get_running_loop().time() - last_emit >= _HINT_UNPROCESSED_ALERT_SECONDS
                )
            ):
                # The outstanding hint has stayed unprocessed far past the
                # delivery window. Never replay it; say so loudly instead.
                from shared.log import logger

                stall_alerted = True
                logger.error(
                    "impersonation relay hint unprocessed past the stall threshold; "
                    "not replayed — the host must drain the inbox itself",
                    agent_id=agent_id,
                    lease_id=str(lease_id),
                    outstanding_ids=sorted(outstanding),
                )
            # The DB clock grants/revokes authority. Local time only shortens a
            # wait before its deadline; clock skew must not revoke a live lease
            # or spin on a past local timestamp while the DB still grants it.
            await listener.wait_one(max(0.5, min(catchup_seconds, _seconds_left(snapshot))))
    finally:
        await listener.close()


def _queue_rpc(
    process: subprocess.Popen[str], ident: int, method: str, params: dict[str, object]
) -> dict[str, Any]:
    """One JSON-RPC exchange with the stdio app-server; bounded and fail-loud."""
    if process.stdin is None or process.stdout is None:
        raise RuntimeError("codex app-server stdio pipes are unavailable")
    process.stdin.write(json.dumps({"id": ident, "method": method, "params": params}) + "\n")
    process.stdin.flush()
    deadline = time.monotonic() + _RPC_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        ready, _, _ = select.select([process.stdout], [], [], _RPC_POLL_SECONDS)
        if not ready:
            continue
        line = process.stdout.readline()
        if not line:
            raise RuntimeError("codex app-server exited during the queue RPC")
        payload = json.loads(line)
        if payload.get("id") == ident:
            return payload
    raise TimeoutError(f"codex queue RPC {method} timed out")


def _queued_hint_ids(process: subprocess.Popen[str], thread_id: UUID) -> list[str]:
    """Ids of THIS relay's hint submissions still queued on the thread.

    Identified by the hint text markers only — real user messages and other
    threads' submissions are never touched. Read-only: the Codex store is
    only accessed through the official queue RPCs.
    """
    ids: list[str] = []
    cursor: str | None = None
    ident = 1
    while True:
        params: dict[str, object] = {"threadId": str(thread_id), "limit": 100}
        if cursor is not None:
            params["cursor"] = cursor
        response = _queue_rpc(process, ident, "thread/queue/list", params)
        ident += 1
        result = response.get("result")
        if not isinstance(result, dict):
            raise TypeError("codex thread/queue/list returned no object result")
        result = cast(dict[str, Any], result)
        data = result.get("data")
        if not isinstance(data, list):
            raise TypeError("codex thread/queue/list returned no submission list")
        submissions = cast(list[dict[str, Any]], data)
        for submission in submissions:
            raw_input = submission.get("input")
            if not isinstance(raw_input, dict):
                continue
            input_spec = cast(dict[str, Any], raw_input)
            items = input_spec.get("items")
            if not isinstance(items, list):
                continue
            texts: list[str] = []
            for item in cast(list[dict[str, Any]], items):
                if item.get("type") == "text":
                    texts.append(str(item.get("text", "")))
            if any(text.startswith(_HINT_TEXT_MARKERS) for text in texts):
                ids.append(str(submission["id"]))
        cursor = result.get("nextCursor")
        if not cursor:
            return ids


def invalidate_our_hints(thread_id: UUID) -> int:
    """Best-effort removal of this relay's stale queued hints via the official
    Codex thread/queue RPCs (experimentalApi). Never touches Codex's private
    store. Returns the number of deleted submissions; failures log loudly and
    count as zero — single-outstanding emission still bounds the storm without
    this channel.
    """
    process = subprocess.Popen(  # fixed argv: the same codex binary the relay queues through
        ["codex", "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    deleted = 0
    try:
        _queue_rpc(
            process,
            0,
            "initialize",
            {
                "clientInfo": {"name": "ava-impersonation-relay", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        if process.stdin is not None:
            process.stdin.write(json.dumps({"method": "initialized"}) + "\n")
            process.stdin.flush()
        for queued_id in _queued_hint_ids(process, thread_id):
            _queue_rpc(
                process,
                1000 + deleted,
                "thread/queue/delete",
                {"threadId": str(thread_id), "queuedSubmissionId": queued_id},
            )
            deleted += 1
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        from shared.log import logger

        logger.warning(
            "codex queue invalidation unavailable; stale hints may remain queued",
            thread_id=str(thread_id),
            error=str(exc),
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    return deleted


def _write_heartbeat(lease_id: UUID, token: str) -> bool:
    """One durable liveness beat; False means the lease ended or the relay
    credential was revoked — the heartbeat loop stops silently."""
    from shared import impersonation

    try:
        impersonation.relay_heartbeat(str(lease_id), token)
    except impersonation.ImpersonationError:
        return False
    return True


async def _heartbeat_loop(
    lease_id: UUID, token: str, *, interval: float = RELAY_HEARTBEAT_SECONDS
) -> None:
    while True:
        await asyncio.sleep(interval)
        if not await asyncio.to_thread(_write_heartbeat, lease_id, token):
            return


def cmd_relay(args: argparse.Namespace) -> int:
    """Run a native relay until lease release/expiry, failure, or interruption.

    The first heartbeat is written before the inbox loop, so the accepting
    runtime's readiness gate observes a live relay immediately. The relay
    never renews the lease; its credential only reads and beats.
    """
    from cli.commands import impersonation

    try:
        lease_id = UUID(args.lease_id)
        if args.token_stdin:
            token = impersonation.relay_token_from_stdin()
        else:
            token = impersonation.relay_token_from_env()
        emit = host_emitter(args.provider, args.thread_id, codex_remote=args.codex_remote)

        async def run() -> None:
            async def read_inbox() -> InboxSnapshot:
                return await asyncio.to_thread(_read_inbox, args.agent_id, lease_id, token)

            invalidate: Callable[[], Awaitable[None]] | None = None
            if args.provider == "codex" and args.thread_id is not None:
                thread_id = UUID(args.thread_id)

                async def invalidate_stale() -> None:
                    await asyncio.to_thread(invalidate_our_hints, thread_id)

                invalidate = invalidate_stale

            if not await asyncio.to_thread(_write_heartbeat, lease_id, token):
                return  # lease already terminal; nothing to relay
            heartbeat = asyncio.create_task(_heartbeat_loop(lease_id, token))
            try:
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
                    invalidate=invalidate,
                )
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

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
