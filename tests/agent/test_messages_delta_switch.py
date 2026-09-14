"""Messages channel: DeltaChannel form since the write switch (task #3180).

Pins:
1. Annotation normalization - `add_messages` (plugin-declared spelling),
   `guarded_add_messages` (pre-switch channel reducer) and the delta form
   (`DeltaChannel(guarded_delta_reducer, ...)`) are one contract; plugin
   registration keeps accepting the declared spelling.
2. Reducer resolution - the delta form resolves to the guarded single-merge,
   never the last-value fallback (a silent full overwrite of `messages`).
3. A real LangGraph run over the flipped channel stores delta artifacts
   (per-super-step writes + periodic `_DeltaSnapshot` cadence) and folds at
   read time on resume.
"""

from typing import Annotated, Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.channels.delta import DeltaChannel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

from agent.messages_guard import guarded_add_messages, guarded_delta_reducer
from agent.state import (
    _MESSAGES_DELTA_CHANNEL,
    _MESSAGES_DELTA_SNAPSHOT_FREQUENCY,
    BaseAgentState,
    _accumulate_delta,
    _is_messages_reducer_form,
    _messages_annotation_key,
    _resolve_reducer,
)

PLUGIN_SPELLING = Annotated[list[AnyMessage], add_messages]
GUARDED_SPELLING = Annotated[list[AnyMessage], guarded_add_messages]


def test_base_annotation_is_the_delta_form() -> None:
    meta = BaseAgentState.__annotations__["messages"].__metadata__
    assert len(meta) == 1
    channel = meta[0]
    assert isinstance(channel, DeltaChannel)
    assert channel.reducer is guarded_delta_reducer
    assert channel.snapshot_frequency == _MESSAGES_DELTA_SNAPSHOT_FREQUENCY


def test_annotation_keys_equivalent_across_spellings() -> None:
    base_key = _messages_annotation_key(BaseAgentState.__annotations__["messages"])
    assert _messages_annotation_key(PLUGIN_SPELLING) == base_key
    assert _messages_annotation_key(GUARDED_SPELLING) == base_key


def test_resolve_reducer_never_falls_back_for_the_delta_form() -> None:
    # The real base field: what the working-copy machinery resolves.
    assert _resolve_reducer(BaseAgentState.model_fields["messages"]) is guarded_add_messages


def test_resolve_reducer_ignores_foreign_delta_channels() -> None:
    foreign = DeltaChannel(lambda _state, writes: writes, snapshot_frequency=5)

    class _Foreign(BaseModel):
        value: Annotated[list[AnyMessage], foreign] = Field(default_factory=list)

    assert _resolve_reducer(_Foreign.model_fields["value"]) is not guarded_add_messages


def test_accumulate_delta_concatenates_for_every_messages_spelling() -> None:
    acc = [HumanMessage(content="a", id="a")]
    new = [HumanMessage(content="b", id="b")]
    for reducer in (add_messages, guarded_add_messages, _MESSAGES_DELTA_CHANNEL):
        merged = _accumulate_delta(acc, new, reducer)
        assert [m.id for m in merged] == ["a", "b"]


def test_is_messages_reducer_form_membership() -> None:
    assert _is_messages_reducer_form(add_messages)
    assert _is_messages_reducer_form(guarded_add_messages)
    assert _is_messages_reducer_form(_MESSAGES_DELTA_CHANNEL)
    assert not _is_messages_reducer_form(lambda _old, new: new)


class _FlippedState(BaseAgentState):
    """Minimal graph state over the real flipped base (fields inherited)."""

    n: int = 0
    target: int = 0


def _build(saver: Any) -> Any:
    def step(state: _FlippedState) -> dict[str, Any]:
        n = state.n
        return {
            "messages": [
                HumanMessage(content=f"u{n}", id=f"u{n}"),
                AIMessage(content=f"a{n}", id=f"a{n}"),
            ],
            "n": n + 1,
        }

    def route(state: _FlippedState) -> str:
        return "step" if state.n < state.target else END

    graph: Any = StateGraph(_FlippedState)
    graph.add_node("step", step)
    graph.add_edge(START, "step")
    graph.add_conditional_edges("step", route)
    return graph.compile(checkpointer=saver)


async def test_flipped_channel_writes_delta_and_resumes_folded() -> None:
    from agent.startup import _delta_evidence

    saver = InMemorySaver()
    graph = _build(saver)
    cfg: dict[str, Any] = {"configurable": {"thread_id": "switch-rt"}}
    final = await graph.ainvoke({"n": 0, "target": 6}, cfg)
    assert final["n"] == 6
    ids = [m.id for m in final["messages"]]
    assert ids == [f"{p}{n}" for n in range(6) for p in ("u", "a")]

    tup = await saver.aget_tuple(cfg)
    assert tup is not None
    assert "messages" in _delta_evidence(tup.checkpoint, tup.metadata)

    final2 = await graph.ainvoke({"n": 6, "target": 6}, cfg)
    ids2 = [m.id for m in final2["messages"]]
    assert ids2[: len(ids)] == ids
    assert len(ids2) == len(ids) + 2
