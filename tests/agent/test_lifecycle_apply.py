"""Real process admission -> durable queue -> graph dispatch, no OS effect."""

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent._starting import claim_agent_row
from agent.db import ClaimedInbound, claim_inbound_batch
from agent.graph._claim_dispatch import _BatchState, _handle_restart, _handle_terminate
from agent.graph._claim_routing import resolve_routing
from agent.graph._nodes import END
from agent.inbound_ownership import RuntimeOwnershipLostError
from agent.lifecycle_apply import apply_process_lifecycle
from shared.context import AvaContext
from shared.db import insert_inbound_message
from shared.db_transaction import async_write_transaction
from shared.inbound import InboundKind
from shared.runtime_incarnation import current_incarnation
from shared.turn_identity import bind_turn_identity
from tests.agent.test_lifecycle_intent import _command
from tests.agent.test_runtime_incarnation import _row


def test_payload_cannot_mint_accepted_dispatch_receipt() -> None:
    item = ClaimedInbound.from_row(
        (1, 2, "", "terminate", "user", {"durable_lifecycle": True}, None, None)
    )
    assert not item.durable_lifecycle


@pytest.mark.parametrize("replacement", ["none", "owner", "pointer"])
async def test_accepted_termination_ignores_pending_veto_but_retains_apply_fence(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, replacement: str
) -> None:
    agent_id = _row(db_conn)
    claim_agent_row(agent_id)
    first = _command(db_conn, agent_id, "terminate")
    batch = await claim_inbound_batch(aops_pool, agent_id)
    second = _command(db_conn, agent_id, "restart")
    insert_inbound_message(db_conn, agent_id, "later chat", source="user")
    db_conn.commit()
    assert batch[0].durable_lifecycle
    if replacement == "owner":
        db_conn.execute("UPDATE agents_meta SET runtime_owner=%s WHERE id=%s", (uuid4(), agent_id))
    elif replacement == "pointer":
        db_conn.execute("UPDATE agents_meta SET lifecycle_command_id=NULL WHERE id=%s", (agent_id,))
    db_conn.commit()
    ctx = AvaContext(ops_pool=aops_pool)
    routing = await resolve_routing(ctx, agent_id, batch)
    assert routing.exit_kind == InboundKind.TERMINATE
    assert not routing.terminate_vetoed_by_pending
    state = _BatchState()
    await _handle_terminate(ctx, batch[0], state)
    assert state.next_goto == END
    command = db_conn.execute(
        "SELECT status,applied_at IS NOT NULL,observed_at FROM inbound_messages WHERE id=%s",
        (first,),
    ).fetchone()
    assert command == ("claimed", replacement == "none", None)
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (("terminated",) if replacement == "none" else ("running",))
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (second,)
    ).fetchone() == ("pending",)
    assert db_conn.execute(
        "SELECT status,content FROM inbound_messages WHERE agent_id=%s AND kind='chat'", (agent_id,)
    ).fetchone() == ("pending", "later chat")
    if replacement != "none":
        assert state.new_msgs == []


@pytest.mark.parametrize("kind", ["restart", "terminate"])
async def test_actual_process_dispatch_installs_decision_not_observation(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, kind: str
) -> None:
    agent_id = _row(db_conn)
    claim_agent_row(agent_id)
    first = _command(db_conn, agent_id, kind)
    second = _command(db_conn, agent_id, "restart")
    batch = await claim_inbound_batch(aops_pool, agent_id)
    assert [item.id for item in batch] == [first]
    assert db_conn.execute(
        "SELECT status,applied_at FROM inbound_messages WHERE id=%s", (first,)
    ).fetchone() == ("claimed", None)
    state = _BatchState()
    ctx = AvaContext(ops_pool=aops_pool)
    if kind == "restart":
        await _handle_restart(ctx, agent_id, batch[0], state)
    else:
        await _handle_terminate(ctx, batch[0], state)
    assert state.next_goto == END
    expected_status = "restarting" if kind == "restart" else "terminated"
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (expected_status,)
    applied = db_conn.execute(
        "SELECT applied_at,observed_at,status FROM inbound_messages WHERE id=%s", (first,)
    ).fetchone()
    assert applied is not None and applied[0] is not None and applied[1:] == (None, "claimed")
    async with async_write_transaction(aops_pool) as conn:
        assert await apply_process_lifecycle(conn, agent_id, first)
    assert (
        db_conn.execute(
            "SELECT applied_at,observed_at,status FROM inbound_messages WHERE id=%s", (first,)
        ).fetchone()
        == applied
    )
    with pytest.raises(RuntimeOwnershipLostError):
        await claim_inbound_batch(aops_pool, agent_id)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (second,)
    ).fetchone() == ("pending",)


@pytest.mark.parametrize(
    "following_kind", ["restart", "chat", "cancel", "compact_summary", "restart_completed"]
)
async def test_successor_admission_observes_restart_before_next_claim(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    following_kind: str,
) -> None:
    monkeypatch.setattr("agent.session_admission.run_dir", lambda: tmp_path)
    agent_id = _row(db_conn)
    claim_agent_row(agent_id)
    old = current_incarnation(agent_id)
    assert old is not None
    first = _command(db_conn, agent_id, "restart")
    second = _command(db_conn, agent_id, following_kind)
    await claim_inbound_batch(aops_pool, agent_id)
    async with async_write_transaction(aops_pool) as conn:
        assert await apply_process_lifecycle(conn, agent_id, first)
    # The launcher owns this transition after exit/session confirmation.
    # This database test proves admission, not an actual OS process exit.
    db_conn.execute("UPDATE agents_meta SET status='idling',pid=NULL WHERE id=%s", (agent_id,))
    db_conn.execute(
        "UPDATE inbound_messages SET payload=jsonb_build_object('launch_attempts',1) WHERE id=%s",
        (first,),
    )
    db_conn.commit()
    claim_agent_row(agent_id, restart_command_id=first)
    successor = current_incarnation(agent_id)
    assert successor is not None and successor != old
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (None,)
    observed = db_conn.execute(
        "SELECT status,applied_at,observed_at FROM inbound_messages WHERE id=%s", (first,)
    ).fetchone()
    assert observed is not None and observed[0] == "done"
    assert observed[1] is not None and observed[2] is not None
    successor_batch = await claim_inbound_batch(aops_pool, agent_id)
    if following_kind == "restart":
        assert [item.id for item in successor_batch] == [second]
    else:
        # Actual admission emits its own completion marker alongside retained work.
        assert len(successor_batch) == 2
        assert (successor_batch[0].id, successor_batch[0].kind) == (second, following_kind)
        assert successor_batch[1].kind == "restart_completed"
        assert successor_batch[1].id > second
        assert db_conn.execute(
            "SELECT status FROM inbound_messages WHERE id=%s", (second,)
        ).fetchone() == (("claimed",) if following_kind == "chat" else ("done",))
    with bind_turn_identity(agent_id, incarnation=old):
        async with async_write_transaction(aops_pool) as conn:
            assert not await apply_process_lifecycle(conn, agent_id, first)


async def test_process_apply_rollback_and_successor_refusal(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = _row(db_conn)
    claim_agent_row(agent_id)
    inbound = _command(db_conn, agent_id, "restart")
    await claim_inbound_batch(aops_pool, agent_id)
    with pytest.raises(RuntimeError, match="crash"):
        async with async_write_transaction(aops_pool) as conn:
            assert await apply_process_lifecycle(conn, agent_id, inbound)
            raise RuntimeError("crash before effect commit")
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("running",)
    assert db_conn.execute(
        "SELECT applied_at FROM inbound_messages WHERE id=%s", (inbound,)
    ).fetchone() == (None,)
    db_conn.execute("UPDATE agents_meta SET runtime_generation=%s WHERE id=%s", (uuid4(), agent_id))
    db_conn.commit()
    async with async_write_transaction(aops_pool) as conn:
        assert not await apply_process_lifecycle(conn, agent_id, inbound)
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("running",)
