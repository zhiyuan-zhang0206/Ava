"""Committed-id resolution for the inbound reconcile (task #4788).

The inbound reconcile (``agent.startup._reconcile_claimed_inbounds_at_startup``)
needs the set of ``ava_inbound_id``s that a claimed chat row committed. The
historical source — the settled checkpoint's message list — on a
delta-written thread rebuilds the whole write chain (up to ~1000 writes /
~10MB), slow and, under load, killed by the server statement budget.

This module side-loads committed ids from settled writes: a claim that
committed must have appended its message *after* the row was created, so its
write lies in the claim window when checkpoint and database clocks differ by
at most the configured pad. Only writes on the settled checkpoint's ancestry
whose successor advanced the messages channel count. The window is bounded
by the fresh-claim scope (rows at or
past the stale threshold dead-letter regardless of this answer, so only the
youngest claims need coverage) and by a row cap; anything the window cannot
cheaply prove triggers a streaming scan of all settled writes, then a full
checkpoint read for ids still unproven. An applied id found in the window is a
commit proof, so this also finalizes a message committed and later removed —
a shape the list-presence check reset toward re-delivery.

The window proof presumes the thread stores its messages as write rows (the
delta write model, task #3180). A missing fresh id never proves absence: the
caller scans all settled writes, reads the settled checkpoint if needed, and
unions any ids already proven from writes. This also covers materialized
checkpoint values on pre-switch threads and snapshot-only `update_state`
bootstraps.

Read-only: nothing here writes — a storage read that sits beside the checkpoint
read-compat layer (`base/agents/history/delta_read_compat.py`); the reconcile's
status transitions stay in `agent/db/__init__.py`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.sql import SQL
from psycopg_pool import AsyncConnectionPool

from base.config import settings
from base.log import logger


async def claimed_reconcile_scope(
    pool: AsyncConnectionPool, agent_id: int
) -> tuple[bool, datetime | None, set[int]]:
    """(any claimed chats?, earliest fresh creation?, fresh claimed ids).

    One guard read decides the reconcile's whole source strategy:

    - no claimed rows → nothing to finalize; the caller skips the checkpoint
      read entirely;
    - claimed rows, none younger than the stale threshold → every row is
      dead-lettered by the reconcile regardless of the committed set, so the
      caller skips the read as well;
    - otherwise the returned timestamp is the earliest creation of a fresh
      claim. A row reset and re-claimed has a new ``claimed_at`` but an older
      commit may belong to its first claim.
    """
    stale_cutoff_s = settings.daemon.delivery_watchdog_stale_claimed_threshold_seconds
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*), min(LEAST(created_at, COALESCE(claimed_at, created_at))) FILTER ("
            "WHERE COALESCE(claimed_at, created_at)"
            " >= now() - make_interval(secs => %s)), "
            "array_agg(id) FILTER (WHERE COALESCE(claimed_at, created_at) "
            ">= now() - make_interval(secs => %s)) "
            "FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'chat' AND status = 'claimed'",
            (stale_cutoff_s, stale_cutoff_s, agent_id),
        )
        row = await cur.fetchone()
    if row is None:
        return False, None, set()
    return int(row[0]) > 0, row[1], set(row[2] or [])


async def sideload_committed_ids(
    pool: AsyncConnectionPool,
    checkpointer: AsyncPostgresSaver,
    agent_id: int,
    *,
    since: datetime,
) -> set[int] | None:
    """Committed ``ava_inbound_id``s provable from the claim-window writes.

    ``None`` means the window could not be resolved (boundary, row cap, or
    undecodable write). An empty set means the bounded window proved no ids.
    Either case triggers an all-ancestor scan for unproven fresh claims.
    """
    pad_s = settings.daemon.inbound_reconcile_clock_pad_seconds
    scan_limit = settings.daemon.inbound_reconcile_boundary_scan_limit
    row_cap = settings.daemon.inbound_reconcile_window_row_cap

    cutoff = since - timedelta(seconds=pad_s)
    resolved, boundary = await _claim_window_start(pool, agent_id, cutoff, scan_limit=scan_limit)
    if not resolved:
        logger.warning(
            "inbound reconcile side-load boundary unresolved; scanning settled history",
            event="inbound_reconcile_sideload_fallback",
            agent_id=agent_id,
            reason="boundary_scan_limit",
        )
        return None

    rows = await _messages_writes_in_window(pool, agent_id, boundary, row_cap=row_cap)
    if rows is None:
        logger.warning(
            "inbound reconcile side-load window exceeds row cap; scanning settled history",
            event="inbound_reconcile_sideload_fallback",
            agent_id=agent_id,
            reason="window_row_cap",
        )
        return None

    ids: set[int] = set()
    serde = checkpointer.serde
    try:
        for type_tag, blob in rows:
            _collect_inbound_ids(serde.loads_typed((type_tag, blob)), ids)
    except Exception:
        logger.opt(exception=True).warning(
            "inbound reconcile side-load decode failed; scanning settled history",
            event="inbound_reconcile_sideload_fallback",
            agent_id=agent_id,
            reason="decode_error",
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
    set without touching the checkpoint. A resolved window proves positive ids.
    If it cannot cover every fresh claim, scan all settled ancestor writes to
    find commits outside the time bound, then read the full checkpoint for any
    remaining ids that may live in materialized state. Union all positive
    proofs so a committed-then-removed message still finalizes.
    """
    any_claimed, window_since, fresh_ids = await claimed_reconcile_scope(pool, agent_id)
    if not any_claimed or window_since is None:
        return set()
    sideloaded = await sideload_committed_ids(pool, checkpointer, agent_id, since=window_since)
    if sideloaded is not None and fresh_ids <= sideloaded:
        return sideloaded
    historical = await _committed_ids_from_all_settled_writes(
        pool, checkpointer, agent_id, fresh_ids
    )
    proven = (sideloaded or set()) | historical
    if fresh_ids <= proven:
        return proven
    full_read_ids = await _committed_ids_from_settled_checkpoint(checkpointer, agent_id)
    return full_read_ids | proven


async def _committed_ids_from_all_settled_writes(
    pool: AsyncConnectionPool,
    checkpointer: AsyncPostgresSaver,
    agent_id: int,
    wanted_ids: set[int],
) -> set[int]:
    """Stream the settled ancestry until every fresh id is found or it ends.

    This slower fallback does not depend on checkpoint timestamps. It keeps a
    committed-then-removed id recoverable across arbitrary historical skew.
    A failed scan raises so no absent id is reset on incomplete evidence.
    """
    sql, params = _settled_writes_query(agent_id, boundary=None, row_limit=None)
    ids: set[int] = set()
    try:
        async with (
            pool.connection() as conn,
            conn.cursor() as cur,
            # Psycopg implements stream() as an async generator but types it AsyncIterator.
            aclosing(
                cast("AsyncGenerator[tuple[Any, Any], None]", cur.stream(sql, params, size=64))
            ) as rows,
        ):
            async for type_tag, blob in rows:
                _collect_inbound_ids(
                    checkpointer.serde.loads_typed((str(type_tag), bytes(blob))), ids
                )
                if wanted_ids <= ids:
                    return ids
    except Exception:
        logger.exception(
            "inbound reconcile full settled-write scan failed; preserving claimed rows",
            event="inbound_reconcile_sideload_fallback",
            agent_id=agent_id,
            reason="full_write_scan_failed",
        )
        raise
    return ids


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
    ``False`` means the boundary sits beyond the scan limit. Missing or
    unreadable timestamps never qualify as a boundary. A wrong historical
    clock can omit a positive id here, which the all-ancestor fallback finds.
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


def _before(ts: Any, cutoff: datetime) -> bool:
    """True only when ``ts`` is confidently older than ``cutoff``."""
    parsed = _parse_ts(ts)
    return parsed is not None and parsed < cutoff


def _parse_ts(ts: Any) -> datetime | None:
    if not isinstance(ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


async def _messages_writes_in_window(
    pool: AsyncConnectionPool, agent_id: int, boundary: str | None, *, row_cap: int
) -> list[tuple[str, bytes]] | None:
    """Settled messages writes after ``boundary`` (``None`` = whole thread).

    Fetches one row past the cap to distinguish "exactly at cap" from "over
    cap"; over the cap returns ``None`` — the caller falls back rather than
    trust a truncated window.
    """
    sql, params = _settled_writes_query(agent_id, boundary, row_limit=row_cap + 1)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)
        rows = await cur.fetchall()
    if len(rows) > row_cap:
        return None
    return [(str(type_tag), bytes(blob)) for type_tag, blob in rows]


def _settled_writes_query(
    agent_id: int, boundary: str | None, *, row_limit: int | None
) -> tuple[SQL, list[Any]]:
    """Select writes the latest settled checkpoint incorporated on its ancestry."""
    # A write on the latest checkpoint is still pending. A control-only
    # successor cannot turn its parent's pending message write into a commit.
    sql = (
        "WITH RECURSIVE settled AS ("
        " SELECT checkpoint_id, parent_checkpoint_id, checkpoint FROM checkpoints "
        " WHERE thread_id = %s AND checkpoint_ns = '' "
        " AND checkpoint_id = (SELECT max(checkpoint_id) FROM checkpoints "
        "   WHERE thread_id = %s AND checkpoint_ns = '') "
        " UNION ALL "
        " SELECT parent.checkpoint_id, parent.parent_checkpoint_id, parent.checkpoint "
        " FROM checkpoints parent "
        " JOIN settled child ON parent.checkpoint_id = child.parent_checkpoint_id "
        " WHERE parent.thread_id = %s AND parent.checkpoint_ns = '' "
        " AND (%s::text IS NULL OR parent.checkpoint_id > %s)"
        ") SELECT writes.type, writes.blob FROM checkpoint_writes writes "
        "JOIN settled child ON child.parent_checkpoint_id = writes.checkpoint_id "
        "JOIN checkpoints parent ON parent.checkpoint_id = writes.checkpoint_id "
        "AND parent.thread_id = %s AND parent.checkpoint_ns = '' "
        "WHERE writes.thread_id = %s AND writes.checkpoint_ns = '' "
        "AND writes.channel = 'messages' "
        "AND child.checkpoint->'channel_versions' ? 'messages' "
        "AND child.checkpoint->'channel_versions'->>'messages' "
        "IS DISTINCT FROM parent.checkpoint->'channel_versions'->>'messages' "
        "AND (%s::text IS NULL OR writes.checkpoint_id > %s) "
    )
    params: list[Any] = [
        str(agent_id),
        str(agent_id),
        str(agent_id),
        boundary,
        boundary,
        str(agent_id),
        str(agent_id),
        boundary,
        boundary,
    ]
    if row_limit is not None:
        sql += " LIMIT %s"
        params.append(row_limit)
    return SQL(sql), params


def _collect_inbound_ids(value: Any, ids: set[int]) -> None:
    """Extract ``ava_inbound_id``s from one decoded write value.

    Write values for the messages channel are a list of message-likes or a
    single message-like (see ``base.agents.history.delta_read_compat._fold_messages``).
    """
    raw_values = cast("list[Any]", value if isinstance(value, list) else [value])
    for item in raw_values:
        kwargs = getattr(item, "additional_kwargs", None)
        if not isinstance(kwargs, dict):
            continue
        inbound_id = cast("dict[str, Any]", kwargs).get("ava_inbound_id")
        if isinstance(inbound_id, int):
            ids.add(inbound_id)
