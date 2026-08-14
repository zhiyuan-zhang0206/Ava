"""`init_context` node — the sole owner of the agent's standing message head.

The head is the SystemMessage plus the ordered context notes. It used to be
assembled in four hand-copied places (claim's cold start, claim's stale-pop,
claim's compact return, the auto-compact hook); the drift between those copies
is what these tests pin down now that there is one owner.
"""

from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph._context import AvaContext
from agent.graph._init_context import init_context_node
from agent.state import AgentState, ContextReset
from shared.config import settings
from shared.db import create_agent


def _config(tid: int) -> RunnableConfig:
    return {"configurable": {"thread_id": str(tid)}}


def _runtime(ops_pool: AsyncConnectionPool | None) -> Runtime[AvaContext]:
    """`ops_pool=None` takes the container path (matching MyAva's `evals/__main__.py`)."""
    return Runtime(
        context=AvaContext(ops_pool=ops_pool, llm=AsyncMock(), event_publisher=MagicMock())
    )


def _note(tag: str) -> HumanMessage:
    return HumanMessage(
        content=f"NOTE-{tag}",
        additional_kwargs={"ava_msg_type": "system_note", "ava_note_tag": tag},
    )


def _fake_notes(monkeypatch: pytest.MonkeyPatch, *tags: str) -> list[HumanMessage]:
    """Pin the ordered registry to a known list so ordering assertions do not
    depend on which layers happen to be enabled in the test environment."""
    notes = [_note(t) for t in tags]
    monkeypatch.setattr("agent.graph._init_context.context_notes", lambda: list(notes))
    return notes


def _tags(msgs: list[AnyMessage]) -> list[str | None]:
    return [m.additional_kwargs.get("ava_note_tag") for m in msgs]  # pyright: ignore[reportUnknownMemberType]


async def test_empty_window_lays_down_system_prompt_then_notes(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty window is the one condition that triggers establishment: the
    SystemMessage first, then the notes in registry order."""
    tid = create_agent(db_conn)
    notes = _fake_notes(monkeypatch, "memory_discipline", "memory", "agent_id")

    cmd = await init_context_node(AgentState(), _runtime(aops_pool), _config(tid))

    msgs = cmd.update["messages"]  # type: ignore[index]
    assert isinstance(msgs[0], SystemMessage)
    assert str(msgs[0].content) != ""  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert msgs[1:] == notes
    assert cmd.goto == "claim"


async def test_intact_window_is_a_pass_through(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live conversation is left alone — no head, no re-injection, no update."""
    tid = create_agent(db_conn)
    _fake_notes(monkeypatch, "memory")
    state = AgentState(messages=[SystemMessage(content="sys"), HumanMessage(content="prev")])

    cmd = await init_context_node(state, _runtime(aops_pool), _config(tid))

    assert cmd.update is None
    assert cmd.goto == "claim"


async def test_parked_tail_lands_behind_the_head(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a compaction parks — the summary, then any chats co-batched with it —
    is laid down after the notes, and the requester's resume is honoured."""
    tid = create_agent(db_conn)
    notes = _fake_notes(monkeypatch, "memory", "agent_id")
    summary = HumanMessage(content="THE-SUMMARY")
    chat = HumanMessage(content="a chat that arrived with the compact")
    state = AgentState(
        context_reset=ContextReset(tail=[summary, chat], resume="before_llm"),
    )

    cmd = await init_context_node(state, _runtime(aops_pool), _config(tid))

    msgs = cmd.update["messages"]  # type: ignore[index]
    assert isinstance(msgs[0], SystemMessage)
    assert msgs[1:3] == notes
    assert msgs[3] is summary
    assert msgs[4] is chat
    assert cmd.goto == "before_llm"


async def test_consumed_reset_is_cleared(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The channel is written back to its default, so a later invocation cannot
    replay the same tail into a second window."""
    tid = create_agent(db_conn)
    _fake_notes(monkeypatch, "memory")
    state = AgentState(
        context_reset=ContextReset(tail=[HumanMessage(content="s")], resume="llm"),
    )

    cmd = await init_context_node(state, _runtime(aops_pool), _config(tid))

    reset = cmd.update["context_reset"]  # type: ignore[index]
    assert reset.tail == []  # pyright: ignore[reportUnknownMemberType]
    assert reset.resume == "claim"  # pyright: ignore[reportUnknownMemberType]


async def test_container_mode_head_is_the_bare_prompt(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The eval harness has no ops_pool, no workspace, and nothing the notes
    describe — its head is the system prompt alone, so an eval's context stays
    deterministic."""
    tid = create_agent(db_conn)
    _fake_notes(monkeypatch, "memory", "agent_id")  # would be injected if consulted

    cmd = await init_context_node(AgentState(), _runtime(None), _config(tid))

    msgs = cmd.update["messages"]  # type: ignore[index]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(msgs[0], SystemMessage)


async def test_head_is_identical_whether_or_not_the_window_had_history(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression this node exists to prevent.

    Establishment used to run in four hand-maintained copies of the same ordered
    list, and they had drifted: the preloaded-skills note was appended at cold
    start, never popped by the stale-pop block, and missing from claim's
    post-compact list — so an agent-authored compaction silently dropped it while
    an auto-compaction kept it. With one owner and one condition (an empty
    window), every path that empties the window gets the same head.
    """
    tid = create_agent(db_conn)
    tags = (
        "memory_discipline",
        "memory",
        "agent_id",
        "agent_memory",
        "exec_timeout",
        "preloaded_skills",
    )
    _fake_notes(monkeypatch, *tags)

    cold = await init_context_node(AgentState(), _runtime(aops_pool), _config(tid))
    # A compaction reaches this node the same way: the window is already empty
    # (RemoveMessage(REMOVE_ALL) applied), with the summary parked.
    post_compact = await init_context_node(
        AgentState(context_reset=ContextReset(tail=[HumanMessage(content="sum")], resume="llm")),
        _runtime(aops_pool),
        _config(tid),
    )

    cold_msgs = cold.update["messages"]  # type: ignore[index]
    compact_msgs = post_compact.update["messages"]  # type: ignore[index]
    assert _tags(cold_msgs) == [None, *tags]  # pyright: ignore[reportUnknownArgumentType]
    assert _tags(compact_msgs) == [  # pyright: ignore[reportUnknownArgumentType]
        None,
        *tags,
        None,
    ]  # same head, then the summary  # pyright: ignore[reportUnknownArgumentType]
    assert "preloaded_skills" in _tags(compact_msgs)  # pyright: ignore[reportUnknownArgumentType]


async def test_established_head_records_what_the_capability_index_lists(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The `# Capabilities` section it renders is a snapshot of a live filesystem
    scan, so the same act has to record what that snapshot covered — otherwise
    nothing downstream can tell that a skill installed later is missing from it.
    """
    import ava.skills as skills_mod

    d = tmp_path / "skills"
    (d / "alpha").mkdir(parents=True)
    (d / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Alpha desc\n---\n\nBODY\n", encoding="utf-8"
    )
    monkeypatch.setattr(skills_mod, "_skills_dir", lambda: d)
    monkeypatch.setattr(skills_mod, "enabled_skill_names", lambda: {"alpha"})
    monkeypatch.setattr(settings.agent, "skills_to_inject_into_system_prompt", ["*"])
    tid = create_agent(db_conn)
    _fake_notes(monkeypatch, "memory")

    cmd = await init_context_node(AgentState(), _runtime(aops_pool), _config(tid))

    prompt = str(cmd.update["messages"][0].content)  # type: ignore[index]
    assert "- `ava.skills.alpha` — Alpha desc" in prompt
    assert cmd.update["capabilities"].indexed == {"alpha"}  # type: ignore[index]


async def test_a_skill_installed_after_establishment_reaches_the_next_turn(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The whole seam, composed: establish a window, install a skill into the
    live catalog the way an install does, and the standing SystemMessage — frozen
    — still does not mention it, while the next turn's before_llm pass does.

    The two halves are unit-tested separately; this is the one place they are
    driven in sequence, with the snapshot handed from the node that records it to
    the hook that reads it.
    """
    import ava.skills as skills_mod
    from agent.hooks.capabilities import _newly_installed_skills

    d = tmp_path / "skills"
    (d / "alpha").mkdir(parents=True)
    (d / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Alpha desc\n---\n\nBODY\n", encoding="utf-8"
    )
    monkeypatch.setattr(skills_mod, "_skills_dir", lambda: d)
    monkeypatch.setattr(
        skills_mod, "enabled_skill_names", lambda: {p.name for p in d.iterdir() if p.is_dir()}
    )
    monkeypatch.setattr(settings.agent, "skills_to_inject_into_system_prompt", ["*"])
    tid = create_agent(db_conn)
    _fake_notes(monkeypatch, "memory")

    established = await init_context_node(AgentState(), _runtime(aops_pool), _config(tid))
    head = established.update["messages"]  # type: ignore[index]

    (d / "beta").mkdir(parents=True)
    (d / "beta" / "SKILL.md").write_text(
        "---\nname: beta\ndescription: Beta desc\n---\n\nBODY\n", encoding="utf-8"
    )

    # The rendered prompt is frozen — this is the bug, and it stays true.
    assert "beta" not in str(head[0].content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    state = AgentState(messages=head, capabilities=established.update["capabilities"])  # type: ignore[index]
    update = await _newly_installed_skills(state, _runtime(aops_pool), _config(tid))

    assert update is not None
    (note,) = update["messages"]
    assert "- `ava.skills.beta` — Beta desc" in note.content  # pyright: ignore[reportUnknownMemberType]
    assert update["capabilities"].indexed == {"alpha", "beta"}  # pyright: ignore[reportUnknownMemberType]


async def test_a_compaction_in_the_same_pass_keeps_its_summary_and_its_head(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The composed failure the drift check's defer exists to prevent.

    A compaction and a mid-session install can land in the same before_llm pass.
    Compaction writes `RemoveMessage(REMOVE_ALL)` and parks the summary for this
    node; a drift note would ride the same `messages` channel, and `add_messages`
    applies the wipe and THEN the append — so an undeferred note is the one thing
    that survives it. This node's sole trigger is an empty channel, so it would
    read that single note as an intact history: summary dropped, SystemMessage
    never laid down, and the agent runs the whole next window on one orphan note.

    Driven through the real hook runner and the real reducer, because the bug
    lives in exactly that composition — each hook is correct on its own.
    """
    from langgraph.graph.message import add_messages

    import ava.skills as skills_mod
    from agent.hooks import HOOKS, make_hook_runner
    from agent.hooks import compact as compact_mod
    from agent.hooks.capabilities import _newly_installed_skills
    from agent.hooks.compact import _auto_compact_with_version_bump
    from shared.lm.context_budget import ContextBudget

    d = tmp_path / "skills"
    (d / "alpha").mkdir(parents=True)
    (d / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Alpha desc\n---\n\nBODY\n", encoding="utf-8"
    )
    monkeypatch.setattr(skills_mod, "_skills_dir", lambda: d)
    monkeypatch.setattr(
        skills_mod, "enabled_skill_names", lambda: {p.name for p in d.iterdir() if p.is_dir()}
    )
    monkeypatch.setattr(settings.agent, "skills_to_inject_into_system_prompt", ["*"])
    tid = create_agent(db_conn)
    _fake_notes(monkeypatch, "memory")

    established = await init_context_node(AgentState(), _runtime(aops_pool), _config(tid))

    # A skill lands mid-window, and the window is simultaneously over the
    # force-compact ceiling — one turn, both triggers.
    (d / "beta").mkdir(parents=True)
    (d / "beta" / "SKILL.md").write_text(
        "---\nname: beta\ndescription: Beta desc\n---\n\nBODY\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "agent.hooks.compact.resolve_context_budget",
        lambda _model: ContextBudget(  # pyright: ignore[reportUnknownArgumentType]
            max_context_tokens=1_000_000, soft_compact_tokens=1, hard_compact_tokens=1
        ),
    )
    summary_text = "compacted summary " * 100

    async def _fake_generate_summary(messages: list[AnyMessage], llm: object) -> str:
        return summary_text

    monkeypatch.setattr(compact_mod, "generate_summary", _fake_generate_summary)

    state = AgentState(
        messages=[
            *established.update["messages"],  # type: ignore[index]
            HumanMessage(content="x" * 4000, id="h0"),
        ],
        capabilities=established.update["capabilities"],  # type: ignore[index]
    )
    saved = list(HOOKS["before_llm"])
    HOOKS["before_llm"][:] = [_auto_compact_with_version_bump, _newly_installed_skills]
    try:
        cmd = await make_hook_runner("before_llm", default_next="llm")(
            state, _runtime(aops_pool), _config(tid)
        )
    finally:
        HOOKS["before_llm"][:] = saved

    assert cmd.goto == "init_context"
    assert isinstance(cmd.update, dict)
    # The reducer that makes the bug possible. With the note deferred there is
    # nothing to append behind the wipe, so the window really is empty — which is
    # the only state this node treats as "establish me".
    committed = cast(
        "list[AnyMessage]",
        add_messages(list(state.messages), cmd.update["messages"]),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    )
    assert committed == []

    reestablished = await init_context_node(
        AgentState(messages=committed, context_reset=cmd.update["context_reset"]),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        _runtime(aops_pool),
        _config(tid),
    )

    msgs = reestablished.update["messages"]  # type: ignore[index]
    assert isinstance(msgs[0], SystemMessage)
    # The rebuilt index carries the skill the deferred note would have named —
    # which is why deferring costs nothing.
    assert "- `ava.skills.beta` — Beta desc" in str(msgs[0].content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    # And the compaction's summary made it into the new window.
    assert summary_text in str(msgs[-1].content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


# ── checkpoint round-trip ──
# `context_reset` is the first state channel to carry messages, so the parked
# tail crosses the checkpointer between the node that parks it and the node that
# consumes it. A compaction that survived the write but lost its summary on the
# read would drop the agent's whole memory of the window it just compacted.


def test_parked_tail_survives_the_checkpoint_round_trip() -> None:
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    serde = JsonPlusSerializer()
    parked = ContextReset(
        tail=[
            HumanMessage(
                content="the summary", additional_kwargs={"ava_msg_type": "compact_summary"}
            ),
            HumanMessage(content="a chat co-batched with the compact"),
        ],
        resume="before_llm",
    )

    restored = serde.loads_typed(serde.dumps_typed(parked))

    assert [m.content for m in restored.tail] == [
        "the summary",
        "a chat co-batched with the compact",
    ]
    # The kwargs are what the timeline classifies the summary by — a bare
    # HumanMessage would render as a user turn.
    assert restored.tail[0].additional_kwargs["ava_msg_type"] == "compact_summary"
    assert restored.resume == "before_llm"


# `capabilities.indexed` crosses the checkpointer too — it is written by this
# node and read a turn later by the drift check. A snapshot that came back empty
# would make the whole installed catalog look newly installed; one that came back
# as `None` would silently disarm the check for the rest of the window.


def test_capabilities_snapshot_survives_the_checkpoint_round_trip() -> None:
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    from agent.state import CapabilitiesState

    serde = JsonPlusSerializer()

    recorded = CapabilitiesState(indexed={"alpha", "web-ai:deep-research"})
    restored = serde.loads_typed(serde.dumps_typed(recorded))
    assert restored.indexed == {"alpha", "web-ai:deep-research"}

    # "never recorded" has to stay distinguishable from "recorded as empty":
    # they take opposite branches in the drift check.
    assert serde.loads_typed(serde.dumps_typed(CapabilitiesState())).indexed is None
    assert serde.loads_typed(serde.dumps_typed(CapabilitiesState(indexed=set()))).indexed == set()
