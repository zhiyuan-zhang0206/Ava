"""First-takeover context and crash-safe introduction receipts."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from agent import impersonation, impersonation_handoff
from agent.impersonation_handoff import ensure_start_marker, start_marker
from agent.state import BaseAgentState
from base.clock import Clock, ClockConfig
from base.config import settings
from base.native_process.runtime_incarnation import RuntimeIncarnation


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
    assert "You are paused" in explanation.content
    assert "receives incoming messages" in explanation.content
    assert "coming from you, an Ava agent" in explanation.content
    assert "continue any unfinished requests" in explanation.content
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


@pytest.mark.parametrize("timestamps,weekday", [(True, False), (True, True), (False, False)])
async def test_all_takeover_notes_stamp_their_real_creation_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, timestamps: bool, weekday: bool
) -> None:
    moment = datetime(2026, 10, 6, 13, 39, 25, tzinfo=UTC)
    clock = Clock(ClockConfig("America/New_York", "America/New_York", weekday), now=lambda: moment)

    def fixed_clock(_cls: type[Clock]) -> Clock:
        return clock

    def saved_document(*_args: object) -> tuple[str, str]:
        return "Finished", str(tmp_path / "0.json")

    def no_op(*_args: object) -> None:
        pass

    monkeypatch.setattr(Clock, "from_settings", classmethod(fixed_clock))
    monkeypatch.setattr(settings.general, "message_timestamps", timestamps)
    monkeypatch.setattr(impersonation_handoff, "_save_document", saved_document)
    monkeypatch.setattr(impersonation_handoff, "_receipt", no_op)
    monkeypatch.setattr(impersonation_handoff, "publish_inbound_wake", no_op)
    graph, config = await _graph()
    await ensure_start_marker(graph, _session())
    await impersonation_handoff.deliver_handoff(
        graph, Mock(), Mock(), _session(), RuntimeIncarnation(42, uuid4(), uuid4())
    )
    notes = (await graph.aget_state(config)).values["messages"][1:]
    assert [note.id for note in notes] == [
        "impersonation-introduction",
        "impersonation-start:42:0",
        "impersonation-handoff:42:0",
    ]
    date = "2026-10-06 Tue" if weekday else "2026-10-06"
    prefix = f"[system] [{date} 09:39:25] " if timestamps else "[system] Impersonation"
    for note in notes:
        assert str(note.content).startswith(prefix)
        assert note.additional_kwargs["ava_created_at"] == moment.isoformat()
        assert note.additional_kwargs["ava_note_tag"] == "impersonation"
