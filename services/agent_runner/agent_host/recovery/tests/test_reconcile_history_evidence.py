"""Historical checkpoint proof survives clock skew and incomplete scans."""

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.graph.tests.cursor_fixture import _fresh_snapshot_cursor as _fresh_snapshot_cursor
from agent.startup import reconcile_claimed_inbounds_at_startup
from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from base.native_process.turn_identity import bind_turn_identity
from services.agent_runner.agent_host.recovery.tests.test_hosted_db_recovery import _admit
from services.agent_runner.agent_host.recovery.tests.test_reconcile_after_abort import (
    _build_graph,
    _CountingSaver,
    _insert_claimed,
    _seed_delta_written_checkpoint,
    _statuses,
)


async def test_checkpoint_clock_skew_scans_settled_history(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """A checkpoint clock over 300 seconds behind DB time cannot bound writes."""
    incarnation = await _admit(
        aops_pool, model_catalog=model_catalog, config_authority=config_authority
    )
    agent = incarnation.agent_id
    committed = _insert_claimed(db_conn, agent, "skewed")
    saver = await _seed_delta_written_checkpoint(aops_pool, agent, committed)
    db_conn.execute(
        "UPDATE checkpoints SET checkpoint = jsonb_set(checkpoint, '{ts}', to_jsonb(%s::text)) "
        "WHERE thread_id = %s",
        ((datetime.now(UTC) - timedelta(minutes=10)).isoformat(), str(agent)),
    )
    db_conn.commit()

    with bind_turn_identity(agent, incarnation=incarnation):
        await reconcile_claimed_inbounds_at_startup(aops_pool, saver, agent)

    assert saver.aget_calls == 0
    assert _statuses(db_conn, [committed]) == {committed: "done"}


async def test_historical_clock_skew_cannot_hide_a_fresh_commit(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """A later clock-correct checkpoint must not make an old skewed write disappear."""
    incarnation = await _admit(
        aops_pool, model_catalog=model_catalog, config_authority=config_authority
    )
    agent = incarnation.agent_id
    committed = _insert_claimed(db_conn, agent, "historically skewed")
    saver = await _seed_delta_written_checkpoint(aops_pool, agent, committed, remove_after=True)
    db_conn.execute(
        "UPDATE checkpoints SET checkpoint = jsonb_set(checkpoint, '{ts}', to_jsonb(%s::text)) "
        "WHERE thread_id = %s",
        ((datetime.now(UTC) - timedelta(minutes=10)).isoformat(), str(agent)),
    )
    db_conn.commit()
    graph = _build_graph(saver)
    await graph.aupdate_state(
        {"configurable": {"thread_id": str(agent)}}, {"halted": True}, as_node="work"
    )

    with bind_turn_identity(agent, incarnation=incarnation):
        await reconcile_claimed_inbounds_at_startup(aops_pool, saver, agent)

    assert saver.aget_calls == 0
    assert _statuses(db_conn, [committed]) == {committed: "done"}


async def test_incomplete_full_write_scan_preserves_claimed_row(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: Any,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """A failed history scan cannot turn missing proof into a reset."""
    incarnation = await _admit(
        aops_pool, model_catalog=model_catalog, config_authority=config_authority
    )
    agent = incarnation.agent_id
    claimed = _insert_claimed(db_conn, agent, "unresolved")
    saver = _CountingSaver(aops_pool)

    import base.agents.history.inbound_sideload as sideload_mod

    async def _failed_scan(*args: Any, **kwargs: Any) -> set[int]:
        raise RuntimeError("history unavailable")

    monkeypatch.setattr(sideload_mod, "committed_ids_for_reconcile", _failed_scan)
    with (
        bind_turn_identity(agent, incarnation=incarnation),
        pytest.raises(RuntimeError, match="history unavailable"),
    ):
        await reconcile_claimed_inbounds_at_startup(aops_pool, saver, agent)

    assert _statuses(db_conn, [claimed]) == {claimed: "claimed"}
