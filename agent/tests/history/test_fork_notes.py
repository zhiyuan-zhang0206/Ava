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

from collections.abc import Callable
from pathlib import Path
from typing import cast

import psycopg
import pytest
from langchain_core.messages import BaseMessage, HumanMessage, RemoveMessage, SystemMessage
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim._dispatch import (
    _STRIP_ON_FORK_TAGS,
)
from agent.graph.claim.node import claim_node
from agent.messages import NoteTag
from agent.state import AgentState
from agent.tests.claim.claim_support import _config, _insert_inbound_kind, _make_runtime
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from base.paths import skills_dir
from tests.fixtures.units import spawn_agent


@pytest.fixture
def skills_unit(unit_home: Path, set_machine_identity: Callable[..., None]) -> None:
    """A per-test unit home, so `<home>/skills` is this test's own skill load dir,
    that still names its machine: the fork tail renders a capability index."""
    set_machine_identity("agent-runner", "test-machine")


def test_strip_tag_set_is_exactly_the_source_identity_notes() -> None:
    """The strip tags are closed over exactly the four source-identity notes —
    the cluster index must never join them (it is what the fork keeps)."""
    assert (
        frozenset(
            {
                NoteTag.AGENT_ID,
                NoteTag.AGENT_MEMORY,
                NoteTag.INHERITED_MEMORY,
                NoteTag.PRELOADED_SKILLS,
            }
        )
        == _STRIP_ON_FORK_TAGS
    )


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
    from agent.graph.claim._dispatch import _fork_rebuild_prefix
    from agent.messages.guard import guarded_add_messages
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
    from agent.graph.claim._dispatch import _fork_rebuild_prefix
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


@pytest.mark.usefixtures("skills_unit")
async def test_fork_tail_grafts_delta_skills_from_inbound_payload(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:
    """Scenario 2b: a skill the fork's config added (source never had it) rides
    the fork inbound payload and lands as a full-body note at the TAIL — after
    the fork marker and the on_fork notes, never inside the cached prefix."""
    load_dir = skills_dir()
    load_dir.mkdir()
    extra = load_dir / "extra"
    extra.mkdir()
    (extra / "SKILL.md").write_text(
        "---\nname: extra\ndescription: the extra skill\n---\n\nEXTRA SKILL BODY\n",
        encoding="utf-8",
    )

    def _all_enabled() -> set[str]:
        return {p.name for p in load_dir.iterdir() if p.is_dir()}

    monkeypatch.setattr(
        "base.packages.extensions.install_registry.loadable_skill_names", _all_enabled
    )

    tid = spawn_agent(
        catalog=model_catalog, authority=config_authority, database_gate=database_gate
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
        _make_runtime(ops_pool=aops_pool, database_gate=database_gate),
        _config(tid),
    )
    msgs = cast(list[BaseMessage], (cmd.update or {})["messages"])
    tail_note = msgs[-1]
    assert isinstance(tail_note, HumanMessage)
    assert isinstance(tail_note.content, str)  # pyright: ignore[reportUnknownMemberType]
    assert "EXTRA SKILL BODY" in tail_note.content
    assert "## ava.skills.extra" in tail_note.content
    assert tail_note.additional_kwargs.get("ava_note_tag") == "preloaded_skills"  # pyright: ignore[reportUnknownMemberType]
    # The graft sits AFTER the fork marker: [RemoveMessage, sys, inherited, marker, ..., delta]
    tags = [
        m.additional_kwargs.get("ava_note_tag")  # pyright: ignore[reportUnknownMemberType]
        for m in msgs
        if isinstance(m, HumanMessage) and m.additional_kwargs.get("ava_note_tag")  # pyright: ignore[reportUnknownMemberType]
    ]
    assert tags[0] == "lifecycle_fork"
    assert tags[-1] == "preloaded_skills"
    # Prefix untouched: sys + inherited tail keep their order at the head.
    first_two = list(msgs[1:3])
    assert [m.content for m in first_two] == ["sys", "inherited tail"]  # pyright: ignore[reportUnknownMemberType]


async def test_fork_without_payload_grafts_nothing(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:
    """Legacy fork rows (payload NULL) keep the pre-ruling behavior: no delta
    note."""
    tid = spawn_agent(
        catalog=model_catalog, authority=config_authority, database_gate=database_gate
    )
    _insert_inbound_kind(db_conn, tid, "", "fork", source="agent:7")
    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, database_gate=database_gate),
        _config(tid),
    )
    msgs = cast(list[BaseMessage], (cmd.update or {})["messages"])
    tags = [
        m.additional_kwargs.get("ava_note_tag")  # pyright: ignore[reportUnknownMemberType]
        for m in msgs
        if isinstance(m, HumanMessage) and m.additional_kwargs.get("ava_note_tag")  # pyright: ignore[reportUnknownMemberType]
    ]
    assert "preloaded_skills" not in tags
