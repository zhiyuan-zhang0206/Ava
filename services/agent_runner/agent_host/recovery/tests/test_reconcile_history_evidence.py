"""Historical checkpoint proof survives clock skew and incomplete scans."""

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.graph.tests.cursor_fixture import _fresh_snapshot_cursor as _fresh_snapshot_cursor
from agent.startup import reconcile_claimed_inbounds_at_startup
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from services.agent_runner.agent_host.recovery.tests.test_hosted_db_recovery import admit_recovery
from services.agent_runner.agent_host.recovery.tests.test_reconcile_after_abort import (
    _build_graph,
    _CountingSaver,
    _seed_delta_written_checkpoint,
    claimed_row,
    row_statuses,
)
from services.agent_runner.agent_host.tests.host_policy import configured_policy


async def test_checkpoint_clock_skew_scans_settled_history(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
) -> None:
    """A checkpoint clock over 300 seconds behind DB time cannot bound writes."""
    incarnation = await admit_recovery(
        aops_pool,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
    )
    agent = incarnation.agent_id
    committed = claimed_row(db_conn, agent, "skewed")
    saver = await _seed_delta_written_checkpoint(aops_pool, agent, committed)
    db_conn.execute(
        "UPDATE checkpoints SET checkpoint = jsonb_set(checkpoint, '{ts}', to_jsonb(%s::text)) "
        "WHERE thread_id = %s",
        ((datetime.now(UTC) - timedelta(minutes=10)).isoformat(), str(agent)),
    )
    db_conn.commit()

    await reconcile_claimed_inbounds_at_startup(
        aops_pool,
        saver,
        agent,
        incarnation=incarnation,
        inputs=configured_policy().reconcile_inputs,
    )

    assert saver.aget_calls == 0
    assert row_statuses(db_conn, [committed]) == {committed: "done"}


async def test_historical_clock_skew_cannot_hide_a_fresh_commit(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
) -> None:
    """A later clock-correct checkpoint must not make an old skewed write disappear."""
    incarnation = await admit_recovery(
        aops_pool,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
    )
    agent = incarnation.agent_id
    committed = claimed_row(db_conn, agent, "historically skewed")
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

    await reconcile_claimed_inbounds_at_startup(
        aops_pool,
        saver,
        agent,
        incarnation=incarnation,
        inputs=configured_policy().reconcile_inputs,
    )

    assert saver.aget_calls == 0
    assert row_statuses(db_conn, [committed]) == {committed: "done"}


async def test_incomplete_full_write_scan_preserves_claimed_row(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: Any,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
) -> None:
    """A failed history scan cannot turn missing proof into a reset."""
    incarnation = await admit_recovery(
        aops_pool,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
    )
    agent = incarnation.agent_id
    claimed = claimed_row(db_conn, agent, "unresolved")
    saver = _CountingSaver(aops_pool)

    import base.agents.history.inbound_sideload as sideload_mod

    async def _failed_scan(*args: Any, **kwargs: Any) -> set[int]:
        raise RuntimeError("history unavailable")

    monkeypatch.setattr(sideload_mod, "committed_ids_for_reconcile", _failed_scan)
    with pytest.raises(RuntimeError, match="history unavailable"):
        await reconcile_claimed_inbounds_at_startup(
            aops_pool,
            saver,
            agent,
            incarnation=incarnation,
            inputs=configured_policy().reconcile_inputs,
        )

    assert row_statuses(db_conn, [claimed]) == {claimed: "claimed"}
