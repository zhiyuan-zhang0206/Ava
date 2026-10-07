"""Real graph persistence distinguishes new messages from legacy replay UUIDs."""

from typing import Annotated, Any, TypedDict

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.channels import DeltaChannel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Durability

from agent.hooks._registry import _merge_result
from agent.messages.guard import guarded_add_messages, guarded_delta_reducer
from agent.state import BaseAgentState
from base.agents.history.timeline import build_timeline_items
from base.agents.messages.identity import normalize_stored_message_ids


@pytest.mark.parametrize("durability", ["sync", "async", "exit"])
def test_graph_input_node_and_working_copy_output_have_persisted_ids(
    durability: Durability,
) -> None:
    class State(TypedDict):
        messages: Annotated[list[Any], DeltaChannel(guarded_delta_reducer, snapshot_frequency=1000)]

    def reply(state: State) -> dict[str, Any]:
        direct = AIMessage(content="same text")
        working = AIMessage(content="same text")
        # Plugin working-copy/hook co-write uses the single-merge guard before
        # put_writes. It must not mark these newly created deltas as legacy.
        guarded_add_messages(state["messages"], [working])
        update: dict[str, Any] = {}
        writers: dict[str, str] = {}
        for name in ("first", "second"):
            _merge_result(
                update,
                writers,
                {"messages": [AIMessage(content="same text")]},
                name,
                "before_llm",
                BaseAgentState.model_fields,
            )
        return {"messages": [direct, working, *update["messages"]]}

    graph = StateGraph(State)
    graph.add_node("reply", reply)  # pyright: ignore[reportUnknownMemberType]
    graph.add_edge(START, "reply")
    graph.add_edge("reply", END)
    saver = InMemorySaver()
    app = graph.compile(checkpointer=saver)  # pyright: ignore[reportUnknownMemberType]
    config: RunnableConfig = {"configurable": {"thread_id": f"identity-{durability}"}}
    app.invoke({"messages": [HumanMessage(content="new input")]}, config, durability=durability)  # pyright: ignore[reportUnknownMemberType]
    first = app.get_state(config).values["messages"]
    second = app.get_state(config).values["messages"]
    assert len(first) == 5
    assert len({m.id for m in first}) == 5
    assert all(m.id and not m.additional_kwargs.get("ava_ephemeral_message_id") for m in first)
    assert [m.id for m in first] == [m.id for m in second]
    items, _ = build_timeline_items(first, [])
    assert [i.source_message_id for i in items] == [m.id for m in first]


def test_legacy_read_marker_precedes_working_merge_and_survives_reducer() -> None:
    raw = AIMessage(content="legacy seed")
    stored = normalize_stored_message_ids([raw])
    guarded_add_messages(stored, [HumanMessage(content="new working delta")])
    assert raw.id is not None
    replayed = guarded_delta_reducer([], [stored])
    item = build_timeline_items(replayed, [])[0][0]
    assert item.source_message_id is None
    assert raw.additional_kwargs["ava_ephemeral_message_id"] is True
