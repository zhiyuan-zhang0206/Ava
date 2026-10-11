"""Only a latest committed halt marker can complete the original intent."""

import asyncio
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.impersonation import flush_checkpoint
from agent.state import checkpoint_msgpack_allowlist
from base.agents.incarnation.native_work_models import (
    NativeCancelMarker,
    NativeCancelOutcome,
    NativeWorkUncertainError,
)
from base.agents.messages.native_cancel import accept_native_cancel, finish_native_cancel
from base.db.code_version_gate import ProcessDbGate
from base.db.transaction import async_write_transaction
from services.agent_runner.agent_host.invocation.native_work import (
    cold_cancel_checkpoint,
    recover_native_cancel,
    settle_native_invocation,
)
from services.agent_runner.agent_host.tests.history.test_hosted_compact_failure import (
    _cold_reader,
    _prepare_graph,
)
from services.agent_runner.agent_host.tests.native_cancel.helpers import managed_work


async def test_checkpoint_flush_ack_and_retained_terminal_receipt(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, *, database_gate: ProcessDbGate
) -> None:
    pool: ConnectionPool
    incarnation, target = await managed_work(db_conn, aops_pool, database_gate=database_gate)
    graph, saver, config, history = await _prepare_graph(aops_pool, target.agent_id, 100, [])
    saver.serde = JsonPlusSerializer(allowed_msgpack_modules=checkpoint_msgpack_allowlist())
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
        accepted = await asyncio.to_thread(
            accept_native_cancel, pool, "checkpoint-original", target.agent_id, target
        )
    marker = NativeCancelMarker(command_id=accepted.command_id, target=target)
    assert await settle_native_invocation(
        aops_pool, saver, graph, incarnation, target, config, resources=None
    )
    cold = _cold_reader(aops_pool)
    cold.serde = JsonPlusSerializer(allowed_msgpack_modules=checkpoint_msgpack_allowlist())
    checkpoint_id = await cold_cancel_checkpoint(cold, marker)
    assert checkpoint_id is not None
    snapshot = await graph.aget_state(config)
    assert [message.id for message in snapshot.values["messages"]] == [
        message.id for message in history
    ]
    assert snapshot.values["halted"] is True
    assert NativeCancelMarker.model_validate(snapshot.values["native_cancel"]) == marker
    assert db_conn.execute(
        "SELECT outcome,checkpoint_id FROM native_cancel_commands WHERE id=%s", (marker.command_id,)
    ).fetchone() == ("applied", checkpoint_id)
    db_conn.execute("DELETE FROM native_graph_work WHERE id=%s", (target.work_id,))
    db_conn.execute(
        "UPDATE agents_meta SET runtime_owner=%s,native_work_id=NULL WHERE id=%s",
        (uuid4(), target.agent_id),
    )
    db_conn.commit()
    async with async_write_transaction(aops_pool) as conn:
        await finish_native_cancel(
            conn,
            incarnation,
            marker,
            outcome=NativeCancelOutcome.APPLIED,
            checkpoint_id=checkpoint_id,
        )


async def test_superseded_checkpoint_cannot_ack_old_marker(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, *, database_gate: ProcessDbGate
) -> None:
    pool: ConnectionPool
    incarnation, target = await managed_work(db_conn, aops_pool, database_gate=database_gate)
    graph, saver, config, _history = await _prepare_graph(aops_pool, target.agent_id, 1, [])
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
        accepted = await asyncio.to_thread(
            accept_native_cancel, pool, "superseded", target.agent_id, target
        )
    marker = NativeCancelMarker(command_id=accepted.command_id, target=target)
    await graph.aupdate_state(
        config, {"native_work": target, "native_cancel": marker, "halted": True}, as_node="claim"
    )
    await flush_checkpoint(saver, target.agent_id)
    checkpoint_id = await cold_cancel_checkpoint(saver, marker)
    assert checkpoint_id is not None
    await graph.aupdate_state(config, {"native_cancel": None, "halted": False}, as_node="claim")
    await flush_checkpoint(saver, target.agent_id)
    assert await cold_cancel_checkpoint(saver, marker) is None
    async with async_write_transaction(aops_pool) as conn:
        with pytest.raises(NativeWorkUncertainError, match="superseded"):
            await finish_native_cancel(
                conn,
                incarnation,
                marker,
                outcome=NativeCancelOutcome.APPLIED,
                checkpoint_id=checkpoint_id,
            )
    assert db_conn.execute(
        "SELECT outcome FROM native_cancel_commands WHERE id=%s", (marker.command_id,)
    ).fetchone() == ("accepted",)


async def test_cold_original_without_marker_or_transfer_is_uncertain(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, *, database_gate: ProcessDbGate
) -> None:
    pool: ConnectionPool
    incarnation, target = await managed_work(db_conn, aops_pool, database_gate=database_gate)
    graph, saver, config, _history = await _prepare_graph(aops_pool, target.agent_id, 1, [])
    original = await graph.aget_state(config)
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
        await asyncio.to_thread(
            accept_native_cancel, pool, "no-stop-proof", target.agent_id, target
        )
    assert not await recover_native_cancel(aops_pool, saver, graph, incarnation, resources=None)
    unchanged = await graph.aget_state(config)
    assert unchanged.config == original.config
    assert unchanged.values["halted"] is False
    assert db_conn.execute(
        "SELECT outcome FROM native_cancel_commands WHERE work_id=%s", (target.work_id,)
    ).fetchone() == ("uncertain",)
    assert db_conn.execute(
        "SELECT phase FROM native_graph_work WHERE id=%s", (target.work_id,)
    ).fetchone() == ("uncertain",)


async def test_no_protected_command_never_adds_cold_startup_hold(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, *, database_gate: ProcessDbGate
) -> None:
    incarnation, target = await managed_work(db_conn, aops_pool, database_gate=database_gate)
    graph, saver, _config, _history = await _prepare_graph(aops_pool, target.agent_id, 1, [])
    assert await recover_native_cancel(aops_pool, saver, graph, incarnation, resources=None)


async def test_empty_database_set_does_not_override_live_continuation_resource(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    tmp_path: Path,
    *,
    database_gate: ProcessDbGate,
) -> None:
    pool: ConnectionPool
    from base.native_process.turn_identity import HostedTurnResources

    incarnation, target = await managed_work(db_conn, aops_pool, database_gate=database_gate)
    graph, saver, config, _history = await _prepare_graph(aops_pool, target.agent_id, 1, [])
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
        accepted = await asyncio.to_thread(
            accept_native_cancel, pool, "live-resource", target.agent_id, target
        )
    scope = HostedTurnResources(unresolved={tmp_path / "live-domain": object()})
    original = await graph.aget_state(config)
    with pytest.raises(NativeWorkUncertainError, match="unresolved"):
        await settle_native_invocation(
            aops_pool, saver, graph, incarnation, target, config, resources=scope
        )
    assert (await graph.aget_state(config)).config == original.config
    assert db_conn.execute(
        "SELECT outcome FROM native_cancel_commands WHERE id=%s", (accepted.command_id,)
    ).fetchone() == ("accepted",)


@pytest.mark.parametrize("missing", [True, False])
async def test_lost_or_misaligned_pointer_holds_pending_original_command(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    missing: bool,
    *,
    database_gate: ProcessDbGate,
) -> None:
    pool: ConnectionPool
    from services.agent_runner.agent_host.invocation.native_work import (
        NativeWorkContinuation,
        prepare_native_invocation,
    )

    incarnation, target = await managed_work(db_conn, aops_pool, database_gate=database_gate)
    graph, saver, config, _history = await _prepare_graph(aops_pool, target.agent_id, 1, [])
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
        await asyncio.to_thread(accept_native_cancel, pool, "pointer-gap", target.agent_id, target)
    db_conn.execute(
        "UPDATE agents_meta SET native_work_id=%s WHERE id=%s",
        (None if missing else uuid4(), target.agent_id),
    )
    db_conn.commit()
    original = await graph.aget_state(config)
    assert not await recover_native_cancel(aops_pool, saver, graph, incarnation, resources=None)
    assert (await graph.aget_state(config)).config == original.config
    with pytest.raises(NativeWorkUncertainError, match="must settle"):
        await prepare_native_invocation(aops_pool, NativeWorkContinuation(uuid4()), incarnation)
    assert db_conn.execute(
        "SELECT count(*) FROM native_graph_work WHERE agent_id=%s", (target.agent_id,)
    ).fetchone() == (1,)
    assert db_conn.execute(
        "SELECT outcome FROM native_cancel_commands WHERE work_id=%s", (target.work_id,)
    ).fetchone() == ("uncertain",)
