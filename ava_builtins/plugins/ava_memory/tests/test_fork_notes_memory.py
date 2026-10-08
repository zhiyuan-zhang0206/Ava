"""The ava_memory notes on a fork: identity notes are on_fork, the cluster index is not, and a fork rebuild keeps a single copy of each note and the prefix bytes."""

from __future__ import annotations

from typing import Any, cast

import psycopg
import pytest
from langchain_core.messages import (
    AnyMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.node import claim_node
from agent.graph.prompt.context_notes import FRAMEWORK_NOTES
from agent.messages import NoteTag
from agent.state import AgentState
from agent.tests.claim.claim_support import _config, _insert_inbound_kind, _make_runtime
from base.packages.plugins.extensions import ContextNote, ExtensionRegistry
from tests.fixtures.units import spawn_agent
from tests.path_scoped.agent_tests import _fresh_snapshot_cursor as _fresh_snapshot_cursor
from tests.path_scoped.agent_tests import (
    _fresh_unresolved_skill_warnings as _fresh_unresolved_skill_warnings,
)


@pytest.fixture(autouse=True)
def memory_plugin() -> Any:
    """The ava_memory agent-runtime face; its `contribute()` declares the memory
    notes (mirrors ava_builtins/plugins/ava_memory/tests/test_ava_memory_notes.py)."""
    from ava_builtins.plugins.ava_memory import agent_runtime as _plugin

    return _plugin


def _registry(memory_plugin: Any) -> ExtensionRegistry:
    """The registry the loader would build with only this plugin enabled."""
    return ExtensionRegistry((("ava_memory", memory_plugin.contribute()),))


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
    """The fork strips these from the inherited head and re-grafts the new
    agent's own — so all three must stay `on_fork`."""
    assert _entry_by_name(memory_plugin, "agent_id_note").on_fork is True
    assert _entry_by_name(memory_plugin, "preloaded_skills_note").on_fork is True
    assert _entry_by_name(memory_plugin, "per_agent_memory_note").on_fork is True


def test_cluster_memory_index_is_not_on_fork(memory_plugin: Any) -> None:
    """Cluster-wide content: grafting it duplicated the index in the forked
    window (issue #1320). The inherited copy stands."""
    assert _entry_by_name(memory_plugin, "memory_index_note").on_fork is False


async def test_fork_end_to_end_single_copy_each_note(
    memory_plugin: Any,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    """The full fork claim with the real registry: the inherited head carries a
    source-id note, a source-memory note, a source-preloads note and the
    cluster index; after the claim exactly one of each of the first three
    remains (the grafted, new-agent copies), and the cluster index survives
    exactly once — no second copy grafted."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "fork", source="agent:7")

    def _tagged(tag: NoteTag, content: str, id: str) -> HumanMessage:
        return HumanMessage(
            content=f"[system] {content}",
            id=id,
            additional_kwargs={"ava_msg_type": "system_note", "ava_note_tag": tag.value},
        )

    inherited = [
        SystemMessage(content="sys"),
        _tagged(NoteTag.AGENT_ID, "old agent id", "note-old-id"),
        _tagged(NoteTag.AGENT_MEMORY, "source's memory", "note-old-mem"),
        _tagged(NoteTag.INHERITED_MEMORY, "source's inherited memory", "note-old-inherit"),
        _tagged(NoteTag.PRELOADED_SKILLS, "source's preloaded skills", "note-old-preload"),
        _tagged(NoteTag.MEMORY, "shared pool index", "note-cluster-index"),
    ]

    cmd = await claim_node(
        AgentState(messages=list(inherited)),
        _make_runtime(ops_pool=aops_pool, extensions=_registry(memory_plugin)),
        _config(tid),
    )

    assert cmd.goto == "before_llm"
    update = cast(dict[str, object], cmd.update or {})
    msgs = cast(list[BaseMessage], update["messages"])
    # Full-wipe rebuild: one RemoveMessage(__remove_all__) at the head, the
    # inherited history re-listed with the three source-identity notes dropped,
    # then the grafted sequence.
    from langgraph.graph.message import REMOVE_ALL_MESSAGES

    assert isinstance(msgs[0], RemoveMessage) and msgs[0].id == REMOVE_ALL_MESSAGES
    rebuilt_ids = {m.id for m in msgs[1:] if not isinstance(m, RemoveMessage)}
    assert {
        "note-old-id",
        "note-old-mem",
        "note-old-inherit",
        "note-old-preload",
    } & rebuilt_ids == set()
    assert "note-cluster-index" in rebuilt_ids
    # Grafted sequence: fork marker, new agent id, new per-agent memory (the
    # preloaded-skills builder is empty in this env, and the cluster index is
    # not on_fork — so nothing else).
    tags = [
        m.additional_kwargs.get("ava_note_tag")  # pyright: ignore[reportUnknownMemberType]
        for m in msgs[-3:]
    ]
    assert tags == ["lifecycle_fork", "agent_id", "agent_memory"]


def _fake_note(tag: NoteTag, content: str, id: str) -> HumanMessage:
    return HumanMessage(
        content=f"[system] {content}",
        id=id,
        additional_kwargs={"ava_msg_type": "system_note", "ava_note_tag": tag.value},
    )


async def test_fork_rebuild_preserves_prefix_bytes_until_first_stripped_note(
    memory_plugin: Any,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    """The cache contract (task #2694): everything in front of the first
    source-identity note survives the fork rebuild byte-identical — same
    content, same order — so the provider's prefix cache stays valid. The
    source-identity notes drop, and the conversation tail follows the grafted
    sequence."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "fork", source="agent:7")
    inherited: list[AnyMessage] = [
        SystemMessage(content="sys"),
        _fake_note(NoteTag.MEMORY, "shared pool index (kept)", "note-cluster"),
        _fake_note(NoteTag.AGENT_ID, "old agent id", "note-old-id"),
        _fake_note(NoteTag.AGENT_MEMORY, "source's memory", "note-old-mem"),
        _fake_note(NoteTag.PRELOADED_SKILLS, "source's preloaded skills", "note-old-preload"),
        HumanMessage(content="conversation tail"),
    ]
    cmd = await claim_node(
        AgentState(messages=list(inherited)),
        _make_runtime(ops_pool=aops_pool, extensions=_registry(memory_plugin)),
        _config(tid),
    )
    msgs = cast(list[BaseMessage], (cmd.update or {})["messages"])
    assert isinstance(msgs[0], RemoveMessage)
    survivors = [m for m in msgs[1:] if not isinstance(m, RemoveMessage)]
    # Byte-identical prefix: the system prompt and the kept cluster note are
    # the exact source messages, in order — nothing re-rendered in front of
    # the first dropped note.
    assert [m.content for m in survivors[:2]] == [  # pyright: ignore[reportUnknownMemberType]
        "sys",
        "[system] shared pool index (kept)",
    ]
    assert [m.id for m in survivors[:2]] == [inherited[0].id, inherited[1].id]
    # The conversation tail survives — positioned right after the kept notes
    # and before the grafted fork-marker sequence.
    assert len(survivors) == 6
    assert survivors[2].content == "conversation tail"  # pyright: ignore[reportUnknownMemberType]
    grafted_tags = [
        m.additional_kwargs.get("ava_note_tag")  # pyright: ignore[reportUnknownMemberType]
        for m in survivors[3:]
    ]
    assert grafted_tags[0] == "lifecycle_fork"
    assert set(grafted_tags[1:]) == {"agent_id", "agent_memory"}  # pyright: ignore[reportUnknownArgumentType]
