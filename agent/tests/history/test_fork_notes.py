from __future__ import annotations

from pathlib import Path
from typing import cast

import psycopg
import pytest
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.node import claim_node
from agent.graph.claim.tests.claim_support import insert_inbound_kind, make_runtime, thread_config
from agent.state import AgentState
from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import MachineIdentitySetter
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from base.packages.extensions import install_registry
from base.paths import meiva_home, skills_dir


@pytest.fixture
def skills_unit(unit_home: Path, set_machine_identity: MachineIdentitySetter) -> None:
    """A per-test unit home, so `<home>/skills` is this test's own skill load dir,
    that still names its machine: the fork tail renders a capability index."""
    set_machine_identity(role="agent-runner", name="test-machine")


@pytest.mark.usefixtures("skills_unit")
async def test_fork_tail_grafts_delta_skills_from_inbound_payload(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """Scenario 2b: a skill the fork's config added (source never had it) rides
    the fork inbound payload and lands as a full-body note at the TAIL — after
    the fork marker and the on_fork notes, never inside the cached prefix."""
    load_dir = skills_dir(meiva_home())
    load_dir.mkdir()
    extra = load_dir / "extra"
    extra.mkdir()
    (extra / "SKILL.md").write_text(
        "---\nname: extra\ndescription: the extra skill\n---\n\nEXTRA SKILL BODY\n",
        encoding="utf-8",
    )

    def _all_enabled() -> set[str]:
        return {p.name for p in load_dir.iterdir() if p.is_dir()}

    monkeypatch.setattr(install_registry, "loadable_skill_names", _all_enabled)

    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, '', 'fork', 'agent:7', %s::jsonb)",
            (tid, '{"tail_skills": ["extra"]}'),
        )
    db_conn.commit()

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys"), HumanMessage(content="inherited tail")]),
        make_runtime(ops_pool=aops_pool, database_gate=database_gate),
        thread_config(tid),
    )
    from agent.messages.guard import guarded_add_messages

    delta = cast(list[BaseMessage], (cmd.update or {})["messages"])
    msgs = guarded_add_messages(
        [SystemMessage(content="sys"), HumanMessage(content="inherited tail")], delta
    )
    tail_note = msgs[-1]
    assert isinstance(tail_note, HumanMessage)
    assert isinstance(tail_note.content, str)  # pyright: ignore[reportUnknownMemberType]
    assert "EXTRA SKILL BODY" in tail_note.content
    assert "## meiva.skills.extra" in tail_note.content
    assert tail_note.additional_kwargs.get("meiva_note_tag") == "preloaded_skills"  # pyright: ignore[reportUnknownMemberType]
    # The graft sits AFTER the fork marker: [RemoveMessage, sys, inherited, marker, ..., delta]
    tags = [
        m.additional_kwargs.get("meiva_note_tag")  # pyright: ignore[reportUnknownMemberType]
        for m in msgs
        if isinstance(m, HumanMessage) and m.additional_kwargs.get("meiva_note_tag")  # pyright: ignore[reportUnknownMemberType]
    ]
    assert tags[0] == "lifecycle_fork"
    assert tags[-1] == "preloaded_skills"
    # Prefix untouched: sys + inherited tail keep their order at the head.
    first_two = list(msgs[0:2])
    assert [m.content for m in first_two] == ["sys", "inherited tail"]  # pyright: ignore[reportUnknownMemberType]


async def test_fork_without_payload_grafts_nothing(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """Legacy fork rows (payload NULL) keep the pre-ruling behavior: no delta
    note."""
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    insert_inbound_kind(db_conn, tid, "", "fork", source="agent:7")
    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        make_runtime(ops_pool=aops_pool, database_gate=database_gate),
        thread_config(tid),
    )
    msgs = cast(list[BaseMessage], (cmd.update or {})["messages"])
    tags = [
        m.additional_kwargs.get("meiva_note_tag")  # pyright: ignore[reportUnknownMemberType]
        for m in msgs
        if isinstance(m, HumanMessage) and m.additional_kwargs.get("meiva_note_tag")  # pyright: ignore[reportUnknownMemberType]
    ]
    assert "preloaded_skills" not in tags
