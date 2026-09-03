"""Real process admission -> durable queue -> graph dispatch, no OS effect."""

import os
from pathlib import Path
from uuid import uuid4

import psutil
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
from ops.resurrection_retry import authorize_pending_retry, validate_pending_retry
from shared.agents import MachinePaused, ResurrectError
from shared.context import AvaContext
from shared.db import insert_inbound_message
from shared.db_transaction import async_write_transaction
from shared.inbound import InboundKind
from shared.lifecycle_process_identity import capture_process_identity, target_process_ended
from shared.lifecycle_termination_observe import observe_applied_termination
from shared.machine import machine_name
from shared.runtime_incarnation import current_incarnation
from shared.turn_identity import bind_turn_identity
from tests.agent.test_lifecycle_intent import _command
from tests.agent.test_runtime_incarnation import _row


def test_payload_cannot_mint_accepted_dispatch_receipt() -> None:
    item = ClaimedInbound.from_row(
        (1, 2, "", "terminate", "user", {"durable_lifecycle": True}, None, None)
    )
    assert not item.durable_lifecycle


async def test_pending_resurrection_budget_survives_redispatch_without_ack(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id = _row(db_conn)
    claim_agent_row(agent_id)
    command_id = _command(db_conn, agent_id, "terminate")
    await claim_inbound_batch(aops_pool, agent_id)
    async with async_write_transaction(aops_pool) as conn:
        assert await apply_process_lifecycle(conn, agent_id, command_id)
    with pytest.raises(ValueError, match="reserved"):
        insert_inbound_message(
            db_conn, agent_id, "wake", "user", payload={"resurrection_retry": {}}
        )
    wake = insert_inbound_message(db_conn, agent_id, "wake", "user")
    db_conn.commit()
    authorize_pending_retry(agent_id, wake, "chat", 2)
    authorize_pending_retry(agent_id, wake, "chat", 2)
    with pytest.raises(ResurrectError, match="budget exhausted"):
        authorize_pending_retry(agent_id, wake, "chat", 2)
    before = db_conn.execute(
        "SELECT payload,status,created_at FROM inbound_messages WHERE id=%s", (wake,)
    ).fetchone()
    assert before is not None and before[:2] == (
        {"resurrection_retry": {"blocked_by": command_id, "attempts": 2}},
        "pending",
    )
    db_conn.execute("UPDATE agents_meta SET runtime_owner=%s WHERE id=%s", (uuid4(), agent_id))
    with pytest.raises(ResurrectError, match="changed target"):
        validate_pending_retry(db_conn, agent_id, wake)
    assert (
        db_conn.execute(
            "SELECT payload,status,created_at FROM inbound_messages WHERE id=%s", (wake,)
        ).fetchone()
        == before
    )
    assert db_conn.execute(
        "SELECT observed_at FROM inbound_messages WHERE id=%s", (command_id,)
    ).fetchone() == (None,)
    db_conn.commit()


async def test_paused_owned_target_does_not_spend_pending_retry_budget(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = _row(db_conn)
    claim_agent_row(agent_id)
    command_id = _command(db_conn, agent_id, "terminate")
    await claim_inbound_batch(aops_pool, agent_id)
    async with async_write_transaction(aops_pool) as conn:
        assert await apply_process_lifecycle(conn, agent_id, command_id)
    wake = insert_inbound_message(db_conn, agent_id, "wake", "user")
    db_conn.execute(
        "INSERT INTO machines(name,role,paused_at) VALUES(%s,ARRAY['agent-runner'],clock_timestamp()) "
        "ON CONFLICT(name) DO UPDATE SET paused_at=EXCLUDED.paused_at",
        (machine_name(),),
    )
    db_conn.commit()
    before = db_conn.execute(
        "SELECT status,payload,created_at FROM inbound_messages WHERE id=%s", (wake,)
    ).fetchone()
    with pytest.raises(MachinePaused):
        authorize_pending_retry(agent_id, wake, "chat", 2)
    assert (
        db_conn.execute(
            "SELECT status,payload,created_at FROM inbound_messages WHERE id=%s", (wake,)
        ).fetchone()
        == before
    )
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (command_id,)
    db_conn.commit()


async def test_placement_change_after_pause_latch_cannot_spend_budget(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ops import resurrection_retry

    agent_id = _row(db_conn)
    claim_agent_row(agent_id)
    command = _command(db_conn, agent_id, "terminate")
    await claim_inbound_batch(aops_pool, agent_id)
    async with async_write_transaction(aops_pool) as conn:
        assert await apply_process_lifecycle(conn, agent_id, command)
    wake = insert_inbound_message(db_conn, agent_id, "wake", "user")
    db_conn.execute(
        "INSERT INTO machines(name,role,paused_at) VALUES(%s,ARRAY['agent-runner'],clock_timestamp()) "
        "ON CONFLICT(name) DO UPDATE SET paused_at=EXCLUDED.paused_at",
        (machine_name(),),
    )
    db_conn.execute(
        "INSERT INTO machines(name,role) VALUES('pre-latch-home',ARRAY['agent-runner'])"
    )
    db_conn.execute("UPDATE agents_meta SET machine='pre-latch-home' WHERE id=%s", (agent_id,))
    db_conn.commit()
    original = resurrection_retry.lock_active_home_machine

    def move_after_latch(cursor: psycopg.Cursor, target: int) -> str:
        home = original(cursor, target)
        # Independent connection changes placement while only the former home
        # is latched. The subsequent metadata lock must reject that mismatch.
        db_conn.execute("UPDATE agents_meta SET machine=%s WHERE id=%s", (machine_name(), target))
        db_conn.commit()
        return home

    monkeypatch.setattr(resurrection_retry, "lock_active_home_machine", move_after_latch)
    with pytest.raises(resurrection_retry.ResurrectTriggerStaleError, match="placement changed"):
        authorize_pending_retry(agent_id, wake, "chat", 2)
    assert db_conn.execute(
        "SELECT status,payload ? 'resurrection_retry' FROM inbound_messages WHERE id=%s", (wake,)
    ).fetchone() == ("pending", None)
    db_conn.commit()


async def test_process_identity_is_reserved_fixed_and_not_an_exit_receipt(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = _row(db_conn)
    claim_agent_row(agent_id)
    with pytest.raises(ValueError, match="reserved"):
        insert_inbound_message(
            db_conn,
            agent_id,
            "",
            source="user",
            kind="terminate",
            payload={"target_process_identity": {"pid": 1}},
        )
    command_id = _command(db_conn, agent_id, "terminate")
    await claim_inbound_batch(aops_pool, agent_id)
    async with async_write_transaction(aops_pool) as conn:
        assert await apply_process_lifecycle(conn, agent_id, command_id)
    first = db_conn.execute(
        "SELECT payload,applied_at FROM inbound_messages WHERE id=%s", (command_id,)
    ).fetchone()
    assert first is not None
    assert first[0]["target_process_identity"]["pid"] == os.getpid()
    async with async_write_transaction(aops_pool) as conn:
        assert await apply_process_lifecycle(conn, agent_id, command_id)
    assert (
        db_conn.execute(
            "SELECT payload,applied_at FROM inbound_messages WHERE id=%s", (command_id,)
        ).fetchone()
        == first
    )
    # Even after a DB release, this test's actual admitted Python is still live.
    db_conn.execute(
        "UPDATE agents_meta SET pid=NULL,lease_expires_at=NULL WHERE id=%s", (agent_id,)
    )
    assert not observe_applied_termination(db_conn, agent_id, machine_name())
    assert db_conn.execute(
        "SELECT status,observed_at FROM inbound_messages WHERE id=%s", (command_id,)
    ).fetchone() == ("claimed", None)
    db_conn.commit()


@pytest.mark.parametrize("observation", ["gone", "reused", "live", "unknown", "missing"])
def test_exact_process_observation_does_not_need_session_record(
    monkeypatch: pytest.MonkeyPatch, observation: str
) -> None:
    identity = capture_process_identity(os.getpid(), machine_name())
    payload = {"target_process_identity": identity}
    if observation == "missing":
        assert not target_process_ended({}, machine_name())
        return
    if observation == "reused":
        # Same numeric PID, different birth, with no canonical record involved.
        identity["starttime"] = None
        identity["create_time"] = 1.0
    elif observation in {"gone", "unknown"}:

        def unavailable(pid: int) -> psutil.Process:
            if observation == "gone":
                raise psutil.NoSuchProcess(pid)
            raise psutil.AccessDenied(pid)

        monkeypatch.setattr("shared.lifecycle_process_identity.psutil.Process", unavailable)
    assert target_process_ended(payload, machine_name()) is (observation in {"gone", "reused"})
    assert not target_process_ended(payload, "different-machine")


@pytest.mark.parametrize("changed", ["owner", "pointer", "machine"])
async def test_termination_observation_rejects_changed_locked_target(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    agent_id = _row(db_conn)
    claim_agent_row(agent_id)
    command_id = _command(db_conn, agent_id, "terminate")
    await claim_inbound_batch(aops_pool, agent_id)
    async with async_write_transaction(aops_pool) as conn:
        assert await apply_process_lifecycle(conn, agent_id, command_id)
    if changed == "owner":
        db_conn.execute("UPDATE agents_meta SET runtime_owner=%s WHERE id=%s", (uuid4(), agent_id))
    elif changed == "pointer":
        other = _command(db_conn, agent_id, "terminate")
        db_conn.execute(
            "UPDATE agents_meta SET lifecycle_command_id=%s WHERE id=%s", (other, agent_id)
        )
    else:

        def ended(_payload: object, _machine: str) -> bool:
            return True

        monkeypatch.setattr("shared.lifecycle_termination_observe.target_process_ended", ended)
    before = db_conn.execute(
        "SELECT runtime_owner,lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone()
    assert not observe_applied_termination(
        db_conn, agent_id, "another-machine" if changed == "machine" else machine_name()
    )
    assert (
        db_conn.execute(
            "SELECT runtime_owner,lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
        ).fetchone()
        == before
    )
    assert db_conn.execute(
        "SELECT status,observed_at FROM inbound_messages WHERE id=%s", (command_id,)
    ).fetchone() == ("claimed", None)
    db_conn.commit()


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
