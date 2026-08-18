"""Shared test fakes for agent_graph node tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


def make_fake_ops_pool() -> AsyncMock:
    """AsyncConnectionPool stub: `async with pool.connection() as conn:` +
    `async with conn.cursor() as cur:` both work, and `await cur.fetchall()`
    returns [].

    Supports the SQL the graph nodes touch without a real DB — claim's
    pre-SELECT inbound batch + mark_agent_status, and node_lifecycle's
    chat-anchor read (`agent/db.py:list_chat_inbound_anchors`). Tests that
    need real rows back use the `aops_pool` fixture (real pool) instead.
    """
    cur = AsyncMock()
    cur.fetchall.return_value = []
    # Queries answer "no rows" — a truthy MagicMock here would make
    # `has_pending_interrupt` report a pending cancel/terminate on every fake
    # runtime, spuriously firing the interrupt watcher (the llm/exec nodes
    # poll it while the agent runs).
    cur.fetchone.return_value = None
    cur_cm = AsyncMock()
    cur_cm.__aenter__.return_value = cur
    cur_cm.__aexit__.return_value = None
    conn = AsyncMock()
    conn.cursor = MagicMock(return_value=cur_cm)
    conn_cm = AsyncMock()
    conn_cm.__aenter__.return_value = conn
    conn_cm.__aexit__.return_value = None
    pool = AsyncMock()
    pool.connection = MagicMock(return_value=conn_cm)
    # Expose the shared cursor so a test can introspect the SQL the nodes ran
    # (e.g. assert a completed turn issued the last_active_at UPDATE).
    pool.fake_cursor = cur
    return pool
