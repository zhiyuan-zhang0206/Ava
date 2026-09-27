"""Committed-id resolution for the inbound reconcile (task #4788).

The inbound reconcile (``agent.startup._reconcile_claimed_inbounds_at_startup``)
needs the set of ``ava_inbound_id``s that a claimed chat row committed. The
historical source — the settled checkpoint's message list — on a
delta-written thread rebuilds the whole write chain (up to ~1000 writes /
~10MB), slow and, under load, killed by the server statement budget.

This module side-loads the same id set from the writes themselves: a claim
that committed must have appended its message *after* the claim, so every
committed id lies in the writes whose checkpoints are newer than the claim
window's start. The window is bounded by the fresh-claim scope (rows at or
past the stale threshold dead-letter regardless of this answer, so only the
youngest claims need coverage) and by a row cap; anything the window cannot
cheaply prove falls back to the full checkpoint read, which stays the
correctness backstop. An id found in the window is a direct commit proof, so
this also finalizes a message committed and later removed — a shape the
list-presence check reset toward re-delivery.

The window proof presumes the thread stores its messages as write rows (the
delta write model, task #3180): a thread whose messages live only in
materialized checkpoint values — pre-switch threads, snapshot-only
`update_state` bootstraps — has no writes to read, and an empty window there
proves nothing, so it falls back to the full read as well.

Read-only: nothing here writes — a storage read that sits beside the checkpoint
read-compat layer (`shared/agents/history/delta_read_compat.py`); the reconcile's
status transitions stay in `agent/db.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from shared.config import settings
from shared.log import logger


async def claimed_reconcile_scope(
    pool: AsyncConnectionPool, agent_id: int
) -> tuple[bool, datetime | None]:
    """(any claimed chat rows?, earliest claim still inside the stale window).

    One guard read decides the reconcile's whole source strategy:

    - no claimed rows → nothing to finalize; the caller skips the checkpoint
      read entirely;
    - claimed rows, none younger than the stale threshold → every row is
      dead-lettered by the reconcile regardless of the committed set, so the
      caller skips the read as well;
    - otherwise the returned timestamp is the oldest claim the committed set
      can still affect — the window start the side-load must cover.
    """
    stale_cutoff_s = settings.daemon.delivery_watchdog_stale_claimed_threshold_seconds
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*), min(COALESCE(claimed_at, created_at)) FILTER ("
            "WHERE COALESCE(claimed_at, created_at)"
            " >= now() - make_interval(secs => %s)) "
            "FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'chat' AND status = 'claimed'",
            (stale_cutoff_s, agent_id),
        )
        row = await cur.fetchone()
    if row is None:
        return False, None
    return int(row[0]) > 0, row[1]


async def sideload_committed_ids(
    pool: AsyncConnectionPool,
    checkpointer: AsyncPostgresSaver,
    agent_id: int,
    *,
    since: datetime,
) -> set[int] | None:
    """Committed ``ava_inbound_id``s provable from the claim-window writes.

    ``None`` means the window could not be resolved (boundary scan or row cap
    exceeded, no messages write rows on the thread, undecodable write) — the
    caller must fall back to the full checkpoint read. An empty set is a
    resolved answer: nothing committed.
    """
    pad_s = settings.daemon.inbound_reconcile_clock_pad_seconds
    scan_limit = settings.daemon.inbound_reconcile_boundary_scan_limit
    row_cap = settings.daemon.inbound_reconcile_window_row_cap

    cutoff = since - timedelta(seconds=pad_s)
    resolved, boundary = await _claim_window_start(pool, agent_id, cutoff, scan_limit=scan_limit)
    if not resolved:
        logger.warning(
            "inbound reconcile side-load unresolved — boundary beyond the scan "
            "limit; falling back to the full checkpoint read",
            event="inbound_reconcile_sideload_fallback",
            agent_id=agent_id,
            reason="boundary_scan_limit",
        )
        return None

    rows = await _messages_writes_in_window(pool, agent_id, boundary, row_cap=row_cap)
    if rows is None:
        logger.warning(
            "inbound reconcile side-load unresolved — claim window exceeds the "
            "row cap; falling back to the full checkpoint read",
            event="inbound_reconcile_sideload_fallback",
            agent_id=agent_id,
            reason="window_row_cap",
        )
        return None

    if not rows and not await _messages_write_evidence(pool, agent_id):
        logger.warning(
            "inbound reconcile side-load unresolved — the thread has no messages "
            "write rows (messages live in materialized checkpoint values); "
            "falling back to the full checkpoint read",
            event="inbound_reconcile_sideload_fallback",
            agent_id=agent_id,
            reason="no_write_evidence",
        )
        return None

    ids: set[int] = set()
    serde = checkpointer.serde
    try:
        for type_tag, blob in rows:
            _collect_inbound_ids(serde.loads_typed((type_tag, blob)), ids)
    except Exception:
        logger.warning(
            "inbound reconcile side-load decode failed — falling back to the full checkpoint read",
            event="inbound_reconcile_sideload_fallback",
            agent_id=agent_id,
            reason="decode_error",
            exc_info=True,
        )
        return None
    return ids


async def committed_ids_for_reconcile(
    pool: AsyncConnectionPool,
    checkpointer: AsyncPostgresSaver,
    agent_id: int,
) -> set[int]:
    """The committed ``ava_inbound_id`` set the reconcile finalizes against.

    Guard first — no claimed row that any answer can change returns the empty
    set without touching the checkpoint; then the claim-window side-load; then
    the full checkpoint read as the correctness backstop.
    """
    any_claimed, window_since = await claimed_reconcile_scope(pool, agent_id)
    if not any_claimed or window_since is None:
        return set()
    sideloaded = await sideload_committed_ids(pool, checkpointer, agent_id, since=window_since)
    if sideloaded is not None:
        return sideloaded
    return await _committed_ids_from_settled_checkpoint(checkpointer, agent_id)


async def _committed_ids_from_settled_checkpoint(
    checkpointer: AsyncPostgresSaver, agent_id: int
) -> set[int]:
    """Committed ids from the settled checkpoint's message list — fallback read."""
    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}
    ckpt = await checkpointer.aget(config)
    messages = (ckpt or {}).get("channel_values", {}).get("messages", [])
    committed_inbound_ids: set[int] = set()
    for msg in messages:
        kwargs = cast("dict[str, Any]", getattr(msg, "additional_kwargs", None) or {})
        ava_id = kwargs.get("ava_inbound_id")
        if isinstance(ava_id, int):
            committed_inbound_ids.add(ava_id)
    return committed_inbound_ids


async def _claim_window_start(
    pool: AsyncConnectionPool, agent_id: int, cutoff: datetime, *, scan_limit: int
) -> tuple[bool, str | None]:
    """(resolved, first checkpoint strictly older than ``cutoff``).

    Scans the newest ``scan_limit`` checkpoints newest-first; the first one
    confidently older than the cutoff bounds the window — every write at a
    newer checkpoint id is inside it. ``(True, None)`` means the whole thread
    history is inside the window (boundary before the thread's first write).
    ``False`` means the boundary sits beyond the scan limit. Checkpoints whose
    ``ts`` is missing or unreadable never qualify (they would bound the window
    too tight); they just cost a scan slot.
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT checkpoint_id, checkpoint->>'ts' AS ts FROM checkpoints "
            "WHERE thread_id = %s AND checkpoint_ns = '' "
            "ORDER BY checkpoint_id DESC LIMIT %s",
            (str(agent_id), scan_limit),
        )
        rows = await cur.fetchall()
    for checkpoint_id, ts in rows:
        if _before(ts, cutoff):
            return True, str(checkpoint_id)
    return len(rows) < scan_limit, None


async def _messages_write_evidence(pool: AsyncConnectionPool, agent_id: int) -> bool:
    """Whether this thread stores its messages as write rows at all.

    A thread on the delta write model records every append as a `messages`
    write row; one whose messages live only in materialized checkpoint values
    has none. On such a thread an empty window cannot prove "nothing
    committed" — the caller must fall back to the full checkpoint read.
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM checkpoint_writes "
            "WHERE thread_id = %s AND checkpoint_ns = '' AND channel = 'messages' "
            "LIMIT 1",
            (str(agent_id),),
        )
        return await cur.fetchone() is not None


def _before(ts: Any, cutoff: datetime) -> bool:
    """True only when ``ts`` is confidently older than ``cutoff``."""
    if not isinstance(ts, str):
        return False
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed < cutoff


async def _messages_writes_in_window(
    pool: AsyncConnectionPool, agent_id: int, boundary: str | None, *, row_cap: int
) -> list[tuple[str, bytes]] | None:
    """Messages-channel write rows after ``boundary`` (``None`` = whole thread).

    Fetches one row past the cap to distinguish "exactly at cap" from "over
    cap"; over the cap returns ``None`` — the caller falls back rather than
    trust a truncated window.
    """
    sql = (
        "SELECT type, blob FROM checkpoint_writes "
        "WHERE thread_id = %s AND checkpoint_ns = '' AND channel = 'messages'"
    )
    params: list[Any] = [str(agent_id)]
    if boundary is not None:
        sql += " AND checkpoint_id > %s"
        params.append(boundary)
    sql += " ORDER BY checkpoint_id, task_id, idx LIMIT %s"
    params.append(row_cap + 1)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)
        rows = await cur.fetchall()
    if len(rows) > row_cap:
        return None
    return [(str(type_tag), bytes(blob)) for type_tag, blob in rows]


def _collect_inbound_ids(value: Any, ids: set[int]) -> None:
    """Extract ``ava_inbound_id``s from one decoded write value.

    Write values for the messages channel are a list of message-likes or a
    single message-like (see ``shared.agents.history.delta_read_compat._fold_messages``).
    """
    raw_values = cast("list[Any]", value if isinstance(value, list) else [value])
    for item in raw_values:
        kwargs = getattr(item, "additional_kwargs", None)
        if not isinstance(kwargs, dict):
            continue
        inbound_id = cast("dict[str, Any]", kwargs).get("ava_inbound_id")
        if isinstance(inbound_id, int):
            ids.add(inbound_id)
