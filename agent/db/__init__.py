"""Kernel-side asynchronous queue operations. AgentHost owns runtime admission, status, leases and Redis subscriptions; graph nodes claim and reconcile durable inbound messages."""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NamedTuple, TypeVar, cast
from weakref import WeakKeyDictionary

import psycopg
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.ownership.inbound import lock_inbound_owner
from agent.ownership.native_cancel import bound_native_cancel
from base.agents.compaction.models import CompactHeldError
from base.agents.impersonation.wake import publish_inbound_wake
from base.agents.inbox.live import ALIVE_STATUSES
from base.agents.incarnation.native_work_models import NativeCancelPendingError, NativeWorkTarget
from base.agents.messages.inbound import InterruptReason
from base.clock import Timers
from base.config import settings
from base.db import Database
from base.db.transaction import async_write_transaction
from base.events.live.bus import EventBus
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation

# The slow-borrow threshold is settings.agent.db_pool_slow_acquire_warn_seconds: a
# slow-but-served borrow is visible below the acquire timeout.


# Claim is a hot scheduling boundary, not a general database query. Bound both
# waiting for a client-pool slot and waiting on ownership row locks; a timeout
# unwinds the transaction and lets the host scheduler retry
# from the durable inbound row on a later round.
def claim_db_acquire_timeout_s() -> float:
    return min(5.0, settings.agent.db_pool_acquire_timeout_seconds)


_CT = TypeVar("_CT", bound=psycopg.AsyncConnection[Any])


@dataclass
class _BorrowSpan:
    slot_wait_ms: float = 0.0
    check_ms: float = 0.0
    check_attempts: int = 0


class LoggingConnectionPool(AsyncConnectionPool[_CT]):
    """`AsyncConnectionPool` that names a slow or timed-out connection borrow.

    The pool's `getconn` wait is the agent's only DB backpressure, and its
    default is a silent multi-second block — which is exactly why a transiently
    unresponsive Postgres reads as a mystery node stall rather than a DB
    problem. This override times every borrow: a slow-but-served one logs a
    WARNING, a `PoolTimeout` an ERROR, both tagged with `pool_name`, so a degraded pg
    surfaces in the agent log at the moment it happens. Behaviour is otherwise
    identical to the base pool (the exception still propagates).
    """

    def __init__(self, *args: Any, pool_name: str, timers: Timers, **kwargs: Any) -> None:
        self._pool_name = pool_name
        self._timers = timers
        # The borrow in flight on each task of THIS pool: `_getconn_unchecked` and
        # `_check_connection` run inside that task and add their share to its span.
        self._borrow_spans: WeakKeyDictionary[asyncio.Task[Any], _BorrowSpan] = WeakKeyDictionary()
        super().__init__(*args, **kwargs)

    def _acquire_stats(self) -> dict[str, int]:
        """Stable diagnostic fields across psycopg-pool versions and states."""
        stats = self.get_stats()
        return {
            "pool_size": stats.get("pool_size", 0),
            "pool_available": stats.get("pool_available", 0),
            "requests_waiting": stats.get("requests_waiting", 0),
            "connections_errors": stats.get("connections_errors", 0),
        }

    async def _getconn_unchecked(self, timeout: float) -> _CT:
        # Includes the pool lock and client-side queue, not server-side PgBouncer wait.
        started = self._timers.monotonic()
        try:
            return await super()._getconn_unchecked(timeout)
        finally:
            task = asyncio.current_task()
            span = self._borrow_spans.get(task) if task is not None else None
            if span is not None:
                span.slot_wait_ms += (self._timers.monotonic() - started) * 1000

    async def _check_connection(self, conn: _CT) -> None:
        started = self._timers.monotonic()
        try:
            await super()._check_connection(conn)
        finally:
            task = asyncio.current_task()
            span = self._borrow_spans.get(task) if task is not None else None
            if span is not None:
                span.check_ms += (self._timers.monotonic() - started) * 1000
                span.check_attempts += 1

    async def getconn(self, timeout: float | None = None) -> _CT:
        t0 = self._timers.monotonic()
        span = _BorrowSpan()
        task = asyncio.current_task()
        assert task is not None  # noqa: S101 — async pool borrow runs in a task
        self._borrow_spans[task] = span
        try:
            conn = await super().getconn(timeout=timeout)
        except PoolTimeout:
            stats = self._acquire_stats()
            logger.error(
                "[db pool] {name} acquire timed out after {elapsed:.1f}s "
                "(max_size={mx}, size={pool_size}, available={pool_available}, "
                "waiting={requests_waiting}, connection_errors={connections_errors}) "
                "— Postgres unreachable or the client pool is saturated",
                event="db_pool_acquire_timeout",
                name=self._pool_name,
                elapsed=self._timers.monotonic() - t0,
                slot_wait_ms=span.slot_wait_ms,
                check_ms=span.check_ms,
                check_attempts=span.check_attempts,
                mx=self.max_size,
                **stats,
            )
            raise
        else:
            elapsed = self._timers.monotonic() - t0
            if elapsed >= settings.agent.db_pool_slow_acquire_warn_seconds:
                stats = self._acquire_stats()
                logger.warning(
                    "[db pool] {name} acquire took {elapsed:.1f}s "
                    "(size={pool_size}, available={pool_available}, "
                    "waiting={requests_waiting}, connection_errors={connections_errors})",
                    event="db_pool_acquire_slow",
                    name=self._pool_name,
                    elapsed=elapsed,
                    slot_wait_ms=span.slot_wait_ms,
                    check_ms=span.check_ms,
                    check_attempts=span.check_attempts,
                    **stats,
                )
            return conn
        finally:
            del self._borrow_spans[task]


class ClaimedInbound(NamedTuple):
    """One inbound row returned by `claim_inbound_batch`.

    `source` is the message channel ('system' / 'user' / 'agent:N' / 'ui:page:<name>' / 'watcher:N' / 'schedule:N' etc.); the
    claim node uses it via `base/agents/messages/envelope.py:wrap_inbound` to add an envelope
    prefix for kind='chat'; lifecycle kinds directly assemble the marker text
    without going through wrap_inbound.
    """

    id: int
    agent_id: int
    content: str
    kind: str
    source: str
    payload: dict[str, object] | None = None
    created_at: datetime | None = None
    claimed_at: datetime | None = None
    durable_lifecycle: bool = False
    """Internal dispatch fact from the locked command pointer, never payload input."""

    @classmethod
    def from_row(cls, row: tuple[Any, ...]) -> "ClaimedInbound":
        id_, tid, content, kind, source, payload, created_at, claimed_at = row[:8]
        return cls(id_, tid, content, kind, source, payload, created_at, claimed_at)


_MAX_SPAWNER_ANCESTOR_DEPTH = 32
_FATAL_PROVIDER_REPORT_LOCATION = "agent.turn.runloop._handle_fatal_llm_error"


async def enqueue_fatal_provider_report_to_nearest_alive_ancestor(
    pool: AsyncConnectionPool,
    failed_agent_id: int,
    *,
    db: Database,
    bus: EventBus,
    error_class: str,
    provider: str | None,
    vendor: str | None,
    status: int | None,
    reason: str,
    occurred_at: datetime,
) -> int | None:
    """Deliver a metadata-only permanent-provider report to the nearest live parent.

    The recursive walk follows only the immutable ``agents_meta.born_spawner``
    ``agent:<id>`` edge.
    It deliberately ignores fleet-view relationships and stops when no
    ancestor has the canonical live status-and-lease predicate.
    The note text is assembled from structured classifier fields; it never
    receives the provider exception or agent history, which might contain the
    content that the provider rejected. ``vendor`` is the model's provider key
    — the account the request bills to (deepseek / claude / …); ``provider``
    is the SDK/protocol path that raised (anthropic for DeepSeek's compat
    endpoint). The note names both, vendor first, so the report identifies
    the billed account and not only the path (task #3916).

    Returns the notified ancestor id, or ``None`` when no live birth ancestor
    exists. The inbound transaction commits before its best-effort Redis wake.
    """
    content = (
        f"Descendant agent {failed_agent_id} is blocked after a permanent provider rejection. "
        f"error_class={error_class} vendor={vendor} provider={provider} status={status} reason={reason} "
        f"timestamp={occurred_at.isoformat()} where={_FATAL_PROVIDER_REPORT_LOCATION}"
    )
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        await cur.execute(
            "WITH RECURSIVE spawner_walk(agent_id, spawner, status, lease_expires_at, path, depth) "
            "AS ("
            "  SELECT id, born_spawner, status, lease_expires_at, ARRAY[id], 0 "
            "  FROM agents_meta WHERE id = %s "
            "  UNION ALL "
            "  SELECT parent.id, parent.born_spawner, parent.status, parent.lease_expires_at, "
            "         walk.path || parent.id, walk.depth + 1 "
            "  FROM spawner_walk walk "
            "  JOIN agents_meta parent "
            "    ON parent.id = substring(walk.spawner FROM '^agent:([0-9]+)$')::bigint "
            "  WHERE walk.depth < %s AND NOT parent.id = ANY(walk.path)"
            ") "
            "SELECT agent_id FROM spawner_walk "
            "WHERE depth > 0 AND status = ANY(%s) AND lease_expires_at > now() "
            "ORDER BY depth ASC LIMIT 1",
            (failed_agent_id, _MAX_SPAWNER_ANCESTOR_DEPTH, list(ALIVE_STATUSES)),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        ancestor_id = cast(int, row[0])
        await cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, %s, 'system_note', 'system', %s::jsonb) RETURNING id",
            (ancestor_id, content, json.dumps({"note_tag": "agent_reply"})),
        )
        inbound_row = await cur.fetchone()
        if inbound_row is None:
            raise RuntimeError("expected inbound row after fatal provider ancestor report insert")
        inbound_id = cast(int, inbound_row[0])
    await asyncio.to_thread(publish_inbound_wake, db, bus, ancestor_id, str(inbound_id))
    return ancestor_id


async def _claim_lifecycle_command(
    conn: psycopg.AsyncConnection[Any],
    cur: psycopg.AsyncCursor[Any],
    agent_id: int,
    *,
    incarnation: RuntimeIncarnation | None,
) -> list[ClaimedInbound] | None:
    """Accept the agent's owned restart/terminate command; None when there is none."""
    from agent.ownership.lifecycle_intent import (
        accept_lifecycle_intent,
        settle_superseded_intent,
    )

    command = await accept_lifecycle_intent(conn, agent_id, incarnation=incarnation)
    token = incarnation
    if (
        command is not None
        and token is not None
        and (command.generation != token.generation or command.owner != token.owner)
    ):
        if not await settle_superseded_intent(conn, command):
            raise RuntimeError("replacement cannot execute or settle the prior lifecycle target")
        command = await accept_lifecycle_intent(conn, agent_id, incarnation=incarnation)
    if command is not None:
        if token is None or (command.generation, command.owner) != (
            token.generation,
            token.owner,
        ):
            raise RuntimeError("lifecycle dispatch target is not the current incarnation")
        await cur.execute(
            "SELECT i.id,i.agent_id,i.content,i.kind,i.source,i.payload,"
            "i.created_at,i.claimed_at FROM inbound_messages i JOIN agents_meta m "
            "ON m.id=i.agent_id AND m.lifecycle_command_id=i.id "
            "WHERE i.id=%s AND m.id=%s AND i.status='claimed' "
            "AND i.kind IN ('restart','terminate') "
            "AND i.target_generation=m.runtime_generation "
            "AND i.target_owner=m.runtime_owner "
            "AND m.runtime_generation=%s AND m.runtime_owner=%s",
            (command.id, agent_id, token.generation, token.owner),
        )
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError("accepted lifecycle command disappeared")
        return [ClaimedInbound.from_row(row)._replace(durable_lifecycle=True)]
    return None


async def _refuse_unadmitted_lifecycle(cur: psycopg.AsyncCursor[Any], agent_id: int) -> None:
    await cur.execute(
        "SELECT id FROM inbound_messages WHERE agent_id=%s AND status='pending' "
        "AND kind IN ('restart','terminate') ORDER BY id LIMIT 1 FOR UPDATE",
        (agent_id,),
    )
    if await cur.fetchone() is not None:
        raise RuntimeError("lifecycle claim requires an admitted runtime incarnation")


async def claim_inbound_batch(
    pool: AsyncConnectionPool,
    agent_id: int,
    *,
    lifecycle_only: bool = False,
    incarnation: RuntimeIncarnation | None,
    work: NativeWorkTarget | None,
) -> list[ClaimedInbound]:
    """Claim under current runtime ownership in one explicit write transaction.

    Owned restart/terminate commands are accepted serially and returned alone,
    retaining their fixed target and active pointer until observed or failed.
    The internal dispatch fact is minted only from that locked pointer, never
    caller payload. Other inbounds remain pending for the successor.

    Without an active lifecycle command, chat becomes claimed for checkpoint
    reconciliation and other kinds become done. An unowned consumer cannot
    acknowledge lifecycle work it has no authority to apply.

    The lifecycle-only path leaves cancellation for the external decision
    owner, or the ordinary native claim if that owner returns without ACK.
    """
    acquire_timeout_s = claim_db_acquire_timeout_s()
    async with (
        async_write_transaction(pool, timeout=acquire_timeout_s) as conn,
        conn.cursor() as cur,
    ):
        await conn.execute(
            "SELECT set_config('lock_timeout', %s, true)", (f"{acquire_timeout_s:g}s",)
        )
        await lock_inbound_owner(conn, agent_id, incarnation=incarnation)
        native_cancel = await bound_native_cancel(
            conn, agent_id, incarnation=incarnation, work=work
        )
        if native_cancel is not None:
            raise NativeCancelPendingError(native_cancel)
        compact = await (
            await conn.execute(
                "SELECT 1 FROM native_compact_commands WHERE agent_id=%s AND released_at IS NULL",
                (agent_id,),
            )
        ).fetchone()
        if compact is not None:
            raise CompactHeldError("guarded compact must settle before generic inbound claim")
        await cur.execute("SELECT runtime_kind FROM agents_meta WHERE id=%s", (agent_id,))
        runtime = await cur.fetchone()
        runtime_owned = runtime == ("hosted",)
        if runtime_owned:
            accepted = await _claim_lifecycle_command(conn, cur, agent_id, incarnation=incarnation)
            if accepted is not None:
                return accepted
        else:
            await _refuse_unadmitted_lifecycle(cur, agent_id)
        if lifecycle_only:
            # Accepted intents returned above. A held native runtime has no
            # authority to acknowledge ordinary input in the generic batch.
            return []
        # CASE-on-kind in a single UPDATE keeps the batch grab atomic — chat
        # and non-chat rows in the same batch all commit together. RETURNING
        # order for UPDATE … WHERE id IN (subquery) is heap-scan order, not
        # the subquery's ORDER BY; Python-side sort by created_at re-imposes
        # FIFO.
        await cur.execute(
            "UPDATE inbound_messages SET status = "
            "CASE WHEN kind = 'chat' THEN 'claimed' ELSE 'done' END, "
            "claimed_at = now() "
            "WHERE id IN ("
            "  SELECT id FROM inbound_messages "
            "  WHERE status = 'pending' AND agent_id = %s "
            "  AND (NOT %s OR kind NOT IN ('restart','terminate')) "
            "  ORDER BY created_at ASC, id ASC "
            "  FOR UPDATE SKIP LOCKED"
            ") RETURNING id, agent_id, content, kind, source, payload, created_at, claimed_at",
            (agent_id, runtime_owned),
        )
        rows = await cur.fetchall()
        if rows:
            await cur.execute(
                "UPDATE agents_meta "
                "SET wake_suppressed_until=NULL, wake_suppress_reason=NULL "
                "WHERE id=%s AND wake_suppressed_until IS NOT NULL",
                (agent_id,),
            )
    rows.sort(key=lambda r: (r[6], r[0]))  # created_at FIFO, id breaks timestamp ties
    return [ClaimedInbound.from_row(r) for r in rows]


async def finalize_claimed_inbounds(
    pool: AsyncConnectionPool | None,
    agent_id: int,
    *,
    incarnation: RuntimeIncarnation | None,
    unknown_execution: bool = False,
) -> int:
    """Consume every claimed chat of this agent under the owner lock.

    The queue's done status means consumed, not successfully executed: a claim
    whose execution was interrupted has an unknown result and is never
    redelivered. Used at the compaction boundary and when a turn ends, aborts or
    a runtime is rebuilt. Co-batched chats deferred before any execution remain
    pending for the fresh context. The caller must hold the admitted identity. A
    missing pool in container/eval mode is a no-op.
    """
    if pool is None:
        return 0
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        await lock_inbound_owner(conn, agent_id, incarnation=incarnation)
        await cur.execute(
            "UPDATE inbound_messages SET status = 'done', "
            "payload = CASE WHEN %s THEN COALESCE(payload, '{}'::jsonb) "
            "|| jsonb_build_object('consumed_without_execution', true) ELSE payload END "
            "WHERE status = 'claimed' AND kind = 'chat' AND agent_id = %s "
            "RETURNING id, kind, content, "
            "EXTRACT(EPOCH FROM (now() - created_at))::float8",
            (unknown_execution, agent_id),
        )
        rows = await cur.fetchall()
    for inbound_id, kind, content, age_s in rows if unknown_execution else ():
        logger.warning(
            "claimed inbound {inbound_id} consumed without execution evidence",
            event="inbound_consumed_unknown",
            agent_id=agent_id,
            inbound_id=int(inbound_id),
            kind=str(kind),
            age_s=float(age_s),
            content_preview=content[:200],
        )
    return len(rows)


async def has_pending_inbound_after(
    pool: AsyncConnectionPool,
    agent_id: int,
    after_id: int,
) -> bool:
    """Whether any status='pending' inbound for `agent_id` has id > `after_id`.

    The claim node's terminate veto uses this as a final queue recheck: after
    claiming a batch, a pending row newer than the whole batch means the world
    moved while the exit was being decided — the terminate must yield so the
    fresh message gets processed instead of being stranded by the process death
    (see the veto block in `agent/graph/claim/node.py` claim_node routing).
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM inbound_messages "
            "WHERE status = 'pending' AND agent_id = %s AND id > %s LIMIT 1",
            (agent_id, after_id),
        )
        return await cur.fetchone() is not None


async def has_pending_interrupt(
    pool: AsyncConnectionPool,
    agent_id: int,
    *,
    incarnation: RuntimeIncarnation | None,
    work: NativeWorkTarget | None,
) -> bool:
    """Whether a durable external abort is present, without claiming it."""
    return (
        await pending_interrupt_reason(pool, agent_id, incarnation=incarnation, work=work)
        is not None
    )


async def pending_interrupt_reason(
    pool: AsyncConnectionPool,
    agent_id: int,
    *,
    incarnation: RuntimeIncarnation | None,
    work: NativeWorkTarget | None,
) -> InterruptReason | None:
    """Attribute the first queued external abort without claiming its command.

    A status='pending' EXTERNAL interrupt (kind cancel/terminate) fires: the
    in-flight llm/exec node polls this (via `subscribe_interrupt`) to abort the
    current action the moment a pause/terminate lands — without claiming the
    row; the dispatch itself (cancel -> idle, terminate -> END) stays in the
    claim node, the single owner of inbound semantics. So does a claimed
    terminate whose application is durable but not yet observed.

    Self-initiated lifecycle (`source='self'`, i.e. `meiva.self.terminate()` /
    restart from inside the agent's own exec) is EXCLUDED: that path already
    raises `LifecycleExit` inside the exec child, which the exec node handles
    directly, so the watcher must not also fire on the agent's own row — doing
    so would race the clean lifecycle exit with an external cancel. Only
    external interrupts (user / admin / peer) need the in-flight abort; the
    self row is still dispatched normally at claim.
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        if (
            await bound_native_cancel(conn, agent_id, incarnation=incarnation, work=work)
            is not None
        ):
            return InterruptReason.USER
        await cur.execute(
            "SELECT i.source FROM inbound_messages i WHERE i.agent_id=%s "
            "AND i.source <> 'self' AND i.kind IN ('cancel','terminate') AND "
            "(i.status='pending' OR (i.status='claimed' AND i.kind='terminate' "
            "AND i.applied_at IS NOT NULL AND i.observed_at IS NULL AND EXISTS ("
            "SELECT 1 FROM agents_meta m WHERE m.id=i.agent_id "
            "AND m.lifecycle_command_id=i.id AND m.runtime_kind='hosted' "
            "AND m.runtime_generation=i.target_generation AND m.runtime_owner=i.target_owner))) "
            "ORDER BY i.id LIMIT 1",
            (agent_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return InterruptReason.USER if row[0] == "user" else InterruptReason.SYSTEM
