"""The claim node after an ops resurrect: the settled prior terminate does not swallow the chat the successor must process. Integration: it drives ops.agents.wake.resurrect_agent and the agent's hosted claim, which are peers."""

from collections.abc import Callable
from dataclasses import replace

import psycopg
import pytest
from langchain_core.messages import SystemMessage
from langgraph.graph import END
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

import ava
from agent.graph import claim_node
from agent.state import AgentState
from agent.tests.claim.claim_support import _config, _insert_inbound_kind, _make_runtime
from base.config.service_read import ConfigAuthority
from base.db import Database, insert_inbound_message
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from tests.fixtures.units import spawn_agent


@pytest.fixture
async def running_agent(
    aops_pool: AsyncConnectionPool,
    database: Database,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
):
    """Admit a real hosted owner and bind it throughout each dispatch test."""
    from uuid import uuid4

    from agent.ownership.hosted import admit_hosted_runtime
    from base.cluster.machine import machine_name

    agent_id = spawn_agent(
        catalog=model_catalog, authority=config_authority, database_gate=database_gate
    )
    incarnation = await admit_hosted_runtime(
        aops_pool,
        agent_id,
        machine_name(),
        uuid4(),
        expected_from="idling",
        db=database,
    )
    assert incarnation is not None
    ava.context = replace(ava.context, original_incarnation=incarnation)
    yield lambda: agent_id


async def test_claim_auto_resurrect_chat_batch_wakes_and_keeps_chat(
    running_agent: Callable[[], int],
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
):
    """A settled prior command, not marker recency, protects the real successor."""

    from uuid import uuid4

    from agent.ownership.hosted import admit_hosted_runtime, apply_hosted_lifecycle
    from base.cluster.machine import machine_name
    from ops.agents.wake import resurrect_agent

    tid = running_agent()
    stop = _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")
    assert (
        await claim_node(
            AgentState(messages=[SystemMessage(content="sys")]),
            _make_runtime(ops_pool=aops_pool, database_gate=database_gate),
            _config(
                tid,
            ),
        )
    ).goto == END

    old = ava.context.original_incarnation
    assert old is not None
    assert (
        await apply_hosted_lifecycle(aops_pool, old, bus=event_bus, resources=None) == "terminate"
    )
    assert db_conn.execute(
        "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (stop,)
    ).fetchone() == ("done", True)
    db_conn.commit()
    insert_inbound_message(
        db_conn, tid, "are you there?", source="user", bus=event_bus, database=database
    )
    resurrect_agent(database, event_bus, tid, resurrected_by="user")
    launch = db_conn.execute(
        "SELECT id FROM inbound_messages WHERE agent_id=%s AND kind='resurrect' "
        "AND status='pending'",
        (tid,),
    ).fetchone()
    assert launch is not None
    db_conn.commit()
    successor = await admit_hosted_runtime(
        aops_pool,
        tid,
        machine_name(),
        uuid4(),
        expected_from="idling",
        db=database,
    )
    assert successor is not None

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        Runtime(
            context=replace(
                _make_runtime(ops_pool=aops_pool, database_gate=database_gate).context,
                original_incarnation=successor,
            )
        ),
        _config(tid),
    )

    assert cmd.goto == "before_llm"
    assert cmd.update["halted"] is False  # type: ignore[index]
    contents = [m.content for m in cmd.update["messages"]]  # type: ignore[index]
    assert any("You have been resurrected by user" in c for c in contents)
    assert any("are you there?" in c for c in contents)  # chat not swallowed
    assert not any("Termination was accepted" in c for c in contents)
