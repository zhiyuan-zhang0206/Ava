"""The agent host settles and refuses turns according to hosted ownership."""

import asyncio
from unittest.mock import Mock
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted import admit_hosted_runtime, settle_hosted_runtime
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog


def _agent(conn: psycopg.Connection) -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, status, machine) VALUES (%s, 'idling', 'host-test') "
        "ON CONFLICT (id) DO UPDATE SET status = 'idling', machine = 'host-test'",
        (agent_id,),
    )
    conn.commit()
    return agent_id


async def test_cancel_during_live_announce_settles_the_committed_admission(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
) -> None:
    """The optional Redis announce is downstream of the durable status flip.

    Model the exact half-open #5740 boundary: admission committed ``running``,
    then PUBLISH parked before the graph could claim its pending inbound. A
    work-task cancellation at that await must still settle the same incarnation
    to ``idling``; its live host lease must not preserve a false running row.
    """
    from base.native_process.turn_identity import HostedTurnResources
    from services.agent_runner.agent_host.host import AgentHost

    agent_id = _agent(db_conn)
    announce_entered = asyncio.Event()
    announce_release = asyncio.Event()
    publish_calls = 0

    async def half_open_publish(_bus: object, published_agent_id: int) -> None:
        nonlocal publish_calls
        assert published_agent_id == agent_id
        publish_calls += 1
        if publish_calls == 1:
            announce_entered.set()
            await announce_release.wait()

    monkeypatch.setattr("agent.ownership.hosted.publish_agent_updated", half_open_publish)
    monkeypatch.setattr(
        "services.agent_runner.agent_host.host.publish_agent_updated",
        half_open_publish,
        raising=False,
    )

    def allow_model_config(
        *, model: str | None = None, catalog: ModelCatalog, llm_override: str | None
    ) -> None:
        assert model is not None
        assert catalog is model_catalog
        del llm_override

    monkeypatch.setattr(
        "services.agent_runner.agent_host.runtime.validate_model_config", allow_model_config
    )

    host = AgentHost(
        pool=aops_pool,
        control_pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine="host-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
        catalog=model_catalog,
    )
    # Exercise the owned work task itself. ``run_turn`` deliberately shields
    # this inner task from scheduler cancellation; injecting cancellation at
    # the exact inner boundary proves that boundary is independently clean.
    task = asyncio.create_task(host._run_turn(agent_id, resources=HostedTurnResources()))
    await asyncio.wait_for(announce_entered.wait(), timeout=2.0)
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("running",)

    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=1.0)
    cancellation_settled = bool(done)
    if not cancellation_settled:
        announce_release.set()
        await asyncio.gather(task, return_exceptions=True)

    assert cancellation_settled, "announcement cancellation did not finish settlement"
    assert task.cancelled()
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("idling",)
    assert publish_calls == 2, "settlement must announce after restoring durable status"
    assert agent_id not in host._in_flight


@pytest.mark.parametrize("status", ["running", "idling"])
async def test_host_refuses_a_turn_owned_by_another_live_instance(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    database: Database,
    event_bus: EventBus,
    *,
    model_catalog: ModelCatalog,
) -> None:
    from services.agent_runner.agent_host.host import AgentHost

    agent_id = _agent(db_conn)
    original = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", uuid4(), expected_from="idling", db=database
    )
    assert original is not None
    if status == "idling":
        assert await settle_hosted_runtime(aops_pool, original, bus=event_bus, resources=None)
    host = AgentHost(
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine="host-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
        catalog=model_catalog,
    )

    async def forbidden_runtime(_agent_id: int, _fingerprint: str, _model: str) -> None:
        raise AssertionError("a live other owner must prevent all runtime work")

    monkeypatch.setattr(host, "_runtime_for", forbidden_runtime)
    await host.run_turn(agent_id)
