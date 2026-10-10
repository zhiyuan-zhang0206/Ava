"""Incomplete settled-write evidence must propagate instead of becoming absence."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from base.agents.history import inbound_sideload
from base.config import settings


def _read_inputs() -> inbound_sideload.ReconcileReadInputs:
    return inbound_sideload.ReconcileReadInputs(
        stale_claimed_seconds=lambda: (
            settings.daemon.delivery_watchdog_stale_claimed_threshold_seconds
        ),
        clock_pad_seconds=lambda: settings.daemon.inbound_reconcile_clock_pad_seconds,
        boundary_scan_limit=lambda: settings.daemon.inbound_reconcile_boundary_scan_limit,
        window_row_cap=lambda: settings.daemon.inbound_reconcile_window_row_cap,
    )


async def test_reconcile_propagates_database_stream_failure_without_checkpoint_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = RuntimeError("history unavailable")
    cursor = MagicMock()
    cursor.__aenter__.return_value = cursor

    async def unavailable():
        raise failure
        yield

    cursor.stream.return_value = unavailable()
    connection = MagicMock()
    connection.cursor.return_value = cursor
    connection.__aenter__.return_value = connection
    pool = MagicMock()
    pool.connection.return_value = connection
    saver = MagicMock(aget=AsyncMock())
    monkeypatch.setattr(
        inbound_sideload, "claimed_reconcile_scope", AsyncMock(return_value=(True, object(), {1}))
    )
    monkeypatch.setattr(inbound_sideload, "sideload_committed_ids", AsyncMock(return_value=None))
    with pytest.raises(RuntimeError) as caught:
        await inbound_sideload.committed_ids_for_reconcile(
            cast(AsyncConnectionPool, pool),
            cast(AsyncPostgresSaver, saver),
            1,
            inputs=_read_inputs(),
        )
    assert caught.value is failure
    saver.aget.assert_not_awaited()


async def test_live_cutoffs_belong_to_two_roots_and_the_guard_skips_window_readers(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
) -> None:
    import os
    import time
    from unittest.mock import patch

    from base.config import ConfigBoot
    from base.db import create_agent

    agent = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,kind,source,content,status,claimed_at) "
        "VALUES(%s,'chat','user','still claimed','claimed',clock_timestamp()-interval '30 seconds')",
        (agent,),
    )
    db_conn.commit()
    events: list[str] = []

    def unused() -> int:
        raise AssertionError("an empty fresh-claim scope must not read the window bounds")

    try:
        with patch.dict(os.environ):
            first, second = ConfigBoot(), ConfigBoot()
            first.set_field("delivery_watchdog_stale_claimed_threshold_seconds", 10.0)
            second.set_field("delivery_watchdog_stale_claimed_threshold_seconds", 100.0)

            def inputs(owner: ConfigBoot, name: str) -> inbound_sideload.ReconcileReadInputs:
                def stale() -> float:
                    events.append(name)
                    return owner.view.daemon.delivery_watchdog_stale_claimed_threshold_seconds

                return inbound_sideload.ReconcileReadInputs(stale, unused, unused, unused)

            first_inputs, second_inputs = inputs(first, "first"), inputs(second, "second")
            assert events == [] and not first.is_full() and not second.is_full()
            saver = cast(AsyncPostgresSaver, MagicMock())
            assert (
                await inbound_sideload.committed_ids_for_reconcile(
                    aops_pool, saver, agent, inputs=first_inputs
                )
                == set()
            )
            any_claimed, since, fresh_ids = await inbound_sideload.claimed_reconcile_scope(
                aops_pool, agent, inputs=second_inputs
            )
            assert any_claimed and since is not None and len(fresh_ids) == 1
            first.set_field("delivery_watchdog_stale_claimed_threshold_seconds", 100.0)
            assert (
                await inbound_sideload.claimed_reconcile_scope(
                    aops_pool, agent, inputs=first_inputs
                )
            )[2] == fresh_ids
            second.set_field("delivery_watchdog_stale_claimed_threshold_seconds", 10.0)
            assert (
                await inbound_sideload.claimed_reconcile_scope(
                    aops_pool, agent, inputs=second_inputs
                )
            )[1:] == (None, set())
            assert events == ["first", "second", "first", "second"]
    finally:
        time.tzset()


async def test_window_readers_keep_their_operation_order() -> None:
    from datetime import UTC, datetime

    events: list[str] = []

    def stale() -> float:
        raise AssertionError("the side-load operation does not read the guard cutoff")

    def pad() -> float:
        events.append("pad")
        return 2.0

    def scan() -> int:
        events.append("scan")
        return 5

    def cap() -> int:
        events.append("cap")
        return 7

    inputs = inbound_sideload.ReconcileReadInputs(stale, pad, scan, cap)
    assert events == []
    cursor = MagicMock(execute=AsyncMock(), fetchall=AsyncMock(return_value=[]))
    cursor.__aenter__.return_value = cursor
    connection = MagicMock()
    connection.cursor.return_value = cursor
    connection.__aenter__.return_value = connection
    pool = MagicMock()
    pool.connection.return_value = connection
    since = datetime.now(UTC)
    assert (
        await inbound_sideload.sideload_committed_ids(
            cast(AsyncConnectionPool, pool),
            cast(AsyncPostgresSaver, MagicMock()),
            1,
            since=since,
            inputs=inputs,
        )
        == set()
    )
    assert events == ["pad", "scan", "cap"]
