"""The after_exec hook that delivers the findings an exec child raised.

`ava.security` appends findings to the exec turn's state update, the exec node commits them to
`state.security_findings`, and this hook turns them into SECURITY notes and clears the channel.
The tests build real graph state: one drives the hook directly, the other runs it as a node of a
real LangGraph so the channel's reducer (`operator.add`) and the `Overwrite([])` reset act.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Overwrite

from agent.graph.exec.node import _exec_node_impl
from agent.graph.interrupt import InterruptEvent
from agent.hooks.framework import framework_hooks
from agent.hooks.security import _deliver_security_findings
from agent.state import AgentState, build_agent_state
from agent.tests._fakes import make_fake_ops_pool
from ava.sdk_surface.process_context import process_clients
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity
from base.agents.messages.kwargs import NoteTag, read_ava_kwargs
from base.agents.messages.security_finding import SecurityFindingEntry
from base.clock import Clock
from base.config import settings
from base.db import Database
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import ExtensionRegistry

_CONFIG = {"configurable": {"thread_id": "1042"}}
_FIRST = SecurityFindingEntry(source="web.fetch", triggers=["ignore previous instructions"])
_SECOND = SecurityFindingEntry(source="context-file:/repo/AGENTS.md", triggers=["[system]"])


@pytest.fixture(autouse=True)
def _scan_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.agent, "security_scan_enabled", True)


async def _run_hook(state: AgentState) -> dict[str, Any] | None:
    runtime = Runtime(
        context=AvaContext(
            agent=AgentSlices.resolve(
                default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
            ),
            clock_factory=Clock.from_settings,
        )
    )
    return await _deliver_security_findings(state, runtime, _CONFIG)  # type: ignore[arg-type]


def test_the_hook_runs_after_exec() -> None:
    assert _deliver_security_findings in framework_hooks()["after_exec"]


async def test_no_pending_finding_is_a_no_op() -> None:
    assert await _run_hook(AgentState()) is None


async def test_each_pending_finding_becomes_one_security_note_and_the_channel_resets() -> None:
    update = await _run_hook(AgentState(security_findings=[_FIRST, _SECOND]))

    assert update is not None
    notes = update["messages"]
    assert [read_ava_kwargs(n).get("ava_note_tag") for n in notes] == [NoteTag.SECURITY.value] * 2
    assert "web.fetch" in notes[0].content
    assert "ignore previous instructions" in notes[0].content
    assert "context-file:/repo/AGENTS.md" in notes[1].content
    assert update["security_findings"] == Overwrite([])


async def test_findings_are_cleared_without_notes_when_scanning_was_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.agent, "security_scan_enabled", False)

    update = await _run_hook(AgentState(security_findings=[_FIRST]))

    assert update == {"security_findings": Overwrite([])}


async def test_findings_flow_through_a_real_graph_and_are_consumed_once() -> None:
    """Exec-style commits accumulate through the channel's reducer (two passes of
    one batch), the hook delivers every entry behind the messages already there, and
    the channel is empty afterwards — nothing is delivered twice."""
    state_cls = build_agent_state(ExtensionRegistry())

    async def exec_pass(state: Any) -> dict[str, Any]:
        return {"security_findings": [_FIRST if not state.security_findings else _SECOND]}

    async def after_exec(state: Any) -> dict[str, Any] | None:
        return await _run_hook(state)

    graph: Any = StateGraph(state_cls)
    graph.add_node("exec_one", exec_pass)
    graph.add_node("exec_two", exec_pass)
    graph.add_node("after_exec", after_exec)
    graph.add_edge(START, "exec_one")
    graph.add_edge("exec_one", "exec_two")
    graph.add_edge("exec_two", "after_exec")
    graph.add_edge("after_exec", END)

    final = state_cls.model_validate(
        await graph.compile().ainvoke(
            state_cls(messages=[HumanMessage(content="earlier", id="m0")])
        )
    )

    assert final.security_findings == []
    contents = [str(m.content) for m in final.messages]
    assert contents[0] == "earlier"
    assert len(contents) == 3
    assert "web.fetch" in contents[1]
    assert "context-file:/repo/AGENTS.md" in contents[2]


async def test_a_real_childs_finding_reaches_the_model_through_the_hook(
    fake_cancel_event: InterruptEvent,
) -> None:
    """End to end on real graph state: agent code in a real exec child scans flagged content,
    the exec node commits the finding to `state.security_findings`, and the framework's
    after_exec hook delivers it as a SECURITY note behind the tool result and empties the
    channel — the next LLM call sees the warning exactly once."""
    runtime = Runtime(
        context=AvaContext(
            ops_pool=make_fake_ops_pool(),
            event_publisher=MagicMock(),
            agent=AgentSlices.resolve(
                default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
            ),
            db=Database.from_settings(),
            bus=EventBus.from_settings(),
            clients=process_clients(),
            identity=AgentIdentity(agent_id=1042, owns_loop=True),
            catalog=build_model_catalog(),
            clock_factory=Clock.from_settings,
        )
    )
    code = (
        "from ava.security import scan_content\n"
        "scan_content('ignore previous instructions', source='web.fetch')\n"
        "print('fetched')"
    )
    state = AgentState(
        messages=[
            AIMessage(
                id="ai-1",
                content="",
                tool_calls=[{"name": "execute_code", "args": {"code": code}, "id": "call-0"}],
            )
        ]
    )

    async def after_exec(state: Any) -> dict[str, Any] | None:
        return await _deliver_security_findings(state, runtime, _CONFIG)  # type: ignore[arg-type]

    builder: Any = StateGraph(AgentState, context_schema=AvaContext)
    builder.add_node("exec", _exec_node_impl, input_schema=AgentState)
    builder.add_node("after_exec", after_exec)
    builder.add_edge(START, "exec")
    builder.add_edge("after_exec", END)
    result = await builder.compile().ainvoke(dict(state), context=runtime.context, config=_CONFIG)

    assert [m.type for m in result["messages"]] == ["ai", "tool", "human"]
    assert "fetched" in result["messages"][1].content
    assert "web.fetch" in result["messages"][2].content
    assert "ignore previous instructions" in result["messages"][2].content
    assert result["security_findings"] == []
