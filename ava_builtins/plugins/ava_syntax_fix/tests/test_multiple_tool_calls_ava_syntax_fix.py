"""Syntax repair per tool call: it preserves content, an unfixable call does not skip its sibling, and recovered calls keep their repair."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph.exec.node import _exec_node_impl
from agent.graph.interrupt import InterruptEvent
from agent.state import AgentState
from agent.tests._fakes import make_fake_ops_pool
from ava_builtins.plugins.ava_syntax_fix.agent_runtime import syntax_fix_before_exec
from base.agents.context import AvaContext


def _graph(state_cls: type[AgentState], **compile_options: Any) -> Any:
    from langgraph.graph import END, START, StateGraph

    builder: Any = StateGraph(state_cls, context_schema=AvaContext)
    builder.add_node("exec", _exec_node_impl, input_schema=state_cls)
    builder.add_node("after_exec", lambda _state: {})
    builder.add_edge(START, "exec")
    builder.add_edge("after_exec", END)
    return builder.compile(**compile_options)


async def _run_calls(
    state: AgentState, runtime: Runtime[AvaContext], config: RunnableConfig
) -> dict[str, Any]:
    return await _graph(type(state)).ainvoke(dict(state), context=runtime.context, config=config)


def _state(*codes: str) -> AgentState:
    return AgentState(
        messages=[
            AIMessage(
                id="ai-batch",
                content="",
                tool_calls=[
                    {"name": "execute_code", "args": {"code": code}, "id": f"call-{i}"}
                    for i, code in enumerate(codes)
                ],
            )
        ]
    )


def _runtime() -> Runtime[AvaContext]:
    return Runtime(
        context=AvaContext(
            ops_pool=make_fake_ops_pool(),
            event_publisher=MagicMock(),
        )
    )


async def test_syntax_repair_is_per_call_and_preserves_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ava_builtins.plugins.ava_syntax_fix import agent_runtime as syntax

    state = _state("print(1)", "print(2)")
    ai = state.messages[-1]
    assert isinstance(ai, AIMessage)
    ai.content = [
        {"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["args"]}
        for call in ai.tool_calls
    ]
    original = ai.model_dump()
    fix = MagicMock(side_effect=[("print(10)", ["test"]), ("print(20)", ["test"])])
    monkeypatch.setattr(syntax, "_apply_fix_pipeline", fix)
    update = await syntax.syntax_fix_before_exec(state, _runtime(), {})
    assert update is not None
    fixed = update["messages"][0]
    assert [call.args[0] for call in fix.call_args_list] == ["print(1)", "print(2)"]
    assert [call["id"] for call in fixed.tool_calls] == ["call-0", "call-1"]
    assert [call["args"]["code"] for call in fixed.tool_calls] == ["print(10)", "print(20)"]
    assert [block["input"]["code"] for block in fixed.content] == ["print(10)", "print(20)"]
    assert ai.model_dump() == original


async def test_unfixable_syntax_does_not_skip_sibling(
    monkeypatch: pytest.MonkeyPatch,
    fake_cancel_event: InterruptEvent,
) -> None:
    from agent.messages.guard import guarded_add_messages
    from ava_builtins.plugins.ava_syntax_fix import agent_runtime as syntax

    state = _state("return", 'print("sibling ran")')

    def unchanged(code: str) -> tuple[str, list[str]]:
        return code, []

    monkeypatch.setattr(syntax, "_apply_fix_pipeline", unchanged)
    monkeypatch.setattr(syntax, "_llm_repair_syntax", AsyncMock(return_value=None))
    update = await syntax.syntax_fix_before_exec(state, _runtime(), {})
    assert update is not None
    assert "goto" not in update
    state.messages = guarded_add_messages(state.messages, update["messages"])
    result = await _run_calls(state, _runtime(), {"configurable": {"thread_id": "7"}})
    assert result is not None
    _, first, second = result["messages"]
    assert "SyntaxError" in first.content
    assert "sibling ran" in second.content


def _config() -> RunnableConfig:
    return {"configurable": {"thread_id": "7"}}


def _ai_with_two_content_tool_uses() -> AIMessage:
    first_code = 'print("skills")'
    second_code = 'print("agents")'
    return AIMessage(
        id="ai_multi",
        content=[
            {"type": "thinking", "thinking": "plan", "index": 0},
            {
                "type": "tool_use",
                "id": "call_00",
                "name": "execute_code",
                "input": {},
                "partial_json": json.dumps({"code": first_code}),
                "index": 1,
            },
            {
                "type": "tool_use",
                "id": "call_01",
                "name": "execute_code",
                "input": {},
                "partial_json": json.dumps({"code": second_code}),
                "index": 2,
            },
        ],
        tool_calls=[
            {"name": "execute_code", "args": {"code": first_code}, "id": "call_00"},
        ],
        response_metadata={"stop_reason": "tool_use"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )


async def test_syntax_fix_preserves_recovered_calls() -> None:
    state = AgentState(messages=[_ai_with_two_content_tool_uses()])
    update = await syntax_fix_before_exec(state, _runtime(), _config())
    assert update is not None
    fixed = update["messages"][0]
    assert [call["id"] for call in fixed.tool_calls] == ["call_00", "call_01"]
    assert [call["args"]["code"].strip() for call in fixed.tool_calls] == [
        'print("skills")',
        'print("agents")',
    ]
