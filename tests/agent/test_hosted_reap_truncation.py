"""The update straggler reap ends a turn as truncated, never as a crash.

`ops.agent_pause` CAS-marks an un-landed cohort member 'restarting' mid-turn
(task #4016; the 2026-09-20 macmini drain reaped seven of them); the member's
next fail-closed probe then raises ImpersonationError ("Native runtime no
longer owns this agent") from any of its sites — the graph's hook/claim guards,
the turn-boundary settle probes (host.py:678/744), the held-controls probe
(host.py:366). `services.agent_host.truncation` classifies exactly that mark —
bound to the turn's own incarnation — as the deliberate truncation the drain
already released, so every one of those sites closes quietly (no crash, no
error event, no failure receipt); a replaced row or a genuinely lapsed lease
still raises (fail-closed, unchanged).
"""

from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.impersonation import claim_gate
from services.agent_host.host import AgentHost
from services.agent_host.settlement import close_hosted_turn
from services.agent_host.truncation import reap_truncation_outcome
from shared.context import AvaContext
from shared.impersonation import ImpersonationError
from shared.turn_identity import bind_turn_identity
from tests.agent.test_inbound_ownership import _admit, _agent

_REAP_ERROR = "Native runtime no longer owns this agent"

_MAINTENANCE_PAYLOAD: dict[str, object] = {
    "maintenance": {"holder": "ops:test:truncation", "acquired_at": "2026-09-20T00:00:00+00:00"}
}


def _host(graph: Mock, pool: AsyncConnectionPool) -> AgentHost:
    return AgentHost(pool=pool, checkpointer=Mock(), graph=graph, machine="claim-test")


def _raising_graph() -> Mock:
    graph = Mock()
    graph.ainvoke = AsyncMock(side_effect=ImpersonationError(_REAP_ERROR))
    return graph


def _reap_command(conn: psycopg.Connection, agent_id: int) -> int:
    """The member's un-applied maintenance restart: the mark's other half."""
    row = conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source,payload) "
        "VALUES (%s,'','restart','system:maintenance',%s) RETURNING id",
        (agent_id, Jsonb(_MAINTENANCE_PAYLOAD)),
    ).fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def _mark_for_reap(conn: psycopg.Connection, agent_id: int) -> None:
    """The drain's CAS shape: 'restarting' while the un-applied restart is live."""
    conn.execute("UPDATE agents_meta SET status='restarting' WHERE id=%s", (agent_id,))
    conn.commit()


async def test_the_mark_mid_invocation_truncates_instead_of_crashing(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    """The graph's guards (hook fence / claim gate) refuse under the mark."""
    agent_id = _agent(db_conn)
    incarnation = await _admit(aops_pool, agent_id)
    command = _reap_command(db_conn, agent_id)
    publisher = Mock()

    async def graph_return(*args: object, **kwargs: object) -> dict[str, object]:
        # The mark lands while the invocation runs, exactly as observed on
        # agent 2697 (2026-09-20 04:55:38Z): the guard read then refuses.
        _mark_for_reap(db_conn, agent_id)
        raise ImpersonationError(_REAP_ERROR)

    graph = Mock()
    graph.ainvoke = AsyncMock(side_effect=graph_return)
    host = _host(graph, aops_pool)
    host._runtimes[agent_id] = Mock()
    with bind_turn_identity(agent_id, incarnation=incarnation):
        outcome = await host._invoke_until_done(
            agent_id, AvaContext(ops_pool=aops_pool, event_publisher=publisher)
        )

    assert outcome.truncated and not outcome.crashed and not outcome.exited
    assert graph.ainvoke.await_count == 1  # the mark landed mid-invocation
    publisher.emit.assert_not_called()  # no error event on the truncation
    # Dropped so the successor's admission is cold and re-runs the reconcile.
    assert agent_id not in host._runtimes

    # The settle boundary leaves the row exactly as the reap left it: no corpse
    # marker, ownership records intact, the never-applied command still open.
    await close_hosted_turn(aops_pool, aops_pool, Mock(), incarnation, outcome)
    assert db_conn.execute(
        "SELECT status, runtime_owner, runtime_generation, last_turn_fatal_at "
        "FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone() == ("restarting", incarnation.owner, incarnation.generation, None)
    assert db_conn.execute(
        "SELECT status, applied_at FROM inbound_messages WHERE id=%s", (command,)
    ).fetchone() == ("pending", None)


async def test_the_loop_top_settle_probe_truncates_quietly(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    """Site host.py:678 — the turn starts already under the mark."""
    agent_id = _agent(db_conn)
    incarnation = await _admit(aops_pool, agent_id)
    _reap_command(db_conn, agent_id)
    _mark_for_reap(db_conn, agent_id)
    graph = _raising_graph()
    host = _host(graph, aops_pool)
    with bind_turn_identity(agent_id, incarnation=incarnation):
        outcome = await host._invoke_until_done(agent_id, AvaContext(ops_pool=aops_pool))

    assert outcome.truncated and not outcome.crashed
    assert graph.ainvoke.await_count == 0  # the settle probe refused first


async def test_the_turn_idle_settle_probe_truncates_quietly(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    """Site host.py:744 — the mark lands after the invocation returns."""
    agent_id = _agent(db_conn)
    incarnation = await _admit(aops_pool, agent_id)
    _reap_command(db_conn, agent_id)
    graph = Mock()

    async def graph_return(*args: object, **kwargs: object) -> dict[str, object]:
        _mark_for_reap(db_conn, agent_id)
        return {"exit_requested": False, "restart_requested": False, "turn_idle": True}

    graph.ainvoke = AsyncMock(side_effect=graph_return)
    host = _host(graph, aops_pool)
    with bind_turn_identity(agent_id, incarnation=incarnation):
        outcome = await host._invoke_until_done(agent_id, AvaContext(ops_pool=aops_pool))

    assert outcome.truncated and not outcome.crashed
    assert graph.ainvoke.await_count == 1


async def test_the_claim_gate_refusal_is_the_classified_truncation(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    """Site agent.impersonation.claim_gate — the raise the host classifies."""
    agent_id = _agent(db_conn)
    incarnation = await _admit(aops_pool, agent_id)
    _reap_command(db_conn, agent_id)
    _mark_for_reap(db_conn, agent_id)
    with bind_turn_identity(agent_id, incarnation=incarnation):
        with pytest.raises(ImpersonationError) as raised:
            await claim_gate(Mock(), agent_id)
        # The boundary classifies while the turn's identity is still bound.
        outcome = await reap_truncation_outcome(raised.value, aops_pool, agent_id)
    assert outcome is not None and outcome.truncated


async def test_a_held_wake_stops_quietly_under_the_mark(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Site host.py:366 (held-controls probe) — the wake must not crash.

    The narrow race the guard closes: admission landed first, then the reap
    mark (an admission on a 'restarting' row is refused before the probe).
    """
    agent_id = _agent(db_conn)
    incarnation = await _admit(aops_pool, agent_id)
    _reap_command(db_conn, agent_id)
    _mark_for_reap(db_conn, agent_id)
    monkeypatch.setattr(
        "services.agent_host.host.admit_hosted_runtime", AsyncMock(return_value=incarnation)
    )
    host = _host(_raising_graph(), aops_pool)
    await host._run_held_controls(agent_id, "running")  # must not raise


async def test_a_replaced_row_under_the_mark_still_crashes(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    """The mark is a truncation only for the incarnation that owns it."""
    agent_id = _agent(db_conn)
    incarnation = await _admit(aops_pool, agent_id)
    _reap_command(db_conn, agent_id)
    db_conn.execute(
        "UPDATE agents_meta SET status='restarting', runtime_owner=%s WHERE id=%s",
        (uuid4(), agent_id),
    )
    db_conn.commit()

    host = _host(_raising_graph(), aops_pool)
    with bind_turn_identity(agent_id, incarnation=incarnation), pytest.raises(ImpersonationError):
        await host._invoke_until_done(agent_id, AvaContext(ops_pool=aops_pool))


async def test_a_lapsed_lease_without_the_mark_still_crashes(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    """Renewal is the host beat's job; a truly expired lease stays fail-closed."""
    agent_id = _agent(db_conn)
    incarnation = await _admit(aops_pool, agent_id)
    db_conn.execute(
        "UPDATE agents_meta SET lease_expires_at = now() - interval '5 minutes' WHERE id=%s",
        (agent_id,),
    )
    db_conn.commit()

    host = _host(_raising_graph(), aops_pool)
    with bind_turn_identity(agent_id, incarnation=incarnation), pytest.raises(ImpersonationError):
        await host._invoke_until_done(agent_id, AvaContext(ops_pool=aops_pool))
