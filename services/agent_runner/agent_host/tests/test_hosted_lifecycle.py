"""Hosted application waits for graph return, then uses the durable command."""

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.db import claim_inbound_batch
from agent.ownership.hosted import (
    admit_hosted_runtime,
    apply_hosted_lifecycle,
    settle_hosted_runtime,
)
from agent.ownership.tests.test_lifecycle_intent import _command
from agent.tests.test_inbound_ownership import _admit, _agent
from base.agents.context import AvaContext
from base.agents.incarnation.hosted_force import recover_orphaned_hosted_forces
from base.config import settings
from base.db import Database
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import bind_turn_identity
from ops.lifecycle.termination import _force_terminate_transaction
from services.agent_runner.agent_host.force_termination import kill_terminating_agent_shells
from services.agent_runner.agent_host.host import AgentHost


def _graph_blocked_until_released(
    kind: str, entered: asyncio.Event, release: asyncio.Event
) -> Mock:
    """A graph whose ainvoke parks until `release`, then returns the lifecycle request."""

    async def graph_return(*args: object, **kwargs: object) -> dict[str, bool]:
        entered.set()
        await release.wait()
        return {
            "exit_requested": kind == "terminate",
            "restart_requested": kind == "restart",
            "turn_idle": False,
        }

    graph = Mock()
    graph.ainvoke = AsyncMock(side_effect=graph_return)
    return graph


def _assert_command_unapplied_while_continuation_runs(
    db_conn: psycopg.Connection,
    host: AgentHost,
    old: RuntimeIncarnation,
    *,
    agent_id: int,
    inbound: int,
) -> None:
    assert db_conn.execute(
        "SELECT runtime_generation,runtime_owner,status FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone() == (old.generation, old.owner, "running")
    assert db_conn.execute(
        "SELECT applied_at,observed_at FROM inbound_messages WHERE id=%s", (inbound,)
    ).fetchone() == (None, None)
    assert agent_id in host._runtimes


async def _assert_restart_observed_by_next_admission(
    db_conn: psycopg.Connection,
    pool: AsyncConnectionPool,
    old: RuntimeIncarnation,
    database: Database,
    event_bus: EventBus,
    *,
    agent_id: int,
    inbound: int,
) -> None:
    assert not await settle_hosted_runtime(pool, old, bus=event_bus)
    new = await admit_hosted_runtime(
        pool, agent_id, "claim-test", uuid4(), expected_from="idling", db=database
    )
    assert new is not None and new.generation != old.generation
    assert db_conn.execute(
        "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (inbound,)
    ).fetchone() == ("done", True)


@pytest.mark.parametrize("kind", ["restart", "terminate"])
async def test_hosted_applies_only_after_continuation_returns(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    kind: str,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id = _agent(db_conn)
    old = await _admit(aops_pool, agent_id)
    inbound = _command(db_conn, agent_id, kind)
    entered, release = asyncio.Event(), asyncio.Event()
    host = AgentHost(
        pool=aops_pool,
        checkpointer=Mock(),
        graph=_graph_blocked_until_released(kind, entered, release),
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
    )
    host._runtimes[agent_id] = Mock()
    with bind_turn_identity(agent_id, incarnation=old):
        assert [row.id for row in await claim_inbound_batch(aops_pool, agent_id)] == [inbound]
        assert db_conn.execute(
            "SELECT status FROM inbound_messages WHERE id=%s", (inbound,)
        ).fetchone() == ("claimed",)
        task = asyncio.create_task(
            host._invoke_until_done(
                agent_id,
                AvaContext(
                    ops_pool=aops_pool,
                    agent=AgentSlices.resolve(),
                    db=Database.from_settings(),
                    bus=EventBus.from_settings(),
                ),
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        try:
            _assert_command_unapplied_while_continuation_runs(
                db_conn, host, old, agent_id=agent_id, inbound=inbound
            )
        finally:
            release.set()
            await asyncio.wait_for(task, 3)
    assert agent_id not in host._runtimes
    record = db_conn.execute(
        "SELECT status,applied_at,observed_at FROM inbound_messages WHERE id=%s", (inbound,)
    ).fetchone()
    assert record is not None and record[1] is not None
    if kind == "terminate":
        assert record[0] == "done" and record[2] is not None
    else:
        assert record[0] == "claimed" and record[2] is None
        await _assert_restart_observed_by_next_admission(
            db_conn, aops_pool, old, database, event_bus, agent_id=agent_id, inbound=inbound
        )
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (None,)


@pytest.mark.parametrize("crash", ["after_cache_drop", "before_observe", "after_commit"])
async def test_hosted_terminate_crash_has_no_applied_unobserved_gap(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    crash: str,
    event_bus: EventBus,
) -> None:
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    inbound = _command(db_conn, agent_id, "terminate")
    graph = Mock()
    graph.ainvoke = AsyncMock(
        return_value={"exit_requested": True, "restart_requested": False, "turn_idle": False}
    )
    host = AgentHost(
        pool=aops_pool,
        checkpointer=Mock(),
        graph=graph,
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
    )
    host._runtimes[agent_id] = Mock()
    original_execute = psycopg.AsyncConnection.execute
    original_drop = host.drop_agent

    async def fail_observe(
        conn: psycopg.AsyncConnection, query: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if "UPDATE inbound_messages SET observed_at=" in str(query):
            raise RuntimeError("injected observation crash")
        return await original_execute(conn, query, *args, **kwargs)

    def fail_drop(target: int) -> None:
        original_drop(target)
        raise RuntimeError("injected cache crash")

    async def fail_after_commit(
        pool: AsyncConnectionPool, token: RuntimeIncarnation, **kwargs: Any
    ) -> str | None:
        await apply_hosted_lifecycle(pool, token, **kwargs)
        raise RuntimeError("injected post-commit crash")

    with bind_turn_identity(agent_id, incarnation=owner):
        await claim_inbound_batch(aops_pool, agent_id)
        with monkeypatch.context() as patch:
            if crash == "after_cache_drop":
                patch.setattr(host, "drop_agent", fail_drop)
            elif crash == "before_observe":
                patch.setattr(psycopg.AsyncConnection, "execute", fail_observe)
            else:
                patch.setattr(
                    "services.agent_runner.agent_host.host.apply_hosted_lifecycle",
                    fail_after_commit,
                )
            with pytest.raises(RuntimeError, match="injected"):
                await host._invoke_until_done(
                    agent_id,
                    AvaContext(
                        ops_pool=aops_pool,
                        agent=AgentSlices.resolve(),
                        db=Database.from_settings(),
                        bus=EventBus.from_settings(),
                    ),
                )
        state = db_conn.execute(
            "SELECT status,applied_at IS NOT NULL,observed_at IS NOT NULL "
            "FROM inbound_messages WHERE id=%s",
            (inbound,),
        ).fetchone()
        assert state == (
            ("done", True, True) if crash == "after_commit" else ("claimed", False, False)
        )
        db_conn.commit()
        if crash != "after_commit":
            # Same admitted continuation can retry; cache absence is not a new owner.
            assert await host._invoke_until_done(
                agent_id,
                AvaContext(
                    ops_pool=aops_pool,
                    agent=AgentSlices.resolve(),
                    db=Database.from_settings(),
                    bus=EventBus.from_settings(),
                ),
            )
    assert db_conn.execute(
        "SELECT lifecycle_command_id,status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (None, "terminated")
    assert db_conn.execute(
        "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (inbound,)
    ).fetchone() == ("done", True)


async def test_existing_pg_backstop_finds_accepted_command_without_pending_rows(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, event_bus: EventBus
) -> None:
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    inbound = _command(db_conn, agent_id, "restart")
    with bind_turn_identity(agent_id, incarnation=owner):
        assert [row.id for row in await claim_inbound_batch(aops_pool, agent_id)] == [inbound]
    assert await settle_hosted_runtime(aops_pool, owner, bus=event_bus)
    host = AgentHost(
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
    )
    wakes = await host.pending_inbound_wakes(stale_after_s=60)
    assert agent_id in [wake.agent_id for wake in wakes]
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND status='pending'", (agent_id,)
    ).fetchone() == (0,)


# ─── kill_all_shell_sessions: the at-exit kill ───────────────────────────────
# docs/decisions/2026-09-27-terminate-has-no-closed-state.md: a graceful terminate
# that asked for it has the agent's shell sessions killed on its home host
# after the last step returned, right before the termination commits.


def _terminate_command(
    conn: psycopg.Connection, agent_id: int, *, source: str = "user", kill: bool = False
) -> int:
    row = conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source,payload) "
        "VALUES (%s,'','terminate',%s,%s) RETURNING id",
        (agent_id, source, Jsonb({"kill_all_shell_sessions": True}) if kill else None),
    ).fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def _record_kills(
    monkeypatch: pytest.MonkeyPatch, *, fail: bool = False
) -> list[tuple[int, str | None]]:
    """Patch the host's kill primitive; record (agent id, status at kill time)."""
    calls: list[tuple[int, str | None]] = []

    def _kill(agent_id: int) -> list[int]:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
            row = conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent_id,)).fetchone()
        calls.append((agent_id, None if row is None else row[0]))
        if fail:
            raise RuntimeError("failed to kill shell session(s) [4]")
        return [4]

    monkeypatch.setattr("ops.cluster_status.kill_agent_shells", _kill)
    return calls


async def _run_terminating_turn(aops_pool: AsyncConnectionPool, agent_id: int) -> None:
    graph = Mock()
    graph.ainvoke = AsyncMock(
        return_value={"exit_requested": True, "restart_requested": False, "turn_idle": False}
    )
    host = AgentHost(
        pool=aops_pool,
        checkpointer=Mock(),
        graph=graph,
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
    )
    host._runtimes[agent_id] = Mock()
    assert await host._invoke_until_done(
        agent_id,
        AvaContext(
            ops_pool=aops_pool,
            agent=AgentSlices.resolve(),
            db=Database.from_settings(),
            bus=EventBus.from_settings(),
        ),
    )


@pytest.mark.parametrize("requested", [True, False])
async def test_hosted_terminate_kills_requested_shell_sessions_before_the_death(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    requested: bool,
) -> None:
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    _terminate_command(db_conn, agent_id, kill=requested)
    kills = _record_kills(monkeypatch)
    with bind_turn_identity(agent_id, incarnation=owner):
        await claim_inbound_batch(aops_pool, agent_id)
        await _run_terminating_turn(aops_pool, agent_id)
    # The kill ran after the last step returned, before `terminated` committed.
    assert kills == ([(agent_id, "running")] if requested else [])
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("terminated",)


async def test_hosted_self_terminate_honors_a_queued_kill_request(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent's own terminate won acceptance; the operator's kill-requesting
    terminate queued behind it still takes the sessions with the death."""
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    own = _terminate_command(db_conn, agent_id, source="self")
    _terminate_command(db_conn, agent_id, kill=True)
    kills = _record_kills(monkeypatch)
    with bind_turn_identity(agent_id, incarnation=owner):
        assert [row.id for row in await claim_inbound_batch(aops_pool, agent_id)] == [own]
        await _run_terminating_turn(aops_pool, agent_id)
    assert kills == [(agent_id, "running")]


async def test_hosted_failed_kill_still_applies_the_termination(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
) -> None:
    """A session-kill failure is an ERROR, never a crashed turn: the death applies."""
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    _terminate_command(db_conn, agent_id, kill=True)
    kills = _record_kills(monkeypatch, fail=True)
    with bind_turn_identity(agent_id, incarnation=owner):
        await claim_inbound_batch(aops_pool, agent_id)
        await _run_terminating_turn(aops_pool, agent_id)
    assert kills == [(agent_id, "running")]
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("terminated",)
    assert any(
        record["level"].name == "ERROR" and "could not kill" in record["message"]
        for record in loguru_records
    )


def _any_model(*, model: str | None = None, config: dict[str, object] | None = None) -> str:
    """A fake host's wake carries no provider keys; accept the model as given."""
    del config
    return model or "deepseek-v4-flash-vision-exp"


def _record_force_sweeps(monkeypatch: pytest.MonkeyPatch, command: int) -> list[tuple[int, object]]:
    """Patch the host's kill primitive; record (agent id, command observed_at)."""
    calls: list[tuple[int, object]] = []

    def _kill(agent_id: int) -> list[int]:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
            row = conn.execute(
                "SELECT observed_at FROM inbound_messages WHERE id=%s", (command,)
            ).fetchone()
        calls.append((agent_id, None if row is None else row[0]))
        return []

    monkeypatch.setattr("ops.cluster_status.kill_agent_shells", _kill)
    return calls


@pytest.mark.parametrize("requested", [True, False])
async def test_force_settlement_sweeps_requested_shell_sessions_again(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    requested: bool,
    database: Database,
) -> None:
    """A step still draining past a force's kill may create a shell; the live
    host sweeps again when it observes the force quiescent, before recording
    the observation (docs/decisions/2026-09-27-terminate-has-no-closed-state.md)."""
    monkeypatch.setattr(
        "services.agent_runner.agent_host.runtime.validate_model_config", _any_model
    )
    agent_id = _agent(db_conn)
    host = AgentHost(
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
    )
    await admit_hosted_runtime(
        aops_pool, agent_id, "claim-test", host._owner, expected_from="idling", db=database
    )
    with ConnectionPool[psycopg.Connection](settings.data_plane.db_url) as pool:
        _, _, _, command = await asyncio.to_thread(
            _force_terminate_transaction,
            agent_id,
            pool,
            source="user",
            kill_all_shell_sessions=requested,
        )
    sweeps = _record_force_sweeps(monkeypatch, command)
    await host.run_turn(agent_id)
    assert sweeps == ([(agent_id, None)] if requested else [])
    assert db_conn.execute(
        "SELECT observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (command,)
    ).fetchone() == (True,)


async def test_boot_recovery_sweeps_a_requested_force_shell_kill(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
) -> None:
    agent_id = _agent(db_conn)
    old = AgentHost(
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
    )
    await admit_hosted_runtime(
        aops_pool, agent_id, "claim-test", old._owner, expected_from="idling", db=database
    )
    with ConnectionPool[psycopg.Connection](settings.data_plane.db_url) as pool:
        _, _, _, command = await asyncio.to_thread(
            _force_terminate_transaction,
            agent_id,
            pool,
            source="user",
            kill_all_shell_sessions=True,
        )
    monkeypatch.setattr(
        "base.agents.incarnation.exec_request_evidence.exec_run_dir", lambda: tmp_path
    )
    sweeps = _record_force_sweeps(monkeypatch, command)
    recovered, _ = await recover_orphaned_hosted_forces(
        aops_pool, "claim-test", kill_shell_sessions=kill_terminating_agent_shells
    )
    assert recovered == [agent_id]
    assert sweeps == [(agent_id, None)]
