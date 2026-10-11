"""Force acceptance cannot outrun a live hosted continuation or its exec child."""

import asyncio
import json
import os
import threading
import traceback
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.db import has_pending_interrupt
from agent.graph.exec._subprocess import _run_in_subprocess
from agent.ownership import hosted
from agent.tests.claim.test_inbound_ownership import _insert, agent_row
from base.agents.context import AvaContext
from base.agents.incarnation.hosted_force import original_host_force
from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.native_process.turn_identity import HostedTurnResources
from ops.agents.resurrection_retry import ResurrectSettlementDeferredError
from ops.agents.wake import resurrect_agent
from ops.lifecycle.termination import _force_terminate_transaction
from services.agent_runner.agent_host.dispatcher import TurnScheduler
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.scheduling.health_routes import cancel_turn_route
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from services.agent_runner.agent_host.tests.lifecycle.force_host_setup import (
    host_wakes_need_no_provider_credentials,
)
from tests.fixtures.pin_agent import exec_context as ctx_of


def _blocking_work(entered: threading.Event, release: threading.Event) -> None:
    entered.set()
    assert release.wait(20), "test must release the real thread"


def _observed_host(
    pool: AsyncConnectionPool,
    graph: Mock,
    patch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> tuple[AgentHost, list[str]]:
    host = AgentHost(
        policy=configured_policy(),
        pool=pool,
        checkpointer=Mock(),
        graph=graph,
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
    )
    patch.setattr(host, "_runtime_for", AsyncMock(return_value=Mock(llm=None)))
    original = host._run_turn
    errors: list[str] = []

    async def observed(agent_id: int, *, resources: HostedTurnResources | None) -> None:
        try:
            await original(agent_id, resources=resources)
        except BaseException:
            errors.append(traceback.format_exc())
            raise

    patch.setattr(host, "_run_turn", observed)
    return host, errors


def _configure_late_reader(
    kind: str, patch: pytest.MonkeyPatch, release: threading.Event, agent_id: int
) -> None:
    if kind != "reader":
        return
    reader_name = f"exec-reader-{agent_id}"
    original_run = threading.Thread.run
    original_join = threading.Thread.join

    def delayed(thread: threading.Thread) -> None:
        original_run(thread)
        if thread.name == reader_name:
            assert release.wait(20), "test must release real output reader"

    def bounded_join(thread: threading.Thread, timeout: float | None = None) -> None:
        if thread.name == reader_name and timeout is not None:
            timeout = min(timeout, 0.01)
        original_join(thread, timeout)

    patch.setattr(threading.Thread, "run", delayed)
    patch.setattr(threading.Thread, "join", bounded_join)


async def _assert_pending_force(
    conn: psycopg.Connection,
    pool: AsyncConnectionPool,
    agent_id: int,
    command: int,
    chat: int,
    *,
    database_gate: ProcessDbGate,
) -> None:
    assert await has_pending_interrupt(pool, agent_id, incarnation=None, work=None)
    assert conn.execute(
        "SELECT status,applied_at IS NOT NULL,observed_at FROM inbound_messages WHERE id=%s",
        (command,),
    ).fetchone() == ("claimed", True, None)
    conn.commit()
    handles = Database.from_settings(gate=database_gate), EventBus.from_settings()
    with pytest.raises(ResurrectSettlementDeferredError):
        await asyncio.to_thread(resurrect_agent, *handles, agent_id, resurrected_by="user")
    assert (
        await hosted.admit_hosted_runtime(
            pool, agent_id, "claim-test", uuid4(), expected_from="terminated", db=handles[0]
        )
        is None
    )
    assert conn.execute("SELECT status FROM inbound_messages WHERE id=%s", (chat,)).fetchone() == (
        "pending",
    )
    conn.commit()


async def _prove_successor_ignores_old_cancel(
    conn: psycopg.Connection,
    pool: AsyncConnectionPool,
    patch: pytest.MonkeyPatch,
    agent_id: int,
    graph: Mock,
    payload: bytes,
    entered: asyncio.Event,
    release: asyncio.Event,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    # Simulate explicit resurrection's allocation, not a claim of RPC coverage.
    conn.execute("UPDATE agents_meta SET status='idling' WHERE id=%s", (agent_id,))
    conn.commit()
    replacement = AgentHost(
        policy=configured_policy(),
        pool=pool,
        checkpointer=Mock(),
        graph=graph,
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
    )
    patch.setattr(replacement, "_runtime_for", AsyncMock(return_value=Mock()))
    scheduler = TurnScheduler(replacement.run_turn)
    scheduler.wake(agent_id)
    try:
        await asyncio.wait_for(entered.wait(), 3)
        route = cancel_turn_route(scheduler, replacement)
        _, response, _ = await route(payload)
        assert json.loads(response) == {"cancelled": False}
        assert agent_id in scheduler.active_agents
        release.set()
        async with asyncio.timeout(3):
            while agent_id in scheduler.active_agents:
                await asyncio.sleep(0.01)
    finally:
        release.set()
        await scheduler.aclose()


def _exec_context(hosted: AvaContext) -> AvaContext:
    """The fake host is SQL-only; exec keeps the test SDK clients and actual custody."""
    context = replace(
        ctx_of(hosted.require_identity().agent_id),
        original_incarnation=hosted.original_incarnation,
        native_work=hosted.native_work,
        hosted_resources=hosted.hosted_resources,
    )
    assert context.hosted_resources is hosted.hosted_resources
    return context


@pytest.fixture
def _exec_delivery_environment() -> Iterator[None]:
    """Restore delivery from the real cold SDK owner throughout its exec lifetime."""
    with patch.dict(os.environ):
        yield


@pytest.mark.usefixtures("_exec_delivery_environment")
@pytest.mark.parametrize("work_kind", ["thread", "exec", "reader"])
async def test_force_waits_for_real_work_and_delayed_cancel_cannot_hit_successor(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    work_kind: str,
    database: Database,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    entered, release = threading.Event(), threading.Event()
    _configure_late_reader(work_kind, monkeypatch, release, agent_id)
    marker, release_file = tmp_path / "entered", tmp_path / "release"
    successor_entered, successor_release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def graph_return(*args: object, **kwargs: object) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        if calls > 1:
            successor_entered.set()
            await successor_release.wait()
        elif work_kind == "thread":
            await asyncio.to_thread(_blocking_work, entered, release)
        else:
            await _run_in_subprocess(
                database,
                "from pathlib import Path\nimport time\n"
                f"Path({str(marker)!r}).touch()\n"
                + (
                    "print('reader finishes after bounded join')\n"
                    if work_kind == "reader"
                    else f"while not Path({str(release_file)!r}).exists(): time.sleep(0.01)\n"
                ),
                _exec_context(cast(AvaContext, kwargs["context"])),
                asyncio.Event(),
                20,
                exec_dir=tmp_path,
                accumulation_max_chars=1_000_000,
            )
        return {"exit_requested": False, "restart_requested": False, "turn_idle": True}

    graph = Mock()
    graph.ainvoke = AsyncMock(side_effect=graph_return)
    host, errors = _observed_host(
        aops_pool, graph, monkeypatch, model_catalog=model_catalog, database_gate=database_gate
    )
    monkeypatch.setattr("services.agent_runner.agent_host.dispatcher.CANCEL_UNWIND_TIMEOUT_S", 0.05)
    scheduler = TurnScheduler(host.run_turn)
    scheduler.wake(agent_id)
    try:
        async with asyncio.timeout(15):
            while not (entered.is_set() if work_kind == "thread" else marker.exists()):
                assert not errors, "\n".join(errors)
                await asyncio.sleep(0.01)
        with ConnectionPool[psycopg.Connection](settings.data_plane.db_url) as pool:
            _, _, _, command, _cutoff = await asyncio.to_thread(
                _force_terminate_transaction, agent_id, pool, source="user"
            )
        chat = _insert(db_conn, agent_id)
        payload = json.dumps({"agent_id": agent_id, "command_id": command}).encode()
        status, response, _ = await cancel_turn_route(scheduler, host)(payload)
        assert status == 200 and json.loads(response) == {"cancelled": False}
        assert agent_id in scheduler.active_agents
        await _assert_pending_force(
            db_conn, aops_pool, agent_id, command, chat, database_gate=database_gate
        )
        release.set()
        release_file.touch()
        async with asyncio.timeout(10):
            while agent_id in scheduler.active_agents:
                await asyncio.sleep(0.01)
        assert db_conn.execute(
            "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (command,)
        ).fetchone() == ("done", True)
        await _prove_successor_ignores_old_cancel(
            db_conn,
            aops_pool,
            monkeypatch,
            agent_id,
            graph,
            payload,
            successor_entered,
            successor_release,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        assert calls == 2
    finally:
        release.set()
        release_file.touch()
        successor_release.set()
        await scheduler.aclose()


async def test_idle_force_only_original_live_host_can_observe(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    host = AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
    )
    assert (
        await hosted.admit_hosted_runtime(
            aops_pool, agent_id, "claim-test", host._owner, expected_from="idling", db=database
        )
        is not None
    )
    with ConnectionPool[psycopg.Connection](settings.data_plane.db_url) as pool:
        _, _, _, command, _cutoff = await asyncio.to_thread(
            _force_terminate_transaction, agent_id, pool, source="user"
        )
    assert not await original_host_force(
        aops_pool, agent_id, uuid4(), "claim-test", command_id=command, quiescent=True
    )
    assert [wake.agent_id for wake in await host.pending_inbound_wakes(0)] == [agent_id]
    await host.run_turn(agent_id)
    assert db_conn.execute(
        "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (command,)
    ).fetchone() == ("done", True)


pytestmark = pytest.mark.usefixtures(host_wakes_need_no_provider_credentials.__name__)
