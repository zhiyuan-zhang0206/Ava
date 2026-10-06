"""First-takeover context and crash-safe introduction receipts."""

from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from agent import impersonation
from agent.impersonation_handoff import ensure_start_marker, start_marker
from agent.state import BaseAgentState


def _session(number: int = 0) -> dict[str, Any]:
    return {
        "agent_id": 42,
        "session_id": number,
        "name": "Continue the task",
        "executor_name": "Codex",
        "relay_provider": "codex",
    }


async def _graph() -> tuple[Any, RunnableConfig]:
    builder: Any = StateGraph(BaseAgentState)

    def idle(_state: BaseAgentState) -> dict[str, Any]:
        return {}

    builder.add_node("idle", idle)
    builder.add_edge(START, "idle")
    builder.add_edge("idle", END)
    graph: Any = builder.compile(checkpointer=MemorySaver())
    config: RunnableConfig = {"configurable": {"thread_id": "42"}}
    await graph.ainvoke(
        {"messages": [HumanMessage(id="initial-request", content="Finish the task")]}, config
    )
    return graph, config


async def test_first_takeover_explains_borrowed_identity_and_later_leases_do_not_repeat() -> None:
    graph, config = await _graph()
    await ensure_start_marker(graph, _session())
    first = await graph.aget_state(config)
    assert first.values["impersonation_introduced"] is True
    explanation = first.values["messages"][-2]
    assert explanation.id == "impersonation-introduction"
    assert "native execution pauses" in explanation.content
    assert "inbound messages are delivered to that executor" in explanation.content
    assert "SDK under your identity" in explanation.content
    assert "ACK confirms message receipt, not task completion" in explanation.content
    assert first.values["messages"][-1].id == "impersonation-start:42:0"

    await graph.aupdate_state(
        config, {"messages": [HumanMessage(id="next-request", content="More work")]}
    )
    await ensure_start_marker(graph, _session())
    await ensure_start_marker(graph, _session(1))
    await ensure_start_marker(graph, _session(1))
    messages = (await graph.aget_state(config)).values["messages"]
    assert sum(m.id == "impersonation-introduction" for m in messages) == 1
    assert sum(m.id == "impersonation-start:42:0" for m in messages) == 1
    assert sum(m.id == "impersonation-start:42:1" for m in messages) == 1


async def test_crash_after_checkpoint_before_flush_does_not_repeat_explanation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph, config = await _graph()
    attempts = 0

    async def interrupted_flush(_checkpointer: object, _agent_id: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Interrupted flush")

    monkeypatch.setattr(impersonation, "flush_checkpoint", interrupted_flush)
    with pytest.raises(RuntimeError, match="Interrupted flush"):
        await ensure_start_marker(graph, _session())
    await ensure_start_marker(graph, _session())
    snapshot = await graph.aget_state(config)
    assert snapshot.values["impersonation_introduced"] is True
    assert [m.id for m in snapshot.values["messages"][-2:]] == [
        "impersonation-introduction",
        "impersonation-start:42:0",
    ]
    assert len(snapshot.values["messages"]) == 3
    assert attempts == 2


async def test_old_checkpoint_with_start_marker_gets_explanation_without_rewriting_history() -> (
    None
):
    graph, config = await _graph()
    marker = start_marker(_session())
    await graph.aupdate_state(
        config, {"messages": [marker, HumanMessage(id="saved-work", content="Saved work")]}
    )
    before = (await graph.aget_state(config)).values["messages"]
    await ensure_start_marker(graph, _session())
    snapshot = await graph.aget_state(config)
    assert snapshot.values["messages"][:-1] == before
    assert snapshot.values["messages"][-1].id == "impersonation-introduction"
    assert snapshot.values["impersonation_introduced"] is True
