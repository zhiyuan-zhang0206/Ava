"""Batch acquisition for the claim node: the idle wait loop and its upkeep.

Extracted from agent/graph/_claim.py (Task #1006 split — the batch-claim axis).
Behavior preserved verbatim:

1. First SELECT pending once (guards against race where ava.self.compact has
   INSERTed but the publish already passed) — lives in _claim_node_impl.
2. No pending and (halted or messages empty) → Redis pub/sub wait
   (_wait_for_batch: mark idling → wait_for_inbound → claim → mark running).
   The try/BaseException handler guarantees the status returns to RUNNING on
   any wait failure — a stuck 'idling' row would make the restarter miss the
   agent.
3. (Checkpoint trimming moved out of the agent process — the gateway-side
   events-maintenance daemon now owns retention: see
   services/events_maintenance/checkpoint_reaper.py. The agent only writes
   checkpoints; it never deletes them.)
4. _defer_chats_to_pending reverts co-batched chat rows to 'pending' so a
   compaction is a clean wipe (used by the compact path in _claim_decide).
"""

from __future__ import annotations

from contextlib import suppress

from psycopg_pool import AsyncConnectionPool

from agent.db import (
    ClaimedInbound,
    claim_inbound_batch,
    enter_idling_state,
    mark_agent_status,
    wait_for_inbound,
)
from agent.graph._context import AvaContext
from agent.inbound_ownership import RuntimeOwnershipLostError
from shared.agents import AgentStatus
from shared.db_transaction import async_write_transaction
from shared.log import logger
from shared.trace import claim_idle_wait_span


class LifecycleCasLostError(Exception):
    """Claim-time lifecycle race: between the idle flip and the IDLING→RUNNING
    claim flip, a concurrent op (terminate / reaper) moved the `agents_meta`
    row out of 'idling' and a retry from the live state also lost.

    Raised by `_wait_for_batch` instead of letting mark_agent_status's
    RuntimeError kill the process: a crash during a network outage cannot be
    resurrected (agent 2147, 2026-08-03: 4h dead; Task #688). The claim node
    catches it and ENDs the process cleanly, leaving the row to the owning
    controller (swap-in / restarter / resurrect).
    """


async def _wait_for_batch(
    ctx: AvaContext,
    agent_id: int,
) -> list:
    """Enter IDLING wait loop, block until an inbound batch is claimable.

    Mark idling → Redis pub/sub wait → mark running → return batch. Extracted
    as a separate function to reduce claim_node's branch count (PLR0912).
    The wait runs inside `claim_idle_wait_span()`, which ends the node's
    `execute_task claim` span at the park boundary and records the park as an
    explicit `claim idle-wait` span — a long idle wait shows as a labeled
    span, not a giant opaque node span.

    Caller (claim_node) has already narrowed ops_pool / inbound_listener not
    None (container path early-returned); this function narrows once more so
    pyright sees it — invariant: those handles None never reach here.
    """

    pool = ctx.ops_pool
    listener = ctx.inbound_listener
    if pool is None or listener is None:
        raise RuntimeError(
            "_wait_for_batch called with ctx.ops_pool=None or ctx.inbound_listener=None — caller bug "
            "(container mode should have early-returned at the top of claim_node)"
        )

    await enter_idling_state(pool, agent_id)
    batch: list[ClaimedInbound] = []
    try:
        with claim_idle_wait_span():
            while not batch:
                await wait_for_inbound(pool, listener, agent_id=agent_id)
                batch = await claim_inbound_batch(pool, agent_id)
    except RuntimeOwnershipLostError as exc:
        raise LifecycleCasLostError(str(exc)) from exc
    except BaseException:
        with suppress(Exception):
            await mark_agent_status(
                pool, agent_id, AgentStatus.RUNNING, expected_from=AgentStatus.IDLING
            )
        raise
    try:
        await mark_agent_status(
            pool, agent_id, AgentStatus.RUNNING, expected_from=AgentStatus.IDLING
        )
    except RuntimeError:
        # CAS lost — a concurrent lifecycle op (terminate / reaper) moved
        # the row between the idle flip and this flip. Re-read and adapt
        # instead of crashing (a crash mid-outage
        # cannot be resurrected — agent 2147, 2026-08-03; Task #688; the
        # restart-path twin of this race was fixed in #1299): retry from a
        # live state, otherwise another op owns the row — log and signal the
        # claim node to END cleanly.
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
            row = await cur.fetchone()
        actual = row[0] if row else None
        if actual is not None and actual in (AgentStatus.RUNNING, AgentStatus.IDLING):
            try:
                await mark_agent_status(pool, agent_id, AgentStatus.RUNNING, expected_from=actual)
                return batch
            except RuntimeError:
                actual = None  # moved again mid-retry — treat as foreign
        if actual != AgentStatus.RUNNING:
            logger.warning(
                "claim CAS lost for agent {agent_id} (actual status {actual}) -- "
                "another lifecycle op owns the row; exiting cleanly",
                event="claim_cas_lost",
                agent_id=agent_id,
                actual=actual,
            )
        raise LifecycleCasLostError(
            f"agents_meta row {agent_id} left 'idling' during the claim flip "
            f"(actual status {actual!r}); exiting cleanly"
        ) from None
    return batch


async def _defer_chats_to_pending(
    pool: AsyncConnectionPool | None, agent_id: int, chat_ids: list[int]
) -> None:
    """Revert co-batched chat inbounds to 'pending' so a compaction is a clean wipe.

    A chat that lands in the same claim batch as a compact_summary /
    compact_request arrived while the agent's turn was in flight: it was never
    part of the summarized history, so it must not be lost. The old behavior
    parked it raw after the summary (the extra_msgs tail) — the user-observed
    bug where original messages survived a compact. Reverting the row to
    pending instead lets the next claim deliver it in the freshly established
    context, where it belongs (claim re-delivers pending chats on the next
    wake). ``pool is None`` (container / eval mode, which has no inbound queue)
    is a no-op.
    """
    if pool is None or not chat_ids:
        return
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE inbound_messages SET status = 'pending', claimed_at = NULL "
            "WHERE id = ANY(%s) AND agent_id = %s",
            (chat_ids, agent_id),
        )
