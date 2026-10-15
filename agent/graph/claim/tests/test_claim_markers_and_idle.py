from unittest.mock import MagicMock

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.tests.claim_support import make_runtime, thread_config
from agent.graph.claim.tests.cursor_fixture import fresh_snapshot_cursor as fresh_snapshot_cursor
from agent.state import AgentState
from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog


def test_by_who_self_returns_yourself():
    from agent.graph.claim.node import by_who

    assert by_who("self") == "yourself"


def test_by_who_uppercase_self_passthrough():
    from agent.graph.claim.node import by_who

    assert by_who("SELF") == "SELF"
    assert by_who("Self") == "Self"
    assert by_who("SELF:UPDATE") == "SELF:UPDATE"


def test_by_who_external_source_passthrough():
    from agent.graph.claim.node import by_who

    assert by_who("user") == "user"
    assert by_who("user") == "user"
    assert by_who("agent:42") == "agent:42"
    assert by_who("system") == "system"


def test_by_who_self_prefix_does_not_match_self():
    from agent.graph.claim.node import by_who

    assert by_who("selfish") == "selfish"
    assert by_who("self_other") == "self_other"
    assert by_who("self:other") == "self:other"


def test_render_restart_completed_marker_system_update_no_by_clause():
    from agent.graph.claim.node import render_restart_completed_marker

    text = render_restart_completed_marker("system:update", timestamp_prefix=lambda: "")
    assert "updated and restarted" in text
    assert "by " not in text


def test_render_restart_completed_marker_plain_self_unchanged():
    from agent.graph.claim.node import render_restart_completed_marker

    text = render_restart_completed_marker("self", timestamp_prefix=lambda: "")
    assert "restarted by yourself" in text
    assert "updated" not in text


def test_render_restart_completed_marker_external_source_unchanged():
    from agent.graph.claim.node import render_restart_completed_marker

    text = render_restart_completed_marker("user", timestamp_prefix=lambda: "")
    assert "restarted by user" in text
    assert "updated" not in text


async def test_claim_node_idle_enter_publishes_full_window_snapshot(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
):
    import json

    from langchain_core.messages import SystemMessage

    from agent.graph.claim.node import claim_node

    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )

    pub = MagicMock()
    state = AgentState()
    state.messages = [SystemMessage(content="prompt")]
    await claim_node(
        state,
        make_runtime(ops_pool=aops_pool, event_publisher=pub, database_gate=database_gate),
        thread_config(
            tid,
        ),
    )
    snaps = [
        json.loads(c.args[0]) for c in pub.emit.call_args_list if "timeline_snapshot" in c.args[0]
    ]
    assert len(snaps) == 1
    assert snaps[0]["msg_count"] == 1
    assert [it["item_id"] for it in snaps[0]["items"]] == ["0.0"]
    assert snaps[0]["items"][0]["kind"] == "system_prompt"


def test_claim_will_idle_shares_the_impl_and_wrapper_contract() -> None:
    from datetime import UTC, datetime

    from langchain_core.messages import HumanMessage, SystemMessage

    from agent.graph.claim.node import claim_will_idle
    from agent.messages import system_note_message
    from agent.state_channels import CIRCUIT_REASON_BILLING, CircuitState
    from base.agents.messages.kwargs import NoteTag

    fresh = AgentState()
    fresh.messages = [SystemMessage(content="prompt")]
    assert claim_will_idle(fresh)

    resume = AgentState()
    resume.impersonation_handoff_id = "7:0"
    note = system_note_message(
        content="session ended", tag=NoteTag.IMPERSONATION, created_at=datetime.now(UTC)
    )
    note.id = "impersonation-handoff:7:0"
    resume.messages = [SystemMessage(content="prompt"), note]
    assert not claim_will_idle(resume)

    resume.halted = True
    assert claim_will_idle(resume)

    parked = AgentState()
    parked.messages = [HumanMessage(content="hello")]
    parked.circuit = CircuitState(open=True, reason=CIRCUIT_REASON_BILLING)
    assert claim_will_idle(parked)
