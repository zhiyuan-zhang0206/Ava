"""Delta-channel form of the append-only guard (task #3187).

``guarded_delta_reducer`` adapts the task-#1256 guard to the write shape a
``DeltaChannel`` uses. A delta-channel read replays stored writes through
the reducer, so the guard must (a) accept the three legal mutation classes
exactly as the commit-time guard does, (b) fail fast on the forbidden
shapes, and (c) stay batching-invariant
(``reducer(reducer(s, xs), ys) == reducer(s, xs + ys)``), because a read
may combine writes into larger batches than they were written in.

The graph-level tests drive a real ``StateGraph`` whose messages channel is
``DeltaChannel(guarded_delta_reducer, ...)`` over an ``InMemorySaver``:
appends, a tail edit and a full-wipe reset survive a replay from storage,
and a forbidden edit raises through ``aupdate_state`` before anything is
persisted.
"""

from typing import Annotated, Any, TypedDict

import pytest
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.channels.delta import DeltaChannel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import (
    REMOVE_ALL_MESSAGES,
    _messages_delta_reducer,
)

from agent.messages_guard import (
    MessagesMutationError,
    guarded_add_messages,
    guarded_delta_reducer,
)


def _msgs(*specs: tuple[str, str]) -> list[AnyMessage]:
    return [HumanMessage(content=c, id=i) for i, c in specs]


def _ids(messages: list[AnyMessage]) -> list[str | None]:
    return [m.id for m in messages]


# -- Equivalence with the commit-time guard, on the delta write shape --------


def test_append_matches_guard():
    before = _msgs(("a", "hi"), ("b", "there"))
    write = [HumanMessage(content="!", id="c")]
    merged = guarded_delta_reducer(before, [write])
    assert merged == guarded_add_messages(before, write)
    assert _ids(merged) == ["a", "b", "c"]


def test_multiwrite_fold_is_batching_invariant():
    """DeltaChannel replays writes in combined batches: folding xs then ys
    must equal folding xs + ys, and equal one guarded merge of the flat
    delta."""
    before = _msgs(("a", "1"))
    xs = [HumanMessage(content="2", id="b")]
    ys = [HumanMessage(content="3", id="c")]
    combined = guarded_delta_reducer(before, [xs, ys])
    sequential = guarded_delta_reducer(guarded_delta_reducer(before, [xs]), [ys])
    flattened = guarded_delta_reducer(before, [xs + ys])
    assert combined == sequential == flattened
    assert combined == guarded_add_messages(before, xs + ys)
    assert _ids(combined) == ["a", "b", "c"]


def test_single_message_write_is_one_message():
    before = _msgs(("a", "1"))
    merged = guarded_delta_reducer(before, [HumanMessage(content="2", id="b")])
    assert _ids(merged) == ["a", "b"]


def test_tail_edit_matches_guard():
    before = _msgs(("a", "1"), ("b", "2"), ("c", "3"))
    write = [HumanMessage(content="3!", id="c")]
    merged = guarded_delta_reducer(before, [write])
    assert merged == guarded_add_messages(before, write)
    assert merged[-1].content == "3!"


def test_full_wipe_rebuild_matches_guard():
    """The crash-repair shape: REMOVE_ALL + re-list + splice is one legal
    wipe-class write."""
    before = _msgs(("a", "1"), ("b", "2"))
    write = [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        before[0],
        ToolMessage(content="[interrupted]", tool_call_id="t1", id="tr1"),
        before[1],
    ]
    merged = guarded_delta_reducer(before, [write])
    assert merged == guarded_add_messages(before, write)
    assert _ids(merged) == ["a", "tr1", "b"]


def test_wipe_split_from_relist_stays_invariant():
    before = _msgs(("a", "1"), ("b", "2"))
    wipe = [RemoveMessage(id=REMOVE_ALL_MESSAGES)]
    relist = [HumanMessage(content="s", id="s1")]
    combined = guarded_delta_reducer(before, [wipe, relist])
    sequential = guarded_delta_reducer(guarded_delta_reducer(before, [wipe]), [relist])
    assert combined == sequential == [HumanMessage(content="s", id="s1")]


def test_wipe_then_modify_last_stays_invariant():
    """After a full-wipe rebuild, the next write may modify the rebuilt
    tail - legal at commit time; a single-shot concatenation check would
    misread the edit as rebuild tampering and reject the history."""
    before = _msgs(("a", "1"), ("b", "2"), ("c", "3"))
    wipe = [RemoveMessage(id=REMOVE_ALL_MESSAGES), before[0], before[1], before[2]]
    edit = [HumanMessage(content="3!", id="c")]
    combined = guarded_delta_reducer(before, [wipe, edit])
    sequential = guarded_delta_reducer(guarded_delta_reducer(before, [wipe]), [edit])
    assert combined == sequential
    assert _ids(combined) == ["a", "b", "c"]
    assert combined[-1].content == "3!"


# -- Forbidden shapes fail fast ----------------------------------------------


def test_edit_earlier_message_raises():
    before = _msgs(("a", "1"), ("b", "2"), ("c", "3"))
    with pytest.raises(MessagesMutationError, match="only the last message"):
        guarded_delta_reducer(before, [[HumanMessage(content="CHANGED", id="a")]])


def test_delete_middle_via_remove_raises():
    before = _msgs(("a", "1"), ("b", "2"), ("c", "3"))
    with pytest.raises(MessagesMutationError, match="deletion"):
        guarded_delta_reducer(before, [[RemoveMessage(id="b")]])


def test_delete_last_and_append_raises():
    before = _msgs(("a", "1"), ("b", "2"), ("c", "3"))
    write = [RemoveMessage(id="c"), HumanMessage(content="new", id="d")]
    with pytest.raises(MessagesMutationError):
        guarded_delta_reducer(before, [write])


def test_bulk_individual_removal_raises():
    """MRE #3184's raw reset shape (an individual RemoveMessage per message)
    is a deletion without a wipe marker; the ruling's reset form is the
    full-wipe class (REMOVE_ALL + re-list)."""
    before = _msgs(("a", "1"), ("b", "2"))
    write = [RemoveMessage(id="a"), RemoveMessage(id="b")]
    with pytest.raises(MessagesMutationError, match="deleted"):
        guarded_delta_reducer(before, [write])


def test_unknown_id_removal_raises_like_add_messages():
    """Removing an id that never existed is an add_messages-level error
    (ValueError), identical to the commit-time guard's behavior."""
    before = _msgs(("a", "1"))
    with pytest.raises(ValueError, match="doesn't exist"):
        guarded_delta_reducer(before, [[RemoveMessage(id="zz")]])


def test_wipe_tampering_survivor_raises():
    before = _msgs(("a", "1"), ("b", "2"))
    write = [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        HumanMessage(content="1 EDITED", id="a"),
        before[1],
    ]
    with pytest.raises(MessagesMutationError, match="altered surviving"):
        guarded_delta_reducer(before, [write])


def test_violation_in_later_write_fails_the_fold():
    before = _msgs(("a", "1"))
    good = [HumanMessage(content="2", id="b")]
    bad = [HumanMessage(content="CHANGED", id="a")]
    with pytest.raises(MessagesMutationError):
        guarded_delta_reducer(before, [good, bad])


def test_stock_delta_reducer_has_no_invariant():
    """Red evidence for the guard: langgraph's experimental delta reducer
    has no invariant of its own and silently accepts a mid-history edit."""
    before = _msgs(("a", "1"), ("b", "2"), ("c", "3"))
    merged = _messages_delta_reducer(list(before), [[HumanMessage(content="CHANGED", id="a")]])
    assert merged[0].content == "CHANGED"
    assert _ids(merged) == ["a", "b", "c"]


# -- Graph-level: DeltaChannel messages reducer over InMemorySaver -----------


def _build_app(saver: InMemorySaver, snapshot_frequency: int):
    class S(TypedDict):
        messages: Annotated[
            list[AnyMessage],
            DeltaChannel(guarded_delta_reducer, snapshot_frequency=snapshot_frequency),
        ]
        n: int
        target: int

    def step(state: S) -> dict[str, Any]:
        n = state["n"]
        return {
            "messages": [
                HumanMessage(content=f"u{n}", id=f"u{n}"),
                AIMessage(content=f"a{n}", id=f"a{n}"),
            ],
            "n": n + 1,
        }

    graph = StateGraph(S)
    graph.add_node("step", step)  # pyright: ignore[reportUnknownMemberType]
    graph.add_edge(START, "step")

    def route(state: S) -> str:
        return "step" if state["n"] < state["target"] else END

    graph.add_conditional_edges("step", route)  # pyright: ignore[reportUnknownMemberType]
    return graph.compile(checkpointer=saver)  # pyright: ignore[reportUnknownMemberType]


async def _start(app: Any, cfg: RunnableConfig, target: int) -> None:
    await app.ainvoke({"messages": [], "n": 0, "target": target}, cfg, recursion_limit=40)


async def _advance(app: Any, cfg: RunnableConfig, target: int) -> None:
    await app.ainvoke({"target": target}, cfg, recursion_limit=40)


async def _read(app: Any, cfg: RunnableConfig) -> list[AnyMessage]:
    state = await app.aget_state(cfg)
    return list(state.values["messages"])


async def test_graph_append_replays_from_storage():
    saver = InMemorySaver()
    cfg: RunnableConfig = {"configurable": {"thread_id": "guard-delta-append"}}
    app = _build_app(saver, snapshot_frequency=1000)
    await _start(app, cfg, 3)
    fresh = _build_app(saver, snapshot_frequency=1000)  # new compile folds from storage
    assert _ids(await _read(fresh, cfg)) == ["u0", "a0", "u1", "a1", "u2", "a2"]


async def test_graph_tail_edit_survives_replay_and_continuation():
    saver = InMemorySaver()
    cfg: RunnableConfig = {"configurable": {"thread_id": "guard-delta-edit"}}
    app = _build_app(saver, snapshot_frequency=1000)
    await _start(app, cfg, 2)
    await app.aupdate_state(cfg, {"messages": [AIMessage(content="a1 fixed", id="a1")]})
    fresh = _build_app(saver, snapshot_frequency=1000)
    assert [(m.id, m.content) for m in await _read(fresh, cfg)] == [
        ("u0", "u0"),
        ("a0", "a0"),
        ("u1", "u1"),
        ("a1", "a1 fixed"),
    ]
    await _advance(_build_app(saver, snapshot_frequency=1000), cfg, 3)
    fresh2 = _build_app(saver, snapshot_frequency=1000)
    assert _ids(await _read(fresh2, cfg)) == ["u0", "a0", "u1", "a1", "u2", "a2"]


async def test_graph_full_wipe_reset_survives_replay_and_continuation():
    saver = InMemorySaver()
    cfg: RunnableConfig = {"configurable": {"thread_id": "guard-delta-wipe"}}
    app = _build_app(saver, snapshot_frequency=1000)
    await _start(app, cfg, 2)
    await app.aupdate_state(
        cfg,
        {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                HumanMessage(content="fresh start", id="s1"),
            ]
        },
    )
    fresh = _build_app(saver, snapshot_frequency=1000)
    assert [(m.id, m.content) for m in await _read(fresh, cfg)] == [("s1", "fresh start")]
    await _advance(_build_app(saver, snapshot_frequency=1000), cfg, 3)
    fresh2 = _build_app(saver, snapshot_frequency=1000)
    assert [(m.id, m.content) for m in await _read(fresh2, cfg)] == [
        ("s1", "fresh start"),
        ("u2", "u2"),
        ("a2", "a2"),
    ]


async def test_graph_forbidden_edit_raises_before_persisting():
    saver = InMemorySaver()
    cfg: RunnableConfig = {"configurable": {"thread_id": "guard-delta-violation"}}
    app = _build_app(saver, snapshot_frequency=1000)
    await _start(app, cfg, 2)
    with pytest.raises(MessagesMutationError):
        await app.aupdate_state(cfg, {"messages": [AIMessage(content="changed", id="u1")]})
    fresh = _build_app(saver, snapshot_frequency=1000)
    assert _ids(await _read(fresh, cfg)) == ["u0", "a0", "u1", "a1"]


async def test_graph_snapshot_boundaries_do_not_change_replay():
    """A small snapshot_frequency interleaves _DeltaSnapshot writes with
    delta replay; the post-edit state must read back through them
    identically."""
    saver = InMemorySaver()
    cfg: RunnableConfig = {"configurable": {"thread_id": "guard-delta-snap"}}
    app = _build_app(saver, snapshot_frequency=2)
    await _start(app, cfg, 6)
    await app.aupdate_state(cfg, {"messages": [AIMessage(content="a5 fixed", id="a5")]})
    await _advance(_build_app(saver, snapshot_frequency=2), cfg, 7)
    fresh = _build_app(saver, snapshot_frequency=2)
    messages = await _read(fresh, cfg)
    assert messages[-1].id == "a6"
    assert any(m.id == "a5" and m.content == "a5 fixed" for m in messages)
    assert len(messages) == 14
