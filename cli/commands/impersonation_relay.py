"""Push a durable inbox through the bound relay, without ACK or lease renewal.

Each message uses the delivery budget and ACK window snapshotted on its lease.
Reservations survive relay restarts. Exhaustion expires the takeover
through normal native handoff, preserving all unacknowledged content.
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

import shared.redis_listener
from cli.commands.codex_app_server import live_submit, require_control_endpoint
from shared.agents.impersonation import RELAY_HEARTBEAT_SECONDS
from shared.agents.impersonation.impersonation_delivery import reserve_delivery
from shared.config import settings

_CATCHUP_SECONDS = 30.0
_MIN_EMIT_INTERVAL_SECONDS = 2.0
# Bound each message in the host context; full bodies remain in the durable
# inbox. The same cap also respects Claude Monitor's per-line budget.
_PUSH_MAX_CHARS = 2000
_TERMINAL = frozenset({"released", "rejected", "expired"})
type LeaseStatus = Literal["requested", "accepted", "active", "released", "rejected", "expired"]


class _Lease(BaseModel):
    id: UUID
    agent_id: int
    status: LeaseStatus
    expires_at: datetime
    ack_window_seconds: int
    max_delivery_attempts: int
    relay_batch_window_seconds: int = 0
    start_message: str = ""
    rejection_reason: str | None = None


@dataclass(frozen=True)
class InboxMessage:
    """One durable inbox row crossing the host wake boundary with its body."""

    id: int
    kind: str
    source: str
    content: str
    delivery_attempts: int = 0
    delivery_due: bool = True


@dataclass(frozen=True)
class InboxSnapshot:
    """Pending rows plus the lease facts the delivery loop needs.

    ``messages`` carries each row's full body keyed by id — the push envelope
    is built from it, so the host never has to read the inbox itself.
    ``routine_ids`` is the subset of ``message_ids`` that the merge window may
    coalesce (not a user chat, not a cancel, not a reminder); anything outside
    it pushes immediately. ``batch_window`` is the lease's configured merge
    window in seconds; 0 keeps the pre-window behaviour. ``start_message`` is
    the native agent's briefing; empty only for leases accepted before
    the push protocol shipped.
    """

    message_ids: frozenset[int]
    messages: dict[int, InboxMessage]
    expires_at: datetime
    status: LeaseStatus = "active"
    routine_ids: frozenset[int] = frozenset()
    batch_window: float = 0.0
    start_message: str = ""
    end_reason: str | None = None
    ack_window_seconds: int = 180
    max_delivery_attempts: int = 2

    @property
    def active(self) -> bool:
        return self.status == "active"


class WakeListener(Protocol):
    async def ensure_listening(self) -> None: ...

    async def wait_one(self, timeout: float) -> None: ...

    async def close(self) -> None: ...


def ack_command(lease_id: int | UUID, ids: Sequence[int], agent_id: int | None = None) -> str:
    """The exact ACK command for one pushed batch, on this interpreter."""
    return shlex.join(
        [
            sys.executable,
            "-m",
            "cli",
            "impersonate",
            "ack",
            str(lease_id),
            *map(str, ids),
            *(["--agent", str(agent_id)] if isinstance(lease_id, int) else []),
        ]
    )


def message_push(
    agent_id: int,
    lease_id: int | UUID,
    messages: Sequence[InboxMessage],
    *,
    ack_window_seconds: int,
    max_delivery_attempts: int,
    redelivery: bool = False,
    max_chars: int | None = None,
) -> str:
    """One self-contained envelope covering one batch of durable inbox rows.

    Carries the full content, the batch's message ids and the exact ACK
    command, so the external session processes messages without reading or
    parsing the inbox. A re-delivery push says so explicitly; the ids make it
    idempotent for a host that already handled the batch. ``max_chars`` bounds
    each content block to fit Claude Monitor's per-line budget and limit host
    context; the tail points at the inbox command for the full text.
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
        blocks.append(f"Delivery attempt {message.delivery_attempts + 1}/{max_delivery_attempts}")
        blocks.append(content)
        blocks.append("")
    blocks.append(f"ACK after processing: {ack_command(lease_id, ids, agent_id)}")
    blocks.append(
        f"ACK within {ack_window_seconds}s. "
        + (
            "This batch includes a final delivery; a missed ACK window ends impersonation."
            if any(m.delivery_attempts + 1 >= max_delivery_attempts for m in messages)
            else f"At most {max_delivery_attempts} total attempts per message; "
            "a missed final ACK window ends impersonation."
        )
    )
    return "\n".join(blocks)


def activation_hint(
    agent_id: int, lease_id: int | UUID, *, ack_window_seconds: int, max_delivery_attempts: int
) -> str:
    """Fallback start message for a lease accepted without one.

    New accepts require a nonempty start message, so this only serves leases
    accepted before the push protocol shipped. Updated to the push contract:
    messages arrive here, ACK each batch, inbox is the fallback read.
    """
    prefix = [sys.executable, "-m", "cli", "impersonate"]
    scope = ["--agent", str(agent_id)] if isinstance(lease_id, int) else []
    inbox = shlex.join([*prefix, "inbox", str(lease_id), *scope])
    ack = shlex.join([*prefix, "ack", str(lease_id), *scope])
    return (
        f"Ava control active: agent={agent_id} lease={lease_id}. "
        "Inbox messages are pushed to this session; after processing each batch "
        f"run: {ack} ID... Use {inbox} to read missed or truncated messages. "
        f"Each message has {max_delivery_attempts} delivery attempts, "
        f"with {ack_window_seconds}s to ACK each; exhaustion ends impersonation."
    )


def _ended(
    snapshot: InboxSnapshot, agent_id: int, lease_id: int | UUID, emit: Callable[[str], None]
) -> bool:
    if snapshot.status not in _TERMINAL:
        return False
    if snapshot.status != "released":
        emit(
            f"Ava control {snapshot.status}: agent={agent_id} lease={lease_id}. "
            "Do not use this identity. The native agent can continue its workflow. "
            "The relay did not ACK messages or renew the lease."
            + (f" Reason: {snapshot.end_reason}" if snapshot.end_reason else "")
        )
    return True


def monitor_claude(message: str) -> None:
    """Each flushed stdout line is a same-session Claude Monitor event."""
    print(message, flush=True)


def host_emitter(
    provider: str, thread_id: str | None, *, codex_remote: str | None = None
) -> Callable[[str], None]:
    """Resolve an explicit host destination before opening the inbox relay.

    Codex uses Steer delivery on the owning app server. Missing endpoints,
    refusals and transport failures stop the relay, preserving pending inbox
    rows for the normal handoff. Pending-mode queue delivery is not equivalent.
    """
    if provider == "codex":
        if thread_id is None:
            raise ValueError("codex relay requires --thread-id for an existing session")
        target = UUID(thread_id)
        endpoint = require_control_endpoint(codex_remote)

        def emit_codex(message: str) -> None:
            reason = live_submit(str(target), message, endpoint=endpoint)
            if reason is not None:
                raise RuntimeError(f"Codex Steer delivery failed: {reason}")

        return emit_codex
    if provider == "claude":
        if codex_remote is not None:
            raise ValueError("Claude Monitor does not use --codex-remote")
        if thread_id is not None:
            raise ValueError("Claude Monitor routes to its owner; omit --thread-id")
        return monitor_claude
    raise ValueError(f"Unknown relay provider: {provider}")


def _read_inbox(agent_id: int, lease_id: UUID, token: str) -> InboxSnapshot:
    from shared.agents import impersonation

    lease = _Lease.model_validate(impersonation.relay_get(str(lease_id), token))
    if lease.agent_id != agent_id or lease.id != lease_id:
        raise ValueError("The impersonation lease does not belong to the requested agent")
    if lease.status != "active":
        return InboxSnapshot(
            frozenset(), {}, lease.expires_at, lease.status, end_reason=lease.rejection_reason
        )
    try:
        # relay_inbox validates same-machine active authority in its own transaction.
        rows = impersonation.relay_inbox(str(lease_id), token)
    except impersonation.ImpersonationError:
        latest = _Lease.model_validate(impersonation.relay_get(str(lease_id), token))
        if latest.status in _TERMINAL:
            return InboxSnapshot(
                frozenset(),
                {},
                latest.expires_at,
                latest.status,
                end_reason=latest.rejection_reason,
            )
        raise
    messages = {
        row["id"]: InboxMessage(
            id=row["id"],
            kind=row["kind"],
            source=row["source"],
            content=row["content"],
            delivery_attempts=row["delivery_attempts"],
            delivery_due=row["delivery_due"],
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
        ack_window_seconds=lease.ack_window_seconds,
        max_delivery_attempts=lease.max_delivery_attempts,
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


def _due_ids(snapshot: InboxSnapshot, *, redelivery: bool) -> frozenset[int]:
    return frozenset(
        message.id
        for message in snapshot.messages.values()
        if message.delivery_due
        and message.delivery_attempts < snapshot.max_delivery_attempts
        and (message.delivery_attempts > 0) == redelivery
    )


async def relay_inbox(  # noqa: PLR0915 — one consent/window/reservation delivery loop
    agent_id: int,
    lease_id: int | UUID,
    *,
    read_inbox: Callable[[], Awaitable[InboxSnapshot]],
    reserve: Callable[[list[int]], Awaitable[frozenset[int]]],
    listener: WakeListener,
    emit: Callable[[str], None],
    debounce: float = 0.5,
    catchup_seconds: float = _CATCHUP_SECONDS,
    max_chars: int | None = None,
) -> None:
    """Deliver consent/start first, then respect the lease budget per pending row.

    Database time and durable reservations own the ACK windows and budget;
    local monotonic time only controls batching/rate limits. Every read
    reconciles exhaustion across the whole lease, even outside this inbox
    page. Due retries precede fresh rows so arrivals cannot starve them.
    Reserve immediately before the host call, rechecking ACK/release races.
    A failed or ambiguous host submission spends an attempt, never an ACK.
    """
    if not math.isfinite(debounce) or not 0 <= debounce <= _CATCHUP_SECONDS:
        raise ValueError(f"debounce must be between 0 and {_CATCHUP_SECONDS:g} seconds")
    if not math.isfinite(catchup_seconds) or catchup_seconds <= 0:
        raise ValueError("catchup_seconds must be finite and positive")
    start_sent = False
    last_emit = float("-inf")
    routine_deadline: float | None = None
    activation_pending: frozenset[int] = frozenset()
    try:
        initial = await read_inbox()
        if _ended(initial, agent_id, lease_id, emit):
            return
        await listener.ensure_listening()
        while True:
            snapshot = await read_inbox()
            if _ended(snapshot, agent_id, lease_id, emit):
                return
            if not snapshot.active:
                await listener.wait_one(max(0.5, min(catchup_seconds, _seconds_left(snapshot))))
                continue
            if not start_sent:
                activation_pending = snapshot.message_ids
                hint = activation_hint(
                    agent_id,
                    lease_id,
                    ack_window_seconds=snapshot.ack_window_seconds,
                    max_delivery_attempts=snapshot.max_delivery_attempts,
                )
                start_message = snapshot.start_message or hint
                if snapshot.start_message and isinstance(lease_id, int):
                    start_message += "\n\n" + hint
                emit(start_message)
                start_sent = True
                last_emit = _loop_time()
                continue
            redelivery = bool(_due_ids(snapshot, redelivery=True))
            due = _due_ids(snapshot, redelivery=redelivery)
            if due:
                if not redelivery and not (due & activation_pending):
                    routine_deadline, remaining = _window_wait(
                        snapshot,
                        new_ids=due,
                        routine_deadline=routine_deadline,
                        now=_loop_time(),
                    )
                    if remaining is not None:
                        await listener.wait_one(
                            max(0.5, min(catchup_seconds, _seconds_left(snapshot), remaining))
                        )
                        continue
                # Catch ACK/release during debounce and merge bursts. Claude
                # Monitor replenishes one event allowance per two seconds.
                await asyncio.sleep(
                    max(debounce, last_emit + _MIN_EMIT_INTERVAL_SECONDS - _loop_time())
                )
                snapshot = await read_inbox()
                if _ended(snapshot, agent_id, lease_id, emit):
                    return
                if not snapshot.active:
                    continue
                redelivery = bool(_due_ids(snapshot, redelivery=True))
                due = _due_ids(snapshot, redelivery=redelivery)
                reserved: frozenset[int] = await reserve(sorted(due)) if due else frozenset()
                if reserved:
                    emit(
                        message_push(
                            agent_id,
                            lease_id,
                            [snapshot.messages[i] for i in sorted(reserved)],
                            ack_window_seconds=snapshot.ack_window_seconds,
                            max_delivery_attempts=snapshot.max_delivery_attempts,
                            redelivery=redelivery,
                            max_chars=max_chars,
                        )
                    )
                    last_emit = _loop_time()
                routine_deadline = None
                continue
            # The database owns expiry; periodic catchup also repairs missed
            # Redis wakes and observes the final ACK window's expiration.
            await listener.wait_one(max(0.5, min(catchup_seconds, _seconds_left(snapshot))))
    finally:
        await listener.close()


def _write_heartbeat(lease_id: UUID, token: str) -> bool:
    """One durable liveness beat; False means the lease ended or the relay
    credential was revoked — the heartbeat loop stops silently."""
    from shared.agents import impersonation

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
    never renews the lease; its credential reads, reserves delivery, and beats.
    """
    from cli.commands import impersonation

    try:
        from shared.agents.impersonation import relay_get
        from shared.agents.impersonation.impersonation_history import resolve

        if args.lease_id is None:
            lease_id = UUID(str(resolve(args.agent_id, args.session_id)["id"]))
        else:
            lease_id = UUID(args.lease_id)
        if args.token_stdin:
            token = impersonation.relay_token_from_stdin()
        else:
            token = impersonation.relay_token_from_env()
        session_id = relay_get(str(lease_id), token)["session_id"]
        emit = host_emitter(args.provider, args.thread_id, codex_remote=args.codex_remote)
        max_chars = _PUSH_MAX_CHARS

        async def run() -> None:
            async def read_inbox() -> InboxSnapshot:
                return await asyncio.to_thread(_read_inbox, args.agent_id, lease_id, token)

            async def reserve(ids: list[int]) -> frozenset[int]:
                return await asyncio.to_thread(reserve_delivery, str(lease_id), token, ids)

            if not await asyncio.to_thread(_write_heartbeat, lease_id, token):
                return  # lease already terminal; nothing to relay
            heartbeat = asyncio.create_task(_heartbeat_loop(lease_id, token))
            try:
                listener = shared.redis_listener.RedisInboundListener(
                    settings.data_plane.redis_url, args.agent_id
                )
                await relay_inbox(
                    args.agent_id,
                    session_id,
                    read_inbox=read_inbox,
                    reserve=reserve,
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
