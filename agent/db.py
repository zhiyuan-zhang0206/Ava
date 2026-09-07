"""Kernel-side asynchronous queue operations. AgentHost owns runtime admission, status, leases and Redis subscriptions; graph nodes claim and reconcile durable inbound messages."""

import asyncio
import json
import time
from datetime import datetime
from typing import Any, NamedTuple, TypeVar, cast

import psycopg
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.inbound_ownership import lock_inbound_owner
from shared.config import settings
from shared.db import ALIVE_STATUSES, InboundRow, publish_inbound_wake
from shared.db_transaction import async_write_transaction
from shared.log import logger

# A successful borrow that took at least this long still gets a WARNING — a
# healthy pg hands a conn back in milliseconds, so seconds means it is under
# load (or reconnecting). Below the acquire timeout, so a slow-but-served
# borrow is visible before it ever escalates to a hard PoolTimeout.
_SLOW_ACQUIRE_WARN_S = 3.0

# Claim is a hot scheduling boundary, not a general database query. Bound both
# waiting for a client-pool slot and waiting on ownership row locks; a timeout
# unwinds the transaction and lets the host scheduler retry
# from the durable inbound row on a later round.
_CLAIM_DB_ACQUIRE_TIMEOUT_S = min(5.0, settings.agent.db_pool_acquire_timeout_seconds)
_CLAIM_DB_LOCK_TIMEOUT = f"{_CLAIM_DB_ACQUIRE_TIMEOUT_S:g}s"

_CT = TypeVar("_CT", bound=psycopg.AsyncConnection[Any])


class LoggingConnectionPool(AsyncConnectionPool[_CT]):
    """`AsyncConnectionPool` that names a slow or timed-out connection borrow.

    The pool's `getconn` wait is the agent's only DB backpressure, and its
    default is a silent multi-second block — which is exactly why a transiently
    unresponsive Postgres reads as a mystery node stall rather than a DB
    problem. This override times every borrow: a slow-but-served one logs a
    WARNING, a `PoolTimeout` an ERROR, both tagged with `pool_name`, so a
    degraded pg surfaces in the agent log at the moment it happens. Behaviour is
    otherwise identical to the base pool (the exception still propagates).
    """

    def __init__(self, *args: Any, pool_name: str, **kwargs: Any) -> None:
        self._pool_name = pool_name
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

    async def getconn(self, timeout: float | None = None) -> _CT:
        t0 = time.monotonic()
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
                elapsed=time.monotonic() - t0,
                mx=self.max_size,
                **stats,
            )
            raise
        elapsed = time.monotonic() - t0
        if elapsed >= _SLOW_ACQUIRE_WARN_S:
            stats = self._acquire_stats()
            logger.warning(
                "[db pool] {name} acquire took {elapsed:.1f}s "
                "(size={pool_size}, available={pool_available}, "
                "waiting={requests_waiting}, connection_errors={connections_errors})",
                event="db_pool_acquire_slow",
                name=self._pool_name,
                elapsed=elapsed,
                **stats,
            )
        return conn


class ClaimedInbound(NamedTuple):
    """One inbound row returned by `claim_inbound_batch`.

    `source` is the message channel ('system' / 'user' / 'agent:N' / 'ui:page:<name>' / 'watcher:N' / 'schedule:N' etc.); the
    claim node uses it via `shared/envelope.py:wrap_inbound` to add an envelope
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
_FATAL_PROVIDER_REPORT_LOCATION = "agent._runloop._handle_fatal_llm_error"


async def enqueue_fatal_provider_report_to_nearest_alive_ancestor(
    pool: AsyncConnectionPool,
    failed_agent_id: int,
    *,
    error_class: str,
    provider: str | None,
    status: int | None,
    reason: str,
    occurred_at: datetime,
) -> int | None:
    """Deliver a metadata-only permanent-provider report to the nearest live parent.

    The recursive walk follows only the immutable ``agents_meta.born_spawner``
    ``agent:<id>`` edge, falling back to ``spawner`` for pre-migration rows.
    It deliberately ignores fleet-view relationships and stops when no
    ancestor has the canonical live status-and-lease predicate.
    The note text is assembled from structured classifier fields; it never
    receives the provider exception or agent history, which might contain the
    content that the provider rejected.

    Returns the notified ancestor id, or ``None`` when no live birth ancestor
    exists. The inbound transaction commits before its best-effort Redis wake.
    """
    content = (
        f"Descendant agent {failed_agent_id} is blocked after a permanent provider rejection. "
        f"error_class={error_class} provider={provider} status={status} reason={reason} "
        f"timestamp={occurred_at.isoformat()} where={_FATAL_PROVIDER_REPORT_LOCATION}"
    )
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        await cur.execute(
            "WITH RECURSIVE spawner_walk(agent_id, spawner, status, lease_expires_at, path, depth) "
            "AS ("
            "  SELECT id, COALESCE(born_spawner, spawner), status, lease_expires_at, ARRAY[id], 0 "
            "  FROM agents_meta WHERE id = %s "
            "  UNION ALL "
            "  SELECT parent.id, COALESCE(parent.born_spawner, parent.spawner), parent.status, parent.lease_expires_at, "
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
    await asyncio.to_thread(publish_inbound_wake, ancestor_id, str(inbound_id))
    return ancestor_id


async def claim_inbound_batch(
    pool: AsyncConnectionPool,
    agent_id: int,
    *,
    lifecycle_only: bool = False,
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
    async with (
        async_write_transaction(pool, timeout=_CLAIM_DB_ACQUIRE_TIMEOUT_S) as conn,
        conn.cursor() as cur,
    ):
        await conn.execute("SELECT set_config('lock_timeout', %s, true)", (_CLAIM_DB_LOCK_TIMEOUT,))
        await lock_inbound_owner(conn, agent_id)
        await cur.execute("SELECT runtime_kind FROM agents_meta WHERE id=%s", (agent_id,))
        runtime = await cur.fetchone()
        runtime_owned = runtime in (("process",), ("hosted",))
        if runtime_owned:
            from agent.lifecycle_intent import accept_lifecycle_intent, settle_superseded_intent
            from shared.runtime_incarnation import current_incarnation

            command = await accept_lifecycle_intent(conn, agent_id)
            token = current_incarnation(agent_id)
            if (
                command is not None
                and token is not None
                and (command.generation != token.generation or command.owner != token.owner)
            ):
                if not await settle_superseded_intent(conn, command):
                    raise RuntimeError(
                        "replacement cannot execute or settle the prior lifecycle target"
                    )
                command = await accept_lifecycle_intent(conn, agent_id)
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
        else:
            await cur.execute(
                "SELECT id FROM inbound_messages WHERE agent_id=%s AND status='pending' "
                "AND kind IN ('restart','terminate') ORDER BY id LIMIT 1 FOR UPDATE",
                (agent_id,),
            )
            if await cur.fetchone() is not None:
                raise RuntimeError("lifecycle claim requires an admitted runtime incarnation")
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
    rows.sort(key=lambda r: r[6])  # created_at FIFO (index follows SELECT column count)
    return [ClaimedInbound.from_row(r) for r in rows]


async def list_chat_inbound_anchors(pool: AsyncConnectionPool, agent_id: int) -> list[InboundRow]:
    """Read this agent's kind='chat' inbound rows (any status) in created_at
    ascending order — ts anchors for rendering inbound HumanMessages in a
    timeline snapshot.

    These rows commit synchronously when INSERTed / claimed, so reading them
    never races the async checkpoint commit (the reason the timeline render
    moved off a checkpoint re-read).
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT id, content, kind, source, status, created_at, claimed_at "
            "FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'chat' ORDER BY created_at ASC",
            (agent_id,),
        )
        rows = await cur.fetchall()
    return [InboundRow(*r) for r in rows]


async def reconcile_claimed_inbounds(
    pool: AsyncConnectionPool,
    agent_id: int,
    committed_inbound_ids: set[int],
) -> tuple[int, int, int]:
    """Finalize every `'claimed'` row for `agent_id` based on whether its
    HumanMessage actually made it into `state.messages`.

    Called once on agent process startup. The new process reads its
    LangGraph checkpoint, extracts every `additional_kwargs.ava_inbound_id`
    from `state.messages` (caller-supplied `committed_inbound_ids`), and
    passes that set here. Each `'claimed'` row is then either:

      - **flipped to `'done'`** if its id is in `committed_inbound_ids` —
        the previous process's claim → langgraph commit chain completed
        successfully before exit;
      - **dead-lettered to `'done'`** if it is older than the stale-claimed
        threshold (`settings.daemon.delivery_watchdog_stale_claimed_threshold_seconds`,
        age from `claimed_at`, falling back to `created_at` for pre-column
        rows) — a claim that old is either already answered or hopelessly
        stale, and re-delivering it would surface an ancient message as
        fresh (Task #654: terminated agents pile up claimed rows forever;
        a resurrect with a pruned checkpoint would otherwise reset them all
        to 'pending' and flood the fresh context). The same cutoff the
        delivery watchdog's sweep applies to terminated owners, applied
        here at boot so the resurrect race is closed at the source;
      - **reset to `'pending'`** otherwise — the message was lost in
        transit (process crash mid-handling OR a silent saver-commit
        failure like agent 57). The same inbound will be re-claimed and
        re-delivered to the new process on its first claim cycle.

    Returns `(committed_count, reset_count, dead_lettered_count)` for
    logging. All writes happen in a single transaction; pool conns are
    `autocommit=True` so callers don't need an explicit commit.

    A fresh process is the only legitimate caller — the function assumes
    no other process is concurrently claiming for this agent_id (an agent
    is process-bound 1:1). Calling it mid-run would race with active claim
    nodes.
    """
    stale_cutoff_s = settings.daemon.delivery_watchdog_stale_claimed_threshold_seconds
    if not committed_inbound_ids:
        # No commits to confirm — every claimed row is an orphan, treat as
        # one reset path. (Common shape: brand-new process, no prior
        # in-flight work; the SELECT below will likely return 0 rows.)
        async with async_write_transaction(pool) as conn, conn.cursor() as cur:
            await lock_inbound_owner(conn, agent_id)
            await cur.execute(
                "UPDATE inbound_messages SET status = 'done' "
                "WHERE status = 'claimed' AND kind = 'chat' AND agent_id = %s "
                "  AND COALESCE(claimed_at, created_at) "
                "      < now() - make_interval(secs => %s)",
                (agent_id, stale_cutoff_s),
            )
            dead_lettered = cur.rowcount
            await cur.execute(
                "UPDATE inbound_messages SET status = 'pending' "
                "WHERE status = 'claimed' AND kind = 'chat' AND agent_id = %s",
                (agent_id,),
            )
            return (0, cur.rowcount, dead_lettered)

    # Wrap both updates in an explicit transaction so a crash between them
    # can't leave the table half-finalized (some claimed rows already
    # `done`, the rest still `claimed` waiting on a later boot). Pool conn
    # is autocommit=True; `conn.transaction()` opens an explicit BEGIN.
    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        await conn.execute("SET TRANSACTION READ WRITE")
        await lock_inbound_owner(conn, agent_id)
        await cur.execute(
            "UPDATE inbound_messages SET status = 'done' "
            "WHERE status = 'claimed' AND kind = 'chat' AND agent_id = %s AND id = ANY(%s)",
            (agent_id, sorted(committed_inbound_ids)),
        )
        committed = cur.rowcount
        await cur.execute(
            "UPDATE inbound_messages SET status = 'done' "
            "WHERE status = 'claimed' AND kind = 'chat' AND agent_id = %s "
            "  AND COALESCE(claimed_at, created_at) "
            "      < now() - make_interval(secs => %s)",
            (agent_id, stale_cutoff_s),
        )
        dead_lettered = cur.rowcount
        await cur.execute(
            "UPDATE inbound_messages SET status = 'pending' "
            "WHERE status = 'claimed' AND kind = 'chat' AND agent_id = %s",
            (agent_id,),
        )
        reset = cur.rowcount
    return (committed, reset, dead_lettered)


async def finalize_claimed_inbounds(pool: AsyncConnectionPool | None, agent_id: int) -> int:
    """Mark every 'claimed' row for `agent_id` as 'done' — the compaction
    terminal-consumption point.

    Companion to `reconcile_claimed_inbounds` for the compact path. A
    compaction REMOVE_ALLs the whole checkpoint history, so any claimed row
    whose HumanMessage lived there loses the `ava_inbound_id` that startup
    reconcile matches on; the next restart would see those rows as orphans,
    reset them to 'pending', and re-deliver already-answered messages — which
    surface to the user as a run of consecutive user messages with the
    (compacted) replies missing (Task #823).

    Compaction is the terminal consumption point for every claimed row: their
    content is folded into the summary (or was folded by an earlier
    compaction), so all of them finalize here. Only the co-batched chats —
    deferred to 'pending' by `_defer_chats_to_pending` before this runs —
    survive for re-delivery into the fresh context.

    Returns the number of rows finalized. Pool conns are `autocommit=True`;
    the UPDATE commits as soon as it executes. Safe on the compact path only:
    the agent is process-bound 1:1 and mid-turn, so no other writer is
    concurrently claiming for this agent_id. ``pool is None`` (container /
    eval mode, which has no inbound queue) is a no-op.
    """
    if pool is None:
        return 0
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        await lock_inbound_owner(conn, agent_id)
        await cur.execute(
            "UPDATE inbound_messages SET status = 'done' "
            "WHERE status = 'claimed' AND kind = 'chat' AND agent_id = %s",
            (agent_id,),
        )
        return cur.rowcount


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
    (see the veto block in `agent/graph/_claim.py` claim_node routing).
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM inbound_messages "
            "WHERE status = 'pending' AND agent_id = %s AND id > %s LIMIT 1",
            (agent_id, after_id),
        )
        return await cur.fetchone() is not None


async def has_pending_interrupt(pool: AsyncConnectionPool, agent_id: int) -> bool:
    """Whether a status='pending' EXTERNAL interrupt (kind cancel/terminate) is queued.

    The in-flight llm/exec node polls this (via `subscribe_interrupt`) to abort
    the current action the moment a pause/terminate lands — without claiming the
    row. The dispatch itself (cancel -> idle, terminate -> END) stays in the
    claim node, the single owner of inbound semantics.

    Self-initiated lifecycle (`source='self'`, i.e. `ava.self.terminate()` /
    restart from inside the agent's own exec) is EXCLUDED: that path already
    raises `_LifecycleExit` inside the exec child, which the exec node handles
    directly, so the watcher must not also fire on the agent's own row — doing
    so would race the clean lifecycle exit with an external cancel. Only
    external interrupts (user / admin / peer) need
    the in-flight abort; the self row is still dispatched normally at claim.
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM inbound_messages i WHERE i.agent_id=%s "
            "AND i.kind IN ('cancel','terminate') AND i.source <> 'self' "
            "AND (i.status='pending' OR (i.status='claimed' AND i.kind='terminate' "
            "AND i.applied_at IS NOT NULL AND i.observed_at IS NULL AND EXISTS ("
            "SELECT 1 FROM agents_meta m WHERE m.id=i.agent_id "
            "AND m.lifecycle_command_id=i.id AND m.runtime_kind='hosted' "
            "AND m.runtime_generation=i.target_generation AND m.runtime_owner=i.target_owner))) "
            "LIMIT 1",
            (agent_id,),
        )
        return await cur.fetchone() is not None
