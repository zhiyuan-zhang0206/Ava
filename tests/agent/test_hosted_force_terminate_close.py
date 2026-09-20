"""An externally commanded force terminate ends a turn quietly, never as a crash.

The delivery watchdog's hosted-turn wedge recovery (`services/delivery_watchdog/
turn_liveness.py`) force-terminates a wedged hosted incarnation
(`terminate_agent_op(force=True, source='system')`): the durable force command
is applied and anchored on `agents_meta.lifecycle_command_id`, and the pump's
own boundary observes it (`original_host_force(quiescent=True)`) as the turn
ends. Between those two facts the wedged turn's next fail-closed probe refuses
("Native runtime no longer owns this agent"); `services.agent_host.
force_termination` classifies exactly that command — bound to the turn's own
incarnation, still unobserved, still anchored — as the deliberate termination
it is, so the turn closes quietly (no crash, no error event, no failure
receipt); an observed, foreign-target, or detached command — or any other
exception — still raises (fail-closed, unchanged).
"""

from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from services.agent_host.host import AgentHost
from services.agent_host.settlement import close_hosted_turn
from shared.context import AvaContext
from shared.hosted_force import install_hosted_force
from shared.impersonation import ImpersonationError
from shared.turn_identity import bind_turn_identity
from tests.agent.test_inbound_ownership import _admit, _agent

_FORCE_ERROR = "Native runtime no longer owns this agent"


def _host(graph: Mock, pool: AsyncConnectionPool) -> AgentHost:
    return AgentHost(pool=pool, checkpointer=Mock(), graph=graph, machine="claim-test")


def _raising_graph(exc: BaseException | None = None) -> Mock:
    graph = Mock()
    graph.ainvoke = AsyncMock(side_effect=exc or ImpersonationError(_FORCE_ERROR))
    return graph


def _apply_force(conn: psycopg.Connection, agent_id: int, *, source: str = "system") -> int:
    """The wedge recovery's durable shape: applied force + live pointer + terminated row."""
    row = conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) "
        "VALUES(%s,'','terminate',%s) RETURNING id",
        (agent_id, source),
    ).fetchone()
    assert row is not None
    command_id = int(row[0])
    install_hosted_force(conn, agent_id, command_id)
    conn.execute(
        "UPDATE agents_meta SET status='terminated',termination_source='user',"
        "last_force_terminate_inbound_id=%s WHERE id=%s",
        (command_id, agent_id),
    )
    conn.commit()
    return command_id


def _resurrect_shape(conn: psycopg.Connection, agent_id: int) -> None:
    """The home runner's final resurrect CAS: terminated -> idling, incarnation NULLed."""
    conn.execute(
        "UPDATE agents_meta SET status='idling', pid=NULL, termination_source=NULL, "
        "lease_expires_at=NULL, last_turn_fatal_at=NULL, runtime_generation=NULL, "
        "runtime_owner=NULL, runtime_kind=NULL WHERE id=%s",
        (agent_id,),
    )
    conn.commit()


async def test_the_applied_force_mid_invocation_closes_quietly(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = _agent(db_conn)
    incarnation = await _admit(aops_pool, agent_id)
    publisher = Mock()
    commands: list[int] = []

    async def graph_return(*args: object, **kwargs: object) -> dict[str, object]:
        # The force lands while the invocation runs, exactly as observed on
        # company-air 6240 (2026-09-20 07:33:21Z -> 07:33:49Z): the guard read
        # then refuses.
        commands.append(_apply_force(db_conn, agent_id))
        raise ImpersonationError(_FORCE_ERROR)

    graph = Mock()
    graph.ainvoke = AsyncMock(side_effect=graph_return)
    host = _host(graph, aops_pool)
    host._runtimes[agent_id] = Mock()
    with bind_turn_identity(agent_id, incarnation=incarnation):
        outcome = await host._invoke_until_done(
            agent_id, AvaContext(ops_pool=aops_pool, event_publisher=publisher)
        )

    assert outcome.truncated and not outcome.crashed and not outcome.exited
    assert graph.ainvoke.await_count == 1  # the force landed mid-invocation
    publisher.emit.assert_not_called()  # no error event on the quiet close
    assert agent_id not in host._runtimes  # dropped for the successor

    # The classify does NOT consume the command: the pump's own boundary
    # observes it, and the settle boundary leaves the row as the force left it.
    await close_hosted_turn(aops_pool, aops_pool, Mock(), incarnation, outcome)
    assert db_conn.execute(
        "SELECT status, last_turn_fatal_at FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("terminated", None)
    assert db_conn.execute(
        "SELECT status, observed_at FROM inbound_messages WHERE agent_id=%s", (agent_id,)
    ).fetchone() == ("claimed", None)
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (commands[0],)


async def test_the_force_still_classifies_after_the_resurrect_nulls_the_row(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    """The command's stored target binds it across the resurrect epoch."""
    agent_id = _agent(db_conn)
    incarnation = await _admit(aops_pool, agent_id)
    _apply_force(db_conn, agent_id)
    _resurrect_shape(db_conn, agent_id)
    host = _host(_raising_graph(), aops_pool)
    with bind_turn_identity(agent_id, incarnation=incarnation):
        outcome = await host._invoke_until_done(agent_id, AvaContext(ops_pool=aops_pool))
    assert outcome.truncated and not outcome.crashed


async def test_the_turn_starting_under_the_force_closes_quietly(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    """Site host.py:678 — the turn starts already under the applied force."""
    agent_id = _agent(db_conn)
    incarnation = await _admit(aops_pool, agent_id)
    _apply_force(db_conn, agent_id)
    graph = _raising_graph()
    host = _host(graph, aops_pool)
    with bind_turn_identity(agent_id, incarnation=incarnation):
        outcome = await host._invoke_until_done(agent_id, AvaContext(ops_pool=aops_pool))
    assert outcome.truncated and not outcome.crashed
    assert graph.ainvoke.await_count == 0  # the settle probe refused first


async def test_a_cli_style_user_force_closes_quietly_too(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    """No source whitelist: the durable shape, not the caller, classifies."""
    agent_id = _agent(db_conn)
    incarnation = await _admit(aops_pool, agent_id)
    _apply_force(db_conn, agent_id, source="user")
    host = _host(_raising_graph(), aops_pool)
    with bind_turn_identity(agent_id, incarnation=incarnation):
        outcome = await host._invoke_until_done(agent_id, AvaContext(ops_pool=aops_pool))
    assert outcome.truncated and not outcome.crashed


async def test_the_held_wake_force_guard_stops_quietly(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Site host.py:366 (held-controls probe) — the wake must not crash."""
    agent_id = _agent(db_conn)
    incarnation = await _admit(aops_pool, agent_id)
    _apply_force(db_conn, agent_id)
    monkeypatch.setattr(
        "services.agent_host.host.admit_hosted_runtime", AsyncMock(return_value=incarnation)
    )
    host = _host(_raising_graph(), aops_pool)
    await host._run_held_controls(agent_id, "running")  # must not raise


async def test_an_observed_force_still_crashes(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = _agent(db_conn)
    incarnation = await _admit(aops_pool, agent_id)
    command = _apply_force(db_conn, agent_id)
    db_conn.execute(
        "UPDATE inbound_messages SET observed_at=clock_timestamp(),status='done' WHERE id=%s",
        (command,),
    )
    db_conn.execute("UPDATE agents_meta SET lifecycle_command_id=NULL WHERE id=%s", (agent_id,))
    db_conn.commit()
    host = _host(_raising_graph(), aops_pool)
    with bind_turn_identity(agent_id, incarnation=incarnation), pytest.raises(ImpersonationError):
        await host._invoke_until_done(agent_id, AvaContext(ops_pool=aops_pool))


async def test_a_foreign_incarnation_force_still_crashes(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = _agent(db_conn)
    incarnation = await _admit(aops_pool, agent_id)
    command = _apply_force(db_conn, agent_id)
    db_conn.execute("UPDATE inbound_messages SET target_owner=%s WHERE id=%s", (uuid4(), command))
    db_conn.commit()
    host = _host(_raising_graph(), aops_pool)
    with bind_turn_identity(agent_id, incarnation=incarnation), pytest.raises(ImpersonationError):
        await host._invoke_until_done(agent_id, AvaContext(ops_pool=aops_pool))


async def test_a_detached_pointer_still_crashes(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = _agent(db_conn)
    incarnation = await _admit(aops_pool, agent_id)
    command = _apply_force(db_conn, agent_id)
    assert command
    db_conn.execute("UPDATE agents_meta SET lifecycle_command_id=NULL WHERE id=%s", (agent_id,))
    db_conn.commit()
    host = _host(_raising_graph(), aops_pool)
    with bind_turn_identity(agent_id, incarnation=incarnation), pytest.raises(ImpersonationError):
        await host._invoke_until_done(agent_id, AvaContext(ops_pool=aops_pool))


async def test_a_non_impersonation_exception_still_crashes(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = _agent(db_conn)
    incarnation = await _admit(aops_pool, agent_id)

    async def graph_return(*args: object, **kwargs: object) -> dict[str, object]:
        _apply_force(db_conn, agent_id)
        raise RuntimeError("boom")

    graph = Mock()
    graph.ainvoke = AsyncMock(side_effect=graph_return)
    host = _host(graph, aops_pool)
    with bind_turn_identity(agent_id, incarnation=incarnation), pytest.raises(RuntimeError):
        await host._invoke_until_done(agent_id, AvaContext(ops_pool=aops_pool))
