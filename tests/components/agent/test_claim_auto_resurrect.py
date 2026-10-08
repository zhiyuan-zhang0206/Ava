"""The claim node after an ops resurrect: the settled prior terminate does not swallow the chat the successor must process. Integration: it drives ops.agents.wake.resurrect_agent and the agent's hosted claim, which are peers."""

from collections.abc import Callable

import psycopg
import pytest
from langchain_core.messages import SystemMessage
from langgraph.graph import END
from psycopg_pool import AsyncConnectionPool

from agent.graph import claim_node
from agent.state import AgentState
from agent.tests.claim.claim_support import _config, _insert_inbound_kind, _make_runtime
from base.db import Database, insert_inbound_message
from base.events.live.bus import EventBus
from tests.fixtures.units import spawn_agent


@pytest.fixture
async def running_agent(aops_pool: AsyncConnectionPool, database: Database):
    """Admit a real hosted owner and bind it throughout each dispatch test."""
    from uuid import uuid4

    from agent.ownership.hosted import admit_hosted_runtime
    from base.cluster.machine import machine_name
    from base.native_process.turn_identity import bind_turn_identity

    agent_id = spawn_agent()
    incarnation = await admit_hosted_runtime(
        aops_pool, agent_id, machine_name(), uuid4(), expected_from="idling", db=database
    )
    assert incarnation is not None
    with bind_turn_identity(agent_id, incarnation=incarnation):
        yield lambda: agent_id


async def test_claim_auto_resurrect_chat_batch_wakes_and_keeps_chat(
    running_agent: Callable[[], int],
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
):
    """A settled prior command, not marker recency, protects the real successor."""

    from uuid import uuid4

    from agent.ownership.hosted import admit_hosted_runtime, apply_hosted_lifecycle
    from base.cluster.machine import machine_name
    from base.native_process.runtime_incarnation import current_incarnation
    from base.native_process.turn_identity import bind_turn_identity
    from ops.agents.wake import resurrect_agent

    tid = running_agent()
    stop = _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")
    assert (
        await claim_node(
            AgentState(messages=[SystemMessage(content="sys")]),
            _make_runtime(ops_pool=aops_pool),
            _config(
                tid,
            ),
        )
    ).goto == END

    old = current_incarnation(
        tid,
    )
    assert old is not None
    assert await apply_hosted_lifecycle(aops_pool, old, bus=event_bus) == "terminate"
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
        aops_pool, tid, machine_name(), uuid4(), expected_from="idling", db=database
    )
    assert successor is not None

    with bind_turn_identity(tid, incarnation=successor):
        cmd = await claim_node(
            AgentState(messages=[SystemMessage(content="sys")]),
            _make_runtime(ops_pool=aops_pool),
            _config(
                tid,
            ),
        )

    assert cmd.goto == "before_llm"
    assert cmd.update["halted"] is False  # type: ignore[index]
    contents = [m.content for m in cmd.update["messages"]]  # type: ignore[index]
    assert any("You have been resurrected by user" in c for c in contents)
    assert any("are you there?" in c for c in contents)  # chat not swallowed
    assert not any("Termination was accepted" in c for c in contents)
