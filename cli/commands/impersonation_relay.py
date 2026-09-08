"""Push the agent's durable inbox into an existing external agent session.

Redis is the wake signal; the database owns pending messages. This process runs
under the lease's scoped relay credential (read inbox + heartbeat only), never
ACKs a message and never renews a lease. It heartbeats the lease row so the
accepting runtime and the wake path can observe its liveness.

Delivery is push with an ACK window: the relay sends the native agent's start
message at activation, then pushes every pending inbox row with its full
content in one self-contained envelope per batch. A batch the host does not
ACK within the window is pushed again, marked as re-delivery, until it is
ACKed or the lease ends. The message ids in each envelope make delivery
idempotent, and restarting the relay replays every pending row — at-least-once,
never silently lost. Inbox emptiness never ends it — only a terminal lease
does.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import math
import shlex
import subprocess
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

import shared.proc
import shared.redis_listener
from shared.config import settings
from shared.impersonation import RELAY_HEARTBEAT_SECONDS

_CATCHUP_SECONDS = 30.0
_QUEUE_TIMEOUT_SECONDS = 10.0
_MIN_EMIT_INTERVAL_SECONDS = 2.0
_ACK_WINDOW_SECONDS = 300.0
_CLAUDE_MAX_CHARS = 2000
_TERMINAL = frozenset({"released", "rejected", "expired"})
type LeaseStatus = Literal["requested", "accepted", "active", "released", "rejected", "expired"]


class _Lease(BaseModel):
    id: UUID
    agent_id: int
    status: LeaseStatus
    expires_at: datetime
    relay_batch_window_seconds: int = 0
    start_message: str = ""


@dataclass(frozen=True)
class InboxMessage:
    """One durable inbox row crossing the host wake boundary with its body."""

    id: int
    kind: str
    source: str
    content: str


@dataclass(frozen=True)
class InboxSnapshot:
    """Pending rows plus the lease facts the delivery loop needs.

    ``messages`` carries each row's full body keyed by id — the push envelope
    is built from it, so the host never has to read the inbox itself.
    ``routine_ids`` is the subset of ``message_ids`` that the merge window may
    coalesce (not a user chat, not a cancel, not a reminder); anything outside
    it pushes immediately. ``batch_window`` is the lease's configured merge
    window in seconds; 0 keeps the pre-window behaviour. ``start_message`` is
    the native agent's handoff brief; empty only for leases accepted before
    the push protocol shipped.
    """

    message_ids: frozenset[int]
    messages: dict[int, InboxMessage]
    expires_at: datetime
    status: LeaseStatus = "active"
    routine_ids: frozenset[int] = frozenset()
    batch_window: float = 0.0
    start_message: str = ""

    @property
    def active(self) -> bool:
        return self.status == "active"


class WakeListener(Protocol):
    async def ensure_listening(self) -> None: ...

    async def wait_one(self, timeout: float) -> None: ...

    async def close(self) -> None: ...


def ack_command(lease_id: UUID, ids: Sequence[int]) -> str:
    """The exact ACK command for one pushed batch, on this interpreter."""
    return shlex.join(
        [sys.executable, "-m", "cli", "impersonate", "ack", str(lease_id), *map(str, ids)]
    )


def message_push(
    agent_id: int,
    lease_id: UUID,
    messages: Sequence[InboxMessage],
    *,
    redelivery: bool = False,
    max_chars: int | None = None,
) -> str:
    """One self-contained envelope covering one batch of durable inbox rows.

    Carries the full content, the batch's message ids and the exact ACK
    command, so the external session processes messages without reading or
    parsing the inbox. A re-delivery push says so explicitly; the ids make it
    idempotent for a host that already handled the batch. ``max_chars`` bounds
    each content block (Claude Monitor's per-line budget); the tail points at
    the inbox command for the full text.
    """
    ids = [message.id for message in messages]
    header = f"Ava message agent={agent_id} lease={lease_id} ids={','.join(map(str, ids))}"
    if redelivery:
        header += " (re-delivery: unacknowledged)"
    blocks: list[str] = [header]
    for message in messages:
        content = message.content
        if max_chars is not None and len(content) > max_chars:
            content = (
                content[:max_chars] + " (truncated; run the inbox command to read the full message)"
            )
        blocks.append(f"[id={message.id}] kind={message.kind} from={message.source}")
        blocks.append(content)
        blocks.append("")
    blocks.append(f"ACK after processing: {ack_command(lease_id, ids)}")
    blocks.append(
        f"Unacknowledged messages are re-delivered every {_ACK_WINDOW_SECONDS:g}s "
        "until acknowledged."
    )
    return "\n".join(blocks)


def activation_hint(agent_id: int, lease_id: UUID) -> str:
    """Fallback opener for a lease accepted without a start message.

    New accepts require a nonempty start message, so this only serves leases
    accepted before the push protocol shipped. Updated to the push contract:
    messages arrive here, ACK each batch, inbox is the fallback read.
    """
    prefix = [sys.executable, "-m", "cli", "impersonate"]
    inbox = shlex.join([*prefix, "inbox", str(lease_id)])
    ack = shlex.join([*prefix, "ack", str(lease_id)])
    return (
        f"Ava control active: agent={agent_id} lease={lease_id}. "
        "Inbox messages are pushed to this session; after processing each batch "
        f"run: {ack} ID... Use {inbox} to read missed or truncated messages. "
        "Unacknowledged messages are re-delivered until acknowledged."
    )


def _ended(
    snapshot: InboxSnapshot, agent_id: int, lease_id: UUID, emit: Callable[[str], None]
) -> bool:
    if snapshot.status not in _TERMINAL:
        return False
    if snapshot.status != "released":
        emit(
            f"Ava control {snapshot.status}: agent={agent_id} lease={lease_id}. "
            "Do not use this identity. The native agent can continue its workflow. "
            "The relay did not ACK messages or renew the lease."
        )
    return True


def queue_codex(thread_id: UUID, message: str, *, remote: str | None = None) -> None:
    """Queue on the explicitly selected existing Codex session; never spawn one."""
    argv = ["codex", "queue", "--thread", str(thread_id), "--message", message]
    if remote is not None:
        # Queue into the server holding this thread. A different server can
        # persist the push but leave delivery to Codex's 10-second DB watcher.
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
        return InboxSnapshot(frozenset(), {}, lease.expires_at, lease.status)
    try:
        # relay_inbox validates same-machine active authority in its own transaction.
        rows = impersonation.relay_inbox(str(lease_id), token)
    except impersonation.ImpersonationError:
        latest = _Lease.model_validate(impersonation.relay_get(str(lease_id), token))
        if latest.status in _TERMINAL:
            return InboxSnapshot(frozenset(), {}, latest.expires_at, latest.status)
        raise
    messages = {
        row["id"]: InboxMessage(
            id=row["id"],
            kind=row["kind"],
            source=row["source"],
            content=row["content"],
        )
        for row in rows
    }
    return InboxSnapshot(
        frozenset(messages),
        messages,
        lease.expires_at,
        "active",
        routine_ids=_routine_ids(rows),
        batch_window=float(lease.relay_batch_window_seconds),
        start_message=lease.start_message,
    )


def _routine_ids(rows: list[dict[str, Any]]) -> frozenset[int]:
    """The pending ids the merge window may coalesce.

    User chats, cancels and renewal reminders are never routine: they push
    immediately. Every other arrival (peer/system notification, watcher wake,
    schedule trigger, page message) may wait out the configured window.
    """
    return frozenset(
        row["id"]
        for row in rows
        if row["kind"] not in ("cancel", "reminder")
        and not (row["kind"] == "chat" and row["source"] == "user")
    )


def _loop_time() -> float:
    """The running loop's monotonic clock, as a seam for deterministic tests."""
    return asyncio.get_running_loop().time()


def _seconds_left(snapshot: InboxSnapshot) -> float:
    if snapshot.expires_at.tzinfo is None:
        raise ValueError("Lease expiry must include a timezone")
    return (snapshot.expires_at - datetime.now(UTC)).total_seconds()


def _window_wait(
    snapshot: InboxSnapshot,
    *,
    new_ids: frozenset[int],
    routine_deadline: float | None,
    now: float,
) -> tuple[float | None, float | None]:
    """(deadline, remaining) for a routine-only page still inside its merge window.

    Returns (None, None) when the page is not window-eligible — no new ids,
    any urgent id (user chat, cancel or reminder), or a disabled window (0) —
    or when the window has already elapsed. ``deadline`` carries an open
    window forward across polls so a burst keeps one shared deadline.
    """
    if not new_ids or new_ids - snapshot.routine_ids or snapshot.batch_window <= 0:
        return None, None
    if routine_deadline is None:
        routine_deadline = now + snapshot.batch_window
    remaining = routine_deadline - now
    if remaining <= 0:
        return None, None
    return routine_deadline, remaining


async def relay_inbox(  # noqa: PLR0915 — one lease-driven state machine: terminal, window, push, re-deliver
    agent_id: int,
    lease_id: UUID,
    *,
    read_inbox: Callable[[], Awaitable[InboxSnapshot]],
    listener: WakeListener,
    emit: Callable[[str], None],
    debounce: float = 0.5,
    catchup_seconds: float = _CATCHUP_SECONDS,
    max_chars: int | None = None,
) -> None:
    """Wait natively for consent, then deliver the start message and inbox rows.

    Push with an ACK window: every pending inbox row is pushed with its full
    content; a batch not ACKed within ``_ACK_WINDOW_SECONDS`` is pushed again,
    marked as re-delivery, until the host ACKs it or the lease ends. ACK
    observation shrinks the outstanding set; the envelope ids make delivery
    idempotent, and a relay restart replays every pending row.

    The start message (the native agent's handoff brief) goes first, once, when
    the lease goes active. Rows already pending at activation skip the merge
    window — they waited through consent; later routine arrivals coalesce
    inside the configured window, while user chats, cancels and reminders
    never wait. A sustained no-ACK is visible as periodic re-deliveries, not
    silence.

    Periodic native DB catchup repairs a dropped Redis publish, without running
    an LLM. Delivered ids are not a processing cursor: no message is marked
    done. An emission failure exits so a restart can replay all pending rows.
    Inbox emptiness never ends delivery: only a terminal lease status does.
    """
    if not math.isfinite(debounce) or not 0 <= debounce <= _CATCHUP_SECONDS:
        raise ValueError(f"debounce must be between 0 and {_CATCHUP_SECONDS:g} seconds")
    if not math.isfinite(catchup_seconds) or catchup_seconds <= 0:
        raise ValueError("catchup_seconds must be finite and positive")
    outstanding: dict[int, float] = {}
    start_sent = False
    last_emit = float("-inf")
    routine_deadline: float | None = None
    activation_pending: frozenset[int] = frozenset()

    def urgent(snapshot: InboxSnapshot) -> frozenset[int]:
        return snapshot.message_ids - snapshot.routine_ids

    try:
        initial = await read_inbox()
        if _ended(initial, agent_id, lease_id, emit):
            return
        await listener.ensure_listening()
        while True:
            snapshot = await read_inbox()
            if _ended(snapshot, agent_id, lease_id, emit):
                return
            outstanding = {i: t for i, t in outstanding.items() if i in snapshot.message_ids}
            if not snapshot.active:
                # Still awaiting native consent/drain: nothing is announced yet.
                await listener.wait_one(max(0.5, min(catchup_seconds, _seconds_left(snapshot))))
                continue
            if not start_sent:
                activation_pending = snapshot.message_ids
                emit(snapshot.start_message or activation_hint(agent_id, lease_id))
                start_sent = True
                last_emit = _loop_time()
                continue
            new = snapshot.message_ids - outstanding.keys()
            if new:
                # Activation-pending rows and urgent rows push immediately;
                # routine-only fresh arrivals may wait out the merge window.
                if not (new & urgent(snapshot)) and not (new & activation_pending):
                    routine_deadline, remaining = _window_wait(
                        snapshot,
                        new_ids=new,
                        routine_deadline=routine_deadline,
                        now=_loop_time(),
                    )
                    if remaining is not None:
                        await listener.wait_one(
                            max(
                                0.5,
                                min(catchup_seconds, _seconds_left(snapshot), remaining),
                            )
                        )
                        continue
                # Re-read after a bounded debounce to merge bursts and catch a
                # concurrent release/expiry before attempting host delivery.
                # Claude Monitor replenishes one output-event allowance per
                # two seconds. Sustained overload can stop its subprocess.
                interval = max(
                    debounce,
                    last_emit + _MIN_EMIT_INTERVAL_SECONDS - _loop_time(),
                )
                await asyncio.sleep(interval)
                snapshot = await read_inbox()
                if _ended(snapshot, agent_id, lease_id, emit):
                    return
                outstanding = {i: t for i, t in outstanding.items() if i in snapshot.message_ids}
                new = snapshot.message_ids - outstanding.keys()
                if not new:
                    routine_deadline = None
                    continue  # vanished (ACKed) during the debounce
                emit(
                    message_push(
                        agent_id,
                        lease_id,
                        [snapshot.messages[i] for i in sorted(new)],
                        max_chars=max_chars,
                    )
                )
                now = _loop_time()
                for message_id in new:
                    outstanding[message_id] = now
                last_emit = now
                routine_deadline = None
                continue
            stale = [
                message_id
                for message_id, pushed_at in outstanding.items()
                if _loop_time() - pushed_at >= _ACK_WINDOW_SECONDS
            ]
            if stale:
                interval = max(
                    debounce,
                    last_emit + _MIN_EMIT_INTERVAL_SECONDS - _loop_time(),
                )
                await asyncio.sleep(interval)
                snapshot = await read_inbox()
                if _ended(snapshot, agent_id, lease_id, emit):
                    return
                outstanding = {i: t for i, t in outstanding.items() if i in snapshot.message_ids}
                stale = [message_id for message_id in stale if message_id in outstanding]
                if not stale:
                    continue
                emit(
                    message_push(
                        agent_id,
                        lease_id,
                        [snapshot.messages[i] for i in sorted(stale)],
                        redelivery=True,
                        max_chars=max_chars,
                    )
                )
                now = _loop_time()
                for message_id in stale:
                    outstanding[message_id] = now
                last_emit = now
                continue
            # The DB clock grants/revokes authority. Local time only shortens a
            # wait before its deadline; clock skew must not revoke a live lease
            # or spin on a past local timestamp while the DB still grants it.
            await listener.wait_one(max(0.5, min(catchup_seconds, _seconds_left(snapshot))))
    finally:
        await listener.close()


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
        max_chars = _CLAUDE_MAX_CHARS if args.provider == "claude" else None

        async def run() -> None:
            async def read_inbox() -> InboxSnapshot:
                return await asyncio.to_thread(_read_inbox, args.agent_id, lease_id, token)

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
                    max_chars=max_chars,
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
