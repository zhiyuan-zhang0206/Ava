"""A replacement host adopts a held agent without a model and re-provisions the restart-lost relay."""

from typing import Any
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent import impersonation
from agent.ownership.hosted import admit_hosted_runtime
from base.agents import impersonation as leases
from base.agents.messages.caller_identity import CallerIdentity
from base.cluster.machine import machine_name
from base.db import create_agent
from base.events.live.bus import EventBus
from tests.impersonation_support import attested_caller, recorded_tree


@pytest.mark.parametrize("control", ["restart", "terminate", "cancel"])
async def test_replacement_host_adopts_held_agent_without_model(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    control: str,
) -> None:
    from services.agent_host.host import AgentHost

    agent_id = create_agent(db_conn)
    machine = machine_name()
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling',%s)", (agent_id, machine)
    )
    db_conn.commit()
    owner = await admit_hosted_runtime(
        aops_pool, agent_id, machine, uuid4(), expected_from="idling"
    )
    assert owner is not None
    lease = leases.request(
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
    )
    leases.accept(lease["id"], agent_id, owner, "Handoff brief")
    leases.activate(lease["id"], owner)
    # The held-controls supervision may re-provision the restart-lost bound
    # relay; a real codex relay process must never start inside the test
    # environment. The recorded controller tree is synthetic (its pids are not
    # live processes), so pin the liveness view and the fresh-start window.
    spawn = MagicMock(return_value=MagicMock(poll=MagicMock(return_value=None)))
    monkeypatch.setattr(impersonation.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        "base.agents.impersonation.provider_anchor_states", Mock(return_value=["alive"])
    )
    # A window no process age can exceed: the fresh-start carve-out is open.
    monkeypatch.setattr("base.config.settings.agent.impersonation_reprovision_window_seconds", 1e9)
    impersonation._relay_children.clear()
    graph = MagicMock()
    host = AgentHost(
        pool=aops_pool,
        checkpointer=MagicMock(),
        graph=graph,
        machine=machine,
        bus=EventBus.from_settings(),
    )
    assert agent_id in {wake.agent_id for wake in await host.pending_inbound_wakes(180)}
    # Original host has stopped renewing; admission still uses its ordinary
    # ownership fence, while the longer external decision lease remains live.
    db_conn.execute(
        "UPDATE agents_meta SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
        (agent_id,),
    )
    db_conn.commit()
    await host.run_turn(agent_id)
    assert db_conn.execute(
        "SELECT runtime_owner,status,lease_expires_at>clock_timestamp() FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone() == (host._owner, "idling", True)
    db_conn.commit()
    assert leases.require_active(lease["id"], attested_caller(lease))["status"] == "active"
    # The open lease keeps the row in the periodic pull scan: held supervision
    # must never rely on a wake being delivered (task #3998).
    assert agent_id in {wake.agent_id for wake in await host.pending_inbound_wakes(180)}
    # The replacement incarnation inherited the accepting binding (task #2635)
    # — without it the held-controls supervision could not re-provision, and
    # the spawn above would not have happened (task #2634's path, now task
    # #3998's fresh-start re-provision).
    runtime = db_conn.execute(
        "SELECT runtime_generation, runtime_owner FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone()
    db_conn.commit()
    accepted = db_conn.execute(
        "SELECT accepted_generation, accepted_owner FROM agent_impersonations WHERE id=%s",
        (lease["id"],),
    ).fetchone()
    db_conn.commit()
    assert accepted == runtime
    spawn.assert_called()
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) VALUES(%s,'',%s,'user')",
        (agent_id, control),
    )
    db_conn.commit()
    await host.run_turn(agent_id)
    expected = "expired" if control == "terminate" else "active"
    assert leases.get(lease["id"], attested_caller(lease))["status"] == expected
    if control == "cancel":
        assert db_conn.execute(
            "SELECT status FROM inbound_messages WHERE agent_id=%s AND kind='cancel'",
            (agent_id,),
        ).fetchone() == ("pending",)
        db_conn.commit()
        # Held rows stay scannable: the queued cancel is found by the DB scan
        # even with no wake delivered (task #3998 regression).
        assert agent_id in {wake.agent_id for wake in await host.pending_inbound_wakes(180)}
    if control == "restart":
        # The replacement logical incarnation also adopts without boot hooks.
        await host.run_turn(agent_id)
        assert leases.require_active(lease["id"], attested_caller(lease))["status"] == "active"
    graph.ainvoke.assert_not_called()
    impersonation._relay_children.clear()
