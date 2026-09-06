"""`fork_notes` membership and the fork strip — issue #1320.

The cluster memory index used to be `on_fork`, so a forked agent's window
carried the index twice (inherited + grafted). The registry flags now encode
the rule directly, and `_handle_fork` strips the inherited notes that name the
SOURCE (its id, its per-agent memory, its preloaded skills) before grafting the
new agent's own:

- agent id, per-agent memory, preloaded skills: `on_fork`.
- shared (cluster) memory index: NOT `on_fork` — cluster-wide content; the
  inherited copy is the same thing a graft would add (the timezone rule).

The ava_memory registrations are re-established per test (the same load path
`test_ava_memory_notes.py` uses), because other modules clear the plugin
registrations on teardown.
"""

from __future__ import annotations

import sys
from typing import Any, cast

import psycopg
import pytest
from langchain_core.messages import BaseMessage, HumanMessage, RemoveMessage, SystemMessage
from psycopg_pool import AsyncConnectionPool

from agent.graph import _context_notes
from agent.graph._claim import claim_node
from agent.graph._claim_dispatch import (
    _STRIP_ON_FORK_TAGS,
)
from agent.messages import NoteTag
from agent.state import AgentState
from shared.redis_listener import RedisInboundListener
from tests.conftest import spawn_agent

from .test_claim import _config, _insert_inbound_kind, _make_runtime  # reuse the claim harness


@pytest.fixture(autouse=True)
def memory_plugin() -> Any:
    """Load ava_memory through the real plugin-registration path (mirrors
    tests/plugins/test_ava_memory_notes.py) so the memory-note registrations
    exist regardless of what earlier modules cleared."""
    from agent.state import clear_plugin_registrations
    from shared.plugin_config_registry import bind_from_disk
    from shared.plugin_context import PluginContext

    clear_plugin_registrations()
    for name in list(sys.modules):
        if name.startswith("ava_builtins.plugins.ava_memory"):
            del sys.modules[name]

    with PluginContext("ava_memory"):
        from ava_builtins.plugins.ava_memory import plugin as _plugin

    bind_from_disk()
    yield _plugin

    clear_plugin_registrations()
    for name in list(sys.modules):
        if name.startswith("ava_builtins.plugins.ava_memory"):
            del sys.modules[name]


def _last_entry_by_name(name: str) -> _context_notes.ContextNote:
    """The last-registered entry whose builder is `name` (registration order
    wins; a plugin reload appends, never replaces)."""
    entries = [e for e in _context_notes._CONTEXT_NOTES if e.build.__name__ == name]
    assert entries, f"no context note registered as {name}"
    return entries[-1]


def test_agent_identity_notes_are_on_fork(memory_plugin: Any) -> None:
    """The fork strips these from the inherited head and re-grafts the new
    agent's own — so all three must stay `on_fork`."""
    assert _last_entry_by_name("agent_id_note").on_fork is True
    assert _last_entry_by_name("preloaded_skills_note").on_fork is True
    assert _last_entry_by_name("per_agent_memory_note").on_fork is True


def test_cluster_memory_index_is_not_on_fork(memory_plugin: Any) -> None:
    """Cluster-wide content: grafting it duplicated the index in the forked
    window (issue #1320). The inherited copy stands."""
    assert _last_entry_by_name("memory_index_note").on_fork is False


def test_strip_tag_set_is_exactly_the_source_identity_notes() -> None:
    """The strip tags are closed over exactly the three source-identity notes —
    the cluster index must never join them (it is what the fork keeps)."""
    assert (
        frozenset({NoteTag.AGENT_ID, NoteTag.AGENT_MEMORY, NoteTag.PRELOADED_SKILLS})
        == _STRIP_ON_FORK_TAGS
    )


async def test_fork_end_to_end_single_copy_each_note(
    memory_plugin: Any,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
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
        _tagged(NoteTag.PRELOADED_SKILLS, "source's preloaded skills", "note-old-preload"),
        _tagged(NoteTag.MEMORY, "shared pool index", "note-cluster-index"),
    ]

    cmd = await claim_node(
        AgentState(messages=list(inherited)),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
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
    assert {"note-old-id", "note-old-mem", "note-old-preload"} & rebuilt_ids == set()
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


def test_fork_rebuild_passes_the_messages_guard() -> None:
    """The full-wipe rebuild is the one deletion shape the append-only
    messages guard sanctions (task #1256): survivors keep content + relative
    order; the dropped source-identity notes are gone; the grafted notes land
    as new messages. This pins that the reducer accepts the exact shape
    `_fork_rebuild_prefix` produces."""
    from agent.graph._claim_dispatch import _fork_rebuild_prefix
    from agent.messages_guard import guarded_add_messages
    from agent.state import AgentState

    sys_msg = SystemMessage(content="sys", id="m-sys")
    conversation = HumanMessage(content="hello", id="m-chat")
    before = [
        sys_msg,
        _fake_note(NoteTag.AGENT_ID, "old agent id", "note-old-id"),
        _fake_note(NoteTag.AGENT_MEMORY, "source's memory", "note-old-mem"),
        _fake_note(NoteTag.MEMORY, "shared pool index", "note-cluster-index"),
        conversation,
    ]
    state = AgentState(messages=list(before))
    delta: list[object] = [
        *_fork_rebuild_prefix(state),
        _fake_note(NoteTag.LIFECYCLE_FORK, "marker", "m-marker"),
    ]
    after = guarded_add_messages(before, delta)
    ids = [m.id for m in after]
    assert ids == ["m-sys", "note-cluster-index", "m-chat", "m-marker"]


def test_fork_rebuild_dropping_a_conversation_message_is_rejected() -> None:
    """The guard cannot tell good drops from bad ones, so the strip set is
    the protection — but the rebuild class itself must still reject nothing
    the caller does not explicitly drop. This pins the rebuild shape (not the
    tag policy): dropping a conversation message from the rebuild re-listing
    is invisible to the guard, so the tag filter above is load-bearing."""
    from agent.graph._claim_dispatch import _fork_rebuild_prefix
    from agent.state import AgentState

    conversation = HumanMessage(content="hello", id="m-chat")
    state = AgentState(messages=[SystemMessage(content="sys", id="m-sys"), conversation])
    rebuilt = _fork_rebuild_prefix(state)
    # The prefix only ever drops _STRIP_ON_FORK_TAGS notes — conversation
    # messages and unmarked messages are always re-listed.
    re_listed = [
        m for m in rebuilt if isinstance(m, BaseMessage) and not isinstance(m, RemoveMessage)
    ]
    assert {m.id for m in re_listed} == {"m-sys", "m-chat"}
