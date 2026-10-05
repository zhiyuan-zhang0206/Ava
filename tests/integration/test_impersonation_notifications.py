"""Lease renewal reminders and ordered termination interruption notices."""

import sys
from contextlib import contextmanager
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import psycopg
import pytest
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.graph.claim.node import claim_node
from agent.ownership.hosted import admit_hosted_runtime, apply_hosted_lifecycle
from agent.state import BaseAgentState
from base.agents import impersonation as leases
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity
from base.agents.impersonation.maintenance import remind_expiring_impersonations
from base.agents.incarnation.hosted_force import original_host_force
from base.agents.messages.caller_identity import CallerIdentity
from base.cluster.machine import machine_name
from base.db import Database, create_agent, pool
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import bind_turn_identity
from cli.commands.agents import impersonation_relay as relay
from ops.agents.wake import resurrect_agent
from ops.lifecycle.termination import (
    _enqueue_termination_inbounds,
    _force_terminate_transaction,
)
from tests.impersonation_support import recorded_tree


def test_reminder_commands_are_bare_ava(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    """The renewal reminder names no interpreter or home: a bare `ava` resolves
    through the executor's inherited AVA_HOME."""
    agent_id = create_agent(db_conn)
    owner = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '10 minutes')",
        (agent_id, machine_name(), owner.generation, owner.owner),
    )
    db_conn.commit()
    lease = leases.request(
        database,
        event_bus,
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
        ttl_seconds=300,
        reason="Handle the next message",
        process_metadata={**recorded_tree(), "invoked_python": "/preview source/.venv/bin/python"},
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
    )
    leases.accept(database, event_bus, lease["id"], agent_id, owner, "Handoff brief")
    leases.activate(database, event_bus, lease["id"], owner)
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()+interval '4 minutes' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    with pool(max_size=2) as reaper_pool:
        assert remind_expiring_impersonations(reaper_pool, database, event_bus) == 1
    reminder = db_conn.execute(
        "SELECT content FROM inbound_messages WHERE agent_id=%s AND kind='reminder'",
        (agent_id,),
    ).fetchone()
    assert reminder is not None
    content: str = reminder[0]
    # The renew suggestion repeats the lease's own window, not a fixed hour.
    assert f"\nava impersonate renew 0 --agent {agent_id} --ttl 300\n" in content
    assert f"\nava impersonate release 0 --agent {agent_id} --summary ..." in content
    assert "-m cli" not in content
    assert ".venv" not in content
    assert sys.executable not in content


@pytest.mark.parametrize("bad", [7, "", "  ", ["python"]])
def test_request_rejects_a_malformed_invoked_python(
    db_conn: psycopg.Connection, bad: object, database: Database, event_bus: EventBus
) -> None:
    agent_id = create_agent(db_conn)
    owner = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '10 minutes')",
        (agent_id, machine_name(), owner.generation, owner.owner),
    )
    db_conn.commit()
    with pytest.raises(ValueError, match="invoked_python"):
        leases.request(
            database,
            event_bus,
            agent_id,
            caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
            reason="Handle the next message",
            process_metadata={**recorded_tree(), "invoked_python": bad},
            relay_provider="codex",
            relay_thread_id=str(uuid4()),
        )


async def _termination_session(
    conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    status: str = "active",
    automatic: bool = False,
) -> tuple[RuntimeIncarnation, dict[str, Any]]:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling',%s)",
        (agent_id, machine_name()),
    )
    conn.commit()
    owner = await admit_hosted_runtime(
        aops_pool,
        agent_id,
        machine_name(),
        uuid4(),
        expected_from="idling",
        db=Database.from_settings(),
    )
    assert owner is not None
    session = leases.request(
        Database.from_settings(),
        EventBus.from_settings(),
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        ttl_seconds=300,
        reason="Continue the work",
        name="Interruption test",
        executor_name="test executor",
        process_metadata=recorded_tree(),
        relay_provider="claude",
        automatic=automatic,
    )
    if status in ("accepted", "active"):
        leases.accept(
            Database.from_settings(),
            EventBus.from_settings(),
            session["id"],
            agent_id,
            owner,
            "Handoff brief",
        )
    if status == "active":
        leases.activate(Database.from_settings(), EventBus.from_settings(), session["id"], owner)
    return owner, session


def _native_notices(conn: psycopg.Connection, agent_id: int) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT id,content,payload->>'note_tag',status,created_at FROM inbound_messages "
        "WHERE agent_id=%s AND source='system:impersonation' ORDER BY created_at,id",
        (agent_id,),
    ).fetchall()


async def _terminate_native(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    owner: RuntimeIncarnation,
    session: dict[str, Any],
    runtime: Runtime[AvaContext],
    mode: str,
) -> None:
    config: RunnableConfig = {"configurable": {"thread_id": str(owner.agent_id)}}
    state = BaseAgentState(impersonation_request_id=f"{session['id']}:1")
    with pool(max_size=2) as ops_pool, bind_turn_identity(owner.agent_id, incarnation=owner):
        if mode == "force":
            _force_terminate_transaction(owner.agent_id, ops_pool, source="user")
            assert await original_host_force(
                aops_pool, owner.agent_id, owner.owner, machine_name(), quiescent=True
            )
        else:
            terminate_id = _enqueue_termination_inbounds(
                Database.from_settings(),
                EventBus.from_settings(),
                owner.agent_id,
                ops_pool,
                source="user",
                message=None,
            )
            if mode == "live":
                accepted = await claim_node(state, runtime, config)
                assert accepted.goto == "__end__"
                assert "Termination was accepted" in str(accepted.update)
            else:
                batch = await claim_inbound_batch(aops_pool, owner.agent_id, lifecycle_only=True)
                assert [item.id for item in batch] == [terminate_id]
            assert _native_notices(db_conn, owner.agent_id) == []
            assert (
                await apply_hosted_lifecycle(aops_pool, owner, bus=EventBus.from_settings())
                == "terminate"
            )
    notices = _native_notices(db_conn, owner.agent_id)
    assert [row[2] for row in notices] == ["impersonation", "lifecycle_terminate"]
    assert all(row[3] == "pending" for row in notices)
    assert "was interrupted because this agent was terminated" in notices[0][1]
    assert notices[1][1] == "You were terminated."
    # Notice-only arrivals cannot auto-resurrect a terminated owner.
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (owner.agent_id,)
    ).fetchone() == ("terminated",)


@pytest.mark.parametrize("mode", ["live", "drained", "force", "delayed_resurrection"])
async def test_termination_notices_precede_resurrection_in_native_claim(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    mode: str,
    database: Database,
    event_bus: EventBus,
) -> None:
    owner, session = await _termination_session(
        db_conn, aops_pool, status="requested" if mode == "live" else "active"
    )
    runtime = Runtime(
        context=AvaContext(
            ops_pool=aops_pool,
            event_publisher=MagicMock(),
            agent=AgentSlices.resolve(),
            db=Database.from_settings(),
            bus=EventBus.from_settings(),
            identity=AgentIdentity(agent_id=owner.agent_id, owns_loop=True),
        )
    )
    await _terminate_native(db_conn, aops_pool, owner, session, runtime, mode)
    if mode == "delayed_resurrection":
        _age_and_sweep_notices(db_conn, owner.agent_id)
    resurrect_agent(database, event_bus, owner.agent_id, resurrected_by="user")
    successor = await admit_hosted_runtime(
        aops_pool, owner.agent_id, machine_name(), uuid4(), expected_from="idling", db=database
    )
    assert successor is not None
    with bind_turn_identity(owner.agent_id, incarnation=successor):
        resumed = await claim_node(
            BaseAgentState(), runtime, {"configurable": {"thread_id": str(owner.agent_id)}}
        )
    update = cast(dict[str, Any], resumed.update)
    assert isinstance(update, dict)
    messages = cast(list[BaseMessage], update["messages"])
    assert [message.additional_kwargs["ava_note_tag"] for message in messages] == [
        "impersonation",
        "lifecycle_terminate",
        "lifecycle_resurrect",
    ]
    assert "was interrupted" in messages[0].content
    assert "You were terminated" in messages[1].content
    assert "You have been resurrected" in messages[2].content


def _age_and_sweep_notices(conn: psycopg.Connection, agent_id: int) -> None:
    from services.wake.delivery_watchdog.dead_letter import dead_letter_stale_pending_terminated

    conn.execute(
        "UPDATE inbound_messages SET created_at=created_at-interval '2 days' "
        "WHERE agent_id=%s AND kind='system_note'",
        (agent_id,),
    )
    conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source,created_at) "
        "VALUES(%s,'ordinary stale notice','system_note','system',now()-interval '2 days')",
        (agent_id,),
    )
    conn.commit()
    with pool(max_size=1) as watchdog_pool:
        assert dead_letter_stale_pending_terminated(watchdog_pool, 86400) == 1
    assert all(row[3] == "pending" for row in _native_notices(conn, agent_id))


@pytest.mark.parametrize("status", ["requested", "accepted", "active"])
async def test_termination_closes_automatic_preparation_and_dismisses_reminders(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, status: str
) -> None:
    owner, session = await _termination_session(db_conn, aops_pool, status=status, automatic=True)
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source,payload) "
        "VALUES(%s,'renew','reminder','system',jsonb_build_object('lease_id',%s::text))",
        (owner.agent_id, session["id"]),
    )
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    notices = _native_notices(db_conn, owner.agent_id)
    assert len(notices) == 2
    assert db_conn.execute(
        "SELECT status,rejection_reason,summary_inbound_id FROM agent_impersonations WHERE id=%s",
        (session["id"],),
    ).fetchone() == ("expired", "terminated: agent was terminated", notices[0][0])
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE agent_id=%s AND kind='reminder'",
        (owner.agent_id,),
    ).fetchone() == ("done",)


async def test_termination_notice_rollback_and_repeated_status_writes(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    owner, session = await _termination_session(db_conn, aops_pool)
    with db_conn.transaction(force_rollback=True):
        db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (owner.agent_id,))
        assert len(_native_notices(db_conn, owner.agent_id)) == 2
    assert _native_notices(db_conn, owner.agent_id) == []
    assert db_conn.execute(
        "SELECT status FROM agent_impersonations WHERE id=%s", (session["id"],)
    ).fetchone() == ("active",)
    for _ in range(2):
        db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (owner.agent_id,))
        db_conn.commit()
    assert len(_native_notices(db_conn, owner.agent_id)) == 2


async def test_restart_and_termination_without_a_lease_add_no_notices(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    owner, session = await _termination_session(db_conn, aops_pool)
    db_conn.execute("UPDATE agents_meta SET status='idling' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    assert _native_notices(db_conn, owner.agent_id) == []
    assert db_conn.execute(
        "SELECT status,rejection_reason FROM agent_impersonations WHERE id=%s", (session["id"],)
    ).fetchone() == ("active", None)
    ordinary_id = create_agent(db_conn)
    db_conn.execute("INSERT INTO agents_meta(id,status) VALUES(%s,'running')", (ordinary_id,))
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (ordinary_id,))
    db_conn.commit()
    assert (
        db_conn.execute(
            "SELECT id FROM inbound_messages WHERE agent_id=%s", (ordinary_id,)
        ).fetchall()
        == []
    )


@pytest.mark.parametrize("transport_dead", [False, True])
async def test_terminal_relay_start_delivers_interruption_best_effort(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    transport_dead: bool,
) -> None:
    from cli.commands.agents import impersonation as cli_impersonation
    from cli.parsers import build_parser

    owner, session = await _termination_session(db_conn, aops_pool)
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    emitted: list[str] = []

    def emit(message: str) -> None:
        emitted.append(message)
        if transport_dead:
            raise OSError("executor transport is gone")

    def forbidden(_db: object, *_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("A terminal relay must not read/reserve an ordinary inbox or renew its lease")

    monkeypatch.setattr(relay, "monitor_claude", emit)
    monkeypatch.setattr(cli_impersonation, "relay_token_from_env", lambda: session["relay_token"])
    monkeypatch.setattr(leases, "relay_inbox", forbidden)
    monkeypatch.setattr(leases, "renew", forbidden)
    monkeypatch.setattr(relay, "reserve_delivery", forbidden)
    args = build_parser().parse_args(
        [
            "impersonate",
            "relay",
            str(owner.agent_id),
            "--lease-id",
            session["id"],
            "--provider",
            "claude",
        ]
    )
    # cmd_relay owns asyncio.run, as a real separately launched relay would.
    import asyncio

    result = await asyncio.to_thread(relay.cmd_relay, args)
    assert result == (1 if transport_dead else 0)
    assert len(emitted) == 1
    assert f"Ava impersonation lease {session['session_id']}" in emitted[0]
    assert session["id"] in emitted[0]
    assert "Ended at:" in emitted[0]
    assert "does not end or cancel any newer" in emitted[0]
    assert "The agent was terminated" in emitted[0]
    assert "no active native runtime is implied" in emitted[0]
    assert "ACK" not in emitted[0]
    assert len(_native_notices(db_conn, owner.agent_id)) == 2
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (owner.agent_id,)
    ).fetchone() == ("terminated",)


async def test_running_relay_delivers_termination_once_without_reserving_input(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    owner, session = await _termination_session(db_conn, aops_pool)
    reads = 0
    emitted: list[str] = []

    async def read() -> relay.InboxSnapshot:
        nonlocal reads
        reads += 1
        if reads == 3:
            db_conn.execute(
                "UPDATE agents_meta SET status='terminated' WHERE id=%s", (owner.agent_id,)
            )
            db_conn.commit()
        return relay._read_inbox(
            database, event_bus, owner.agent_id, lease_uuid, session["relay_token"]
        )

    async def reserve(_ids: list[int]) -> frozenset[int]:
        pytest.fail("A terminal notice cannot reserve ordinary input")

    class Listener:
        closed = False

        async def ensure_listening(self) -> None:
            pass

        async def wait_one(self, timeout: float) -> None:
            pytest.fail("The terminated relay must exit before waiting again")

        async def close(self) -> None:
            self.closed = True

    lease_uuid = UUID(session["id"])
    monkeypatch.setattr(relay, "monitor_claude", emitted.append)
    listener = Listener()
    await relay.relay_inbox(
        owner.agent_id,
        session["session_id"],
        read_inbox=read,
        reserve=reserve,
        listener=listener,
        emit=relay.host_emitter("claude", None),
    )
    assert len(emitted) == 2
    assert emitted[0].startswith("Handoff brief\n\nAva control active:")
    assert f"Ava impersonation lease {session['session_id']}" in emitted[1]
    assert session["id"] in emitted[1]
    assert "Ended at:" in emitted[1]
    assert "does not end or cancel any newer" in emitted[1]
    assert "The agent was terminated" in emitted[1]
    assert "no active native runtime is implied" in emitted[1]
    assert listener.closed


async def test_resurrection_timestamp_follows_notes_even_in_an_older_transaction(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:

    owner, _session = await _termination_session(db_conn, aops_pool)
    started = db_conn.execute("SELECT transaction_timestamp()").fetchone()
    assert started is not None
    with pool(max_size=1) as other, other.connection() as conn:
        conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (owner.agent_id,))
    notices = _native_notices(db_conn, owner.agent_id)
    assert notices[0][4] > started[0]

    @contextmanager
    def earlier_transaction(_self: Database):
        yield db_conn

    monkeypatch.setattr(Database, "write_transaction", earlier_transaction)
    resurrect_agent(database, event_bus, owner.agent_id, resurrected_by="user", prompt="Continue")
    ordered = db_conn.execute(
        "SELECT kind,payload->>'note_tag' FROM inbound_messages "
        "WHERE agent_id=%s ORDER BY created_at,id",
        (owner.agent_id,),
    ).fetchall()
    assert ordered == [
        ("system_note", "impersonation"),
        ("system_note", "lifecycle_terminate"),
        ("resurrect", None),
        ("chat", None),
    ]
