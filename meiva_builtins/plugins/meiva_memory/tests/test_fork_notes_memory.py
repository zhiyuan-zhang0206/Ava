"""The meiva_memory notes on a fork: identity notes are on_fork, the cluster index is not, and a fork rebuild keeps a single copy of each note and the prefix bytes."""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from typing import Any, cast

import psycopg
import pytest
from langchain_core.messages import (
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.node import claim_node
from agent.graph.claim.tests.claim_support import insert_inbound_kind, make_runtime, thread_config
from agent.graph.claim.tests.cursor_fixture import fresh_snapshot_cursor as fresh_snapshot_cursor
from agent.graph.prompt.context_notes import FRAMEWORK_NOTES
from agent.messages import NoteTag
from agent.state import AgentState
from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from base.packages.plugins.extensions import ContextNote, ExtensionRegistry
from meiva.boot import process_clients


@pytest.fixture(autouse=True)
def memory_plugin() -> Any:
    """The meiva_memory agent-runtime face; its `contribute()` declares the memory
    notes (mirrors meiva_builtins/plugins/meiva_memory/tests/test_meiva_memory_notes.py)."""
    from meiva_builtins.plugins.meiva_memory import agent_runtime as _plugin

    return _plugin


def _registry(memory_plugin: Any) -> ExtensionRegistry:
    """The registry the loader would build with only this plugin enabled."""
    return ExtensionRegistry((("meiva_memory", memory_plugin.contribute()),))


def _entry_by_name(memory_plugin: Any, name: str) -> ContextNote:
    """The entry whose builder is `name`, among the framework's notes and the plugin's."""
    entries = [
        e
        for e in (*FRAMEWORK_NOTES, *(n for _p, n in _registry(memory_plugin).context_notes()))
        if e.build.__name__ == name
    ]
    assert entries, f"no context note declared as {name}"
    return entries[-1]


def test_agent_identity_notes_are_on_fork(memory_plugin: Any) -> None:
    assert _entry_by_name(memory_plugin, "agent_id_note").on_fork is True
    assert _entry_by_name(memory_plugin, "preloaded_skills_note").on_fork is True
    assert _entry_by_name(memory_plugin, "per_agent_memory_note").on_fork is True


def test_cluster_memory_index_is_not_on_fork(memory_plugin: Any) -> None:
    assert _entry_by_name(memory_plugin, "memory_index_note").on_fork is False


async def test_fork_end_to_end_keeps_source_notes_and_grafts_own(
    redis_url: str,
    memory_plugin: Any,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    insert_inbound_kind(db_conn, tid, "", "fork", source="agent:7")

    def _tagged(tag: NoteTag, content: str, id: str) -> HumanMessage:
        return HumanMessage(
            content=f"[system] {content}",
            id=id,
            additional_kwargs={"meiva_msg_type": "system_note", "meiva_note_tag": tag.value},
        )

    inherited = [
        SystemMessage(content="sys"),
        _tagged(NoteTag.AGENT_ID, "old agent id", "note-old-id"),
        _tagged(NoteTag.AGENT_MEMORY, "source's memory", "note-old-mem"),
        _tagged(NoteTag.INHERITED_MEMORY, "source's inherited memory", "note-old-inherit"),
        _tagged(NoteTag.PRELOADED_SKILLS, "source's preloaded skills", "note-old-preload"),
        _tagged(NoteTag.MEMORY, "shared pool index", "note-cluster-index"),
    ]

    with closing(
        process_clients(database=lambda: Database.from_settings(gate=database_gate))
    ) as clients:
        runtime = make_runtime(
            ops_pool=aops_pool,
            extensions=_registry(memory_plugin),
            agent_id=tid,
            database_gate=database_gate,
        )
        cmd = await claim_node(
            AgentState(messages=list(inherited)),
            Runtime(context=replace(runtime.context, clients=clients)),
            thread_config(tid),
        )

    assert cmd.goto == "before_llm"
    update = cast(dict[str, object], cmd.update or {})
    from agent.messages.guard import guarded_add_messages

    delta = cast(list[BaseMessage], update["messages"])
    msgs = guarded_add_messages(list(inherited), delta)
    assert [m.id for m in msgs[: len(inherited)]] == [m.id for m in inherited]
    assert [m.content for m in msgs[: len(inherited)]] == [m.content for m in inherited]
    tags = [m.additional_kwargs.get("meiva_note_tag") for m in msgs[len(inherited) :]]
    assert tags == ["lifecycle_fork", "agent_id", "agent_memory"]


def _fake_note(tag: NoteTag, content: str, id: str) -> HumanMessage:
    return HumanMessage(
        content=f"[system] {content}",
        id=id,
        additional_kwargs={"meiva_msg_type": "system_note", "meiva_note_tag": tag.value},
    )


async def test_fork_keeps_the_whole_inherited_prefix(
    redis_url: str,
    memory_plugin: Any,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    insert_inbound_kind(db_conn, tid, "", "fork", source="agent:7")
    inherited: list[AnyMessage] = [
        SystemMessage(content="sys"),
        _fake_note(NoteTag.MEMORY, "shared pool index (kept)", "note-cluster"),
        _fake_note(NoteTag.AGENT_ID, "old agent id", "note-old-id"),
        _fake_note(NoteTag.AGENT_MEMORY, "source's memory", "note-old-mem"),
        _fake_note(NoteTag.PRELOADED_SKILLS, "source's preloaded skills", "note-old-preload"),
        HumanMessage(content="conversation tail"),
    ]
    with closing(
        process_clients(database=lambda: Database.from_settings(gate=database_gate))
    ) as clients:
        runtime = make_runtime(
            ops_pool=aops_pool,
            extensions=_registry(memory_plugin),
            agent_id=tid,
            database_gate=database_gate,
        )
        cmd = await claim_node(
            AgentState(messages=list(inherited)),
            Runtime(context=replace(runtime.context, clients=clients)),
            thread_config(tid),
        )
    from agent.messages.guard import guarded_add_messages

    delta = cast(list[BaseMessage], (cmd.update or {})["messages"])
    msgs = guarded_add_messages(list(inherited), delta)
    assert [m.id for m in msgs[: len(inherited)]] == [m.id for m in inherited]
    assert [m.content for m in msgs[: len(inherited)]] == [m.content for m in inherited]
    grafted_tags = [m.additional_kwargs.get("meiva_note_tag") for m in msgs[len(inherited) :]]
    assert grafted_tags[0] == "lifecycle_fork"
    assert set(grafted_tags[1:]) == {"agent_id", "agent_memory"}
