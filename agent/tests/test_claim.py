"""claim node unit tests.

claim node is the core dispatcher of the newly designed 6-Node topology — it decides
state updates and routing based on inbound kind. These tests cover each kind case, using
a real DB (adb_conn fixture) + mock LLM.

Coverage includes:
- dispatch of various kinds (chat / compact_summary / compact_request /
  terminate / restart / restart_completed / resurrect / unknown)
- short-path: first SELECT already has inbound, does not enter wait branch
- long-await: first SELECT empty, agents.status switched 'idling' → wait → INSERT
  wake up → take batch → switch back 'running'

Not tested:
- Redis pub/sub wake integration for ops_db AsyncConnection (tested in test_db.py)
- auto-compact behavior (now a before_llm hook, tests in agent/tests/test_compact.py)
"""

from collections.abc import Callable
from typing import cast

import psycopg
import pytest
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.messages.modifier import RemoveMessage
from langgraph.graph import END
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from psycopg_pool import AsyncConnectionPool

from agent.graph import claim_node
from agent.hooks.compact import compose_summary_message
from agent.messages import NoteTag, system_note_message
from agent.state import AgentState
from agent.tests.claim_status_support import _compact_tail
from agent.tests.claim_status_support import running_agent as running_agent
from agent.tests.claim_support import _config, _fake_llm, _insert_inbound_kind, _make_runtime
from base.host.env.agent_slices import AgentSlices
from tests.fixtures.units import spawn_agent

# Almost all claim tests are short-path dispatch: the inbound is INSERTed before
# claim_node runs, so its first SELECT gets the batch — pure DB side-effect +
# routing assertions, deterministic and parallel-safe (each xdist worker has its
# own throwaway DB). Only the handful that park in the real wait_for_inbound /
# _wait_for_batch loop and depend on a real Redis pub/sub wake keep
# `@pytest.mark.flaky` to run serial.


# ────────────────────────────────────────────────────────────────────────
# Memory index injection — the standing MEMORY.md pointer is put in front of
# the agent at the two context-(re)establishment moments: cold start (first
# human message) and right after a compact summary. Persists in between;
# re-injected after compact because REMOVE_ALL wipes the prior copy.


async def test_claim_resurrect_kind_appends_marker_and_continues(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """resurrect inbound (delivered to the new process by resurrect_agent) → claim appends
    lifecycle marker 'You have been resurrected by {source}' + goto BEFORE_LLM."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "resurrect", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == "before_llm"
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    lifecycle = msgs[0]
    assert isinstance(lifecycle, HumanMessage)
    assert "You have been resurrected by user" in lifecycle.content  # pyright: ignore[reportUnknownMemberType]
    assert lifecycle.additional_kwargs.get("ava_msg_type") == "system_note"  # pyright: ignore[reportUnknownMemberType]
    assert lifecycle.additional_kwargs.get("ava_note_tag") == "lifecycle_resurrect"  # pyright: ignore[reportUnknownMemberType]


async def test_claim_resurrect_batch_appends_only_latest_marker(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """Repeated failed recoveries are consumed together but render one marker."""
    tid = spawn_agent()
    first = _insert_inbound_kind(db_conn, tid, "", "resurrect", source="system:retry")
    latest = _insert_inbound_kind(db_conn, tid, "", "resurrect", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    msgs = cmd.update["messages"]  # type: ignore[index]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert "You have been resurrected by user" in msgs[0].content  # pyright: ignore[reportUnknownMemberType]
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id, status FROM inbound_messages WHERE id = ANY(%s)", ([first, latest],)
        )
        assert dict(cur.fetchall()) == {first: "done", latest: "done"}


# ────────────────────────────────────────────────────────────────────────
# Lifecycle commands bind to an admitted incarnation and dispatch serially.
# A notification's recency cannot stand in for completed termination or actual
# successor admission. Unaccepted work survives for the correct consumer.


async def test_unowned_resurrect_notification_cannot_cancel_pending_terminate(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """A newer notification is not admission or proof that prior intent completed."""
    tid = spawn_agent()
    # id == insertion order: terminate older than the resurrect that follows it
    _insert_inbound_kind(db_conn, tid, "", "cancel", source="user")
    _insert_inbound_kind(db_conn, tid, "", "cancel", source="user")
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")
    _insert_inbound_kind(db_conn, tid, "", "resurrect", source="user")

    with pytest.raises(RuntimeError, match="lifecycle claim requires an admitted"):
        await claim_node(
            AgentState(messages=[SystemMessage(content="sys")]),
            _make_runtime(ops_pool=aops_pool),
            _config(
                tid,
            ),
        )
    assert db_conn.execute(
        "SELECT kind,status,claimed_at FROM inbound_messages WHERE agent_id=%s ORDER BY id", (tid,)
    ).fetchall() == [
        (kind, "pending", None) for kind in ["cancel", "cancel", "terminate", "resurrect"]
    ]


async def test_claim_resurrect_then_terminate_still_dies(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """An owned terminate dispatches alone; an older notification is not lost."""
    tid = running_agent()
    notice = _insert_inbound_kind(db_conn, tid, "", "resurrect", source="user")
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == END
    contents = [m.content for m in cmd.update["messages"]]  # type: ignore[index]
    assert any("Termination was accepted from user" in c for c in contents)
    assert not any("resurrected" in c for c in contents)
    assert db_conn.execute(
        "SELECT status,claimed_at FROM inbound_messages WHERE id=%s", (notice,)
    ).fetchone() == ("pending", None)


async def test_claim_auto_resurrect_compact_request_batch_compacts_and_wakes(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """Auto-resurrect-on-compact path: a /compact delivered to a terminated agent
    inserts the compact_request then a resurrect (newer id). The resurrect wins the
    recency routing so the agent wakes; the compact_request is NOT an exit loser
    (exit_kind is None), so it still runs — the history is compacted and the
    resurrect marker is appended after the summary. Without auto-resurrect the
    compact_request would sit pending with no live process to claim it."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "compact_request", source="user")
    _insert_inbound_kind(db_conn, tid, "", "resurrect", source="user")

    state = AgentState(
        messages=[
            SystemMessage(content="<test sys prompt>"),
            *(HumanMessage(content=f"history-{i}") for i in range(10)),
        ]
    )

    fake_llm = _fake_llm("LLM-generated summary")
    cmd = await claim_node(
        state,
        _make_runtime(ops_pool=aops_pool, llm=fake_llm),
        _config(
            tid,
        ),
    )

    assert cmd.goto == "init_context"
    assert cmd.update["context_reset"].resume == "before_llm"  # type: ignore[index]
    fake_llm.bind_tools.return_value.ainvoke.assert_called_once()  # compact ran
    msgs = _compact_tail(cmd.update)
    assert msgs[0].content == compose_summary_message("LLM-generated summary")  # pyright: ignore[reportUnknownMemberType]
    # resurrect marker appended after the summary — the revive still wakes the agent
    contents = [m.content for m in msgs if isinstance(m.content, str)]  # pyright: ignore[reportUnknownMemberType]
    assert any("You have been resurrected by user" in c for c in contents)


async def test_claim_fork_kind_appends_identity_marker_and_continues(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """fork inbound (delivered to the new process by spawn_agent on fork) → claim appends
    identity marker: contains the fork source from source (agent:M) + new agent's own id (N) +
    'inherited' wording + goto BEFORE_LLM. Simulates forked checkpoint: messages non-empty
    (inherited history from source agent), so no SystemMessage injection; marker appended at
    the end, then the `on_fork` notes (fork_notes stubbed here — its membership is pinned in
    test_fork_notes.py, issue #1320)."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "fork", source="agent:7")
    monkeypatch = pytest.MonkeyPatch()

    def fork_notes(_slices: AgentSlices) -> list[HumanMessage]:
        return [system_note_message(content="Your Agent ID is N.", tag=NoteTag.AGENT_ID)]

    monkeypatch.setattr("agent.graph.claim._dispatch.fork_notes", fork_notes)
    try:
        cmd = await claim_node(
            AgentState(messages=[SystemMessage(content="sys"), HumanMessage(content="inherited")]),
            _make_runtime(ops_pool=aops_pool),
            _config(
                tid,
            ),
        )
    finally:
        monkeypatch.undo()

    assert cmd.goto == "before_llm"
    msgs = cmd.update["messages"]  # type: ignore[index]
    # messages non-empty → no SystemMessage injection; the fork rebuilds the
    # head (full wipe + inherited history re-listed) and appends marker + notes.
    # Shape: [RemoveMessage(__remove_all__), sys, inherited, marker, agent_id].
    assert len(msgs) == 5  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(msgs[0], RemoveMessage) and msgs[0].id == REMOVE_ALL_MESSAGES  # pyright: ignore[reportUnknownMemberType]
    assert [m.additional_kwargs.get("ava_note_tag") for m in msgs[-2:]] == [  # pyright: ignore[reportUnknownMemberType]
        "lifecycle_fork",
        "agent_id",
    ]
    lifecycle = msgs[3]
    assert isinstance(lifecycle, HumanMessage)
    assert isinstance(lifecycle.content, str)  # pyright: ignore[reportUnknownMemberType]
    content = lifecycle.content
    # fork source (M=7, from source) + new agent's own id (N=tid, from config)
    assert "forked from agent:7" in content
    assert f"id {tid}" in content
    assert "inherited from agent:7" in content
    assert lifecycle.additional_kwargs.get("ava_msg_type") == "system_note"  # pyright: ignore[reportUnknownMemberType]
    assert lifecycle.additional_kwargs.get("ava_note_tag") == "lifecycle_fork"  # pyright: ignore[reportUnknownMemberType]


async def test_claim_fork_strips_inherited_source_notes(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """The fork strip (issue #1320): inherited head notes that name the SOURCE —
    its agent id, its per-agent memory, its preloaded skills — are removed, and
    the new agent's own copies are grafted. The cluster memory index is
    cluster-wide: the inherited copy is kept and NOT re-grafted."""
    from langchain_core.messages import RemoveMessage

    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "fork", source="agent:7")

    def _tagged(tag: NoteTag, content: str, id: str) -> HumanMessage:
        return HumanMessage(
            content=f"[system] {content}",
            id=id,
            additional_kwargs={"ava_msg_type": "system_note", "ava_note_tag": tag.value},
        )

    old_id = _tagged(NoteTag.AGENT_ID, "old agent id", "note-old-id")
    old_mem = _tagged(NoteTag.AGENT_MEMORY, "source's memory", "note-old-mem")
    old_preload = _tagged(NoteTag.PRELOADED_SKILLS, "source's preloaded skills", "note-old-preload")
    cluster_index = _tagged(NoteTag.MEMORY, "shared pool index", "note-cluster-index")
    inherited = [SystemMessage(content="sys"), old_id, old_mem, old_preload, cluster_index]
    monkeypatch = pytest.MonkeyPatch()

    def fork_notes(_slices: AgentSlices) -> list[HumanMessage]:
        return [
            system_note_message(content="Your Agent ID is N.", tag=NoteTag.AGENT_ID),
            system_note_message(content="the new agent's memory", tag=NoteTag.AGENT_MEMORY),
        ]

    monkeypatch.setattr("agent.graph.claim._dispatch.fork_notes", fork_notes)
    try:
        cmd = await claim_node(
            AgentState(messages=list(inherited)),
            _make_runtime(ops_pool=aops_pool),
            _config(
                tid,
            ),
        )
    finally:
        monkeypatch.undo()

    assert cmd.goto == "before_llm"
    msgs = cast(list[BaseMessage], (cmd.update or {})["messages"])
    # The rebuild: one full-wipe marker, then the inherited history re-listed
    # with the three source-identity notes dropped (the cluster index — the
    # SYSTEM_NOTE not owned by the source — survives the rebuild).
    assert isinstance(msgs[0], RemoveMessage) and msgs[0].id == REMOVE_ALL_MESSAGES
    rebuilt_ids = {m.id for m in msgs[1:] if not isinstance(m, RemoveMessage)}
    assert {old_id.id, old_mem.id, old_preload.id} & rebuilt_ids == set()
    assert cluster_index.id in rebuilt_ids
    # Grafted after the rebuild: fork marker + the new agent's own id + its
    # own per-agent memory.
    tail = [m for m in msgs if not isinstance(m, RemoveMessage)][-3:]
    assert [m.additional_kwargs.get("ava_note_tag") for m in tail] == [  # pyright: ignore[reportUnknownMemberType]
        "lifecycle_fork",
        "agent_id",
        "agent_memory",
    ]
    grafted_content: object = tail[-1].content  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(grafted_content, str) and "source's memory" not in grafted_content


# auto-compact behavior is now implemented by the before_llm hook in agent/hooks/compact.py,
# tests in agent/tests/test_compact.py's hook tests. This file only tests claim node
# itself (inbound dispatch + state replacement), no longer tests auto-compact.


# ────────────────────────────────────────────────────────────────────────
# mutmut gap-fix follow-up tests (PR #296 → this PR)
# ────────────────────────────────────────────────────────────────────────
# Lock down actionable survived mutation cluster from baseline:
# - `_by_who`: 'self' literal case-sensitivity + return text
# - `_wait_for_batch`: empty batch retry loop + try/finally state machine rollback
# - `_claim_node_impl`: dispatch boundary conditions, container mode, multi-step continue
# - `claim_node`: outer wrapper does not swallow return + msg_count not skewed
# Target ~15-20 actionable mutations killed; _render_restart_completed_marker's
# ~70 string noise not covered.


# ───────────── _by_who unit tests (case-sensitive dispatch) ─────────────


# ───────────── _render_restart_completed_marker wording ───────────────


# ───────────── _wait_for_batch state machine + retry loop ─────────────


# ───────────── _claim_node_impl: container mode (ops_db None) ─────────────


# ───────────── _claim_node_impl: multi-step continue (no batch, not halted) ─────────────


# ───────────── _claim_node_impl: dispatch details ─────────────


# ────────────────────────────────────────────────────────────────────────
# claim_agent_row_or_die_on_stale_schema — schema gate ahead of the row claim
# ────────────────────────────────────────────────────────────────────────
# Boot must verify the central DB schema matches this code BEFORE flipping the
# row directly to running. Gating first keeps a doomed child unclaimed.
