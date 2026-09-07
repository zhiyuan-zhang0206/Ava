"""Defer co-batched chats until compaction has installed its clean context."""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from agent.inbound_ownership import lock_inbound_owner
from shared.db_transaction import async_write_transaction


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
        await lock_inbound_owner(conn, agent_id)
        await cur.execute(
            "UPDATE inbound_messages SET status = 'pending', claimed_at = NULL "
            "WHERE id = ANY(%s) AND agent_id = %s AND kind = 'chat' AND status = 'claimed'",
            (chat_ids, agent_id),
        )
