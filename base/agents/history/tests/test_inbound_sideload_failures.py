"""Incomplete settled-write evidence must propagate instead of becoming absence."""

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from base.agents.history import inbound_sideload


async def test_reconcile_does_not_fall_back_after_incomplete_full_write_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = cast(AsyncConnectionPool, MagicMock())
    saver = cast(AsyncPostgresSaver, MagicMock())
    monkeypatch.setattr(
        inbound_sideload, "claimed_reconcile_scope", AsyncMock(return_value=(True, object(), {1}))
    )
    monkeypatch.setattr(inbound_sideload, "sideload_committed_ids", AsyncMock(return_value=None))
    monkeypatch.setattr(
        inbound_sideload,
        "_committed_ids_from_all_settled_writes",
        AsyncMock(side_effect=RuntimeError("history unavailable")),
    )
    full_read = AsyncMock()
    monkeypatch.setattr(inbound_sideload, "_committed_ids_from_settled_checkpoint", full_read)

    with pytest.raises(RuntimeError, match="history unavailable"):
        await inbound_sideload.committed_ids_for_reconcile(pool, saver, 1)
    full_read.assert_not_awaited()


async def test_full_write_scan_propagates_database_stream_failure() -> None:
    cursor = MagicMock()
    cursor.__aenter__.return_value = cursor

    async def unavailable():
        raise RuntimeError("history unavailable")
        yield

    cursor.stream.return_value = unavailable()
    connection = MagicMock()
    connection.cursor.return_value = cursor
    connection.__aenter__.return_value = connection
    pool = MagicMock()
    pool.connection.return_value = connection

    with pytest.raises(RuntimeError, match="history unavailable"):
        await inbound_sideload._committed_ids_from_all_settled_writes(
            cast(AsyncConnectionPool, pool), cast(AsyncPostgresSaver, MagicMock()), 1, {1}
        )
