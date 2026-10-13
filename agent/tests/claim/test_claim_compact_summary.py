"""Compact-summary claims preserve transport, publication and resume contracts."""

from unittest.mock import MagicMock

import psycopg
from langchain_core.messages import HumanMessage, SystemMessage
from psycopg_pool import AsyncConnectionPool

from agent.graph import claim_node
from agent.graph.hooks.compact import compose_summary_message
from agent.state import AgentState
from agent.tests.claim.claim_status_support import committed_publishes, compact_tail
from agent.tests.claim.claim_support import insert_inbound_kind, make_runtime, thread_config
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from tests.fixtures.unit.identity import machine_identity as machine_identity
from tests.fixtures.units import spawn_agent


async def test_claim_compact_summary_alone_does_not_publish_committed(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """compact_summary alone → does not publish InboundCommitted (it goes through state replace
    not inbound append; frontend reload should be triggered by llm_done)."""
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    insert_inbound_kind(db_conn, tid, "summary", "compact_summary")

    pub = MagicMock()
    state = AgentState(messages=[SystemMessage(content="sys")])
    await claim_node(
        state,
        make_runtime(ops_pool=aops_pool, event_publisher=pub, database_gate=database_gate),
        thread_config(
            tid,
        ),
    )
    assert committed_publishes(pub) == []


async def test_claim_compact_summary_with_no_existing_system_message(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
):
    """state.messages empty (first round) + compact_summary arrives → claim simultaneously
    injects SystemMessage into new_msgs[0] AND takes it out as sys_msg to prepend again
    (Compact path `state.messages[0] if state.messages else new_msgs.pop(0)`).

    Claim used to lay down a cold-start head and then pop it back off when a
    compaction landed on the same turn, so a missed pop duplicated the
    SystemMessage. Claim now never emits one — the head is `init_context`'s — so
    the invariant is stronger and simpler: a compaction emits the clearing
    sentinel and nothing else."""
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    summary_text = "first turn summary"
    insert_inbound_kind(db_conn, tid, summary_text, "compact_summary")

    cmd = await claim_node(
        AgentState(),  # messages empty
        make_runtime(ops_pool=aops_pool, database_gate=database_gate),
        thread_config(
            tid,
        ),
    )

    tail = compact_tail(cmd.update)
    assert not any(isinstance(m, SystemMessage) for m in cmd.update["messages"])  # type: ignore[index]
    assert not any(isinstance(m, SystemMessage) for m in tail)
    assert [m.content for m in tail] == [compose_summary_message(summary_text)]  # pyright: ignore[reportUnknownMemberType]


async def test_claim_compact_summary_returns_before_llm_with_halted_false(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
):
    """compact_summary path → returns Command(goto=before_llm, halted=False).

    Lock down mutant_168 (goto=None), mutant_170 (goto kw deleted), mutant_175-177
    (halted False → True / case change)."""
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    insert_inbound_kind(db_conn, tid, "summary text", "compact_summary")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys"), HumanMessage(content="m1")]),
        make_runtime(ops_pool=aops_pool, database_gate=database_gate),
        thread_config(
            tid,
        ),
    )

    # Compact path detours through init_context, resuming at BEFORE_LLM (default)
    assert cmd.goto == "init_context"
    assert cmd.update["context_reset"].resume == "before_llm"  # type: ignore[index]
    # halted must be False (clears halted to enter next LLM round), cannot be True / missing
    assert cmd.update["halted"] is False  # type: ignore[index]
    # update dict key is the literal 'halted', cannot be 'HALTED' / 'XXhaltedXX'
    assert "halted" in cmd.update  # type: ignore[operator]
