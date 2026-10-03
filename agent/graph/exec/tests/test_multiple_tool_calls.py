"""Each tool call keeps its own execution boundary and protocol result."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph.exec._result import _ExecDone
from agent.graph.exec.node import _exec_node_impl
from agent.graph.interrupt import InterruptEvent
from agent.graph.tool_calls import normalize_tool_calls
from agent.state import AgentState
from agent.tests._fakes import make_fake_ops_pool
from base.agents.context import AvaContext
from base.host.env.agent_slices import AgentSlices


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


async def test_calls_execute_separately_without_rewriting_assistant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ai = AIMessage(
        id="ai-multi",
        content="Two independent calls.",
        tool_calls=[
            {"name": "execute_code", "args": {"code": "first()"}, "id": "call-a"},
            {"name": "execute_code", "args": {"code": "second()"}, "id": "call-b"},
        ],
    )
    original = ai.model_dump()
    run = AsyncMock(
        side_effect=[
            (_ExecDone(output="first result"), {}, 1, [], None, []),
            (_ExecDone(output="second result"), {}, 2, [], None, []),
        ]
    )
    monkeypatch.setattr("agent.graph.exec.node._run_agent_code", run)
    runtime = Runtime(
        context=AvaContext(
            ops_pool=make_fake_ops_pool(), event_publisher=MagicMock(), agent=AgentSlices.resolve()
        )
    )
    command = await _run_calls(
        AgentState(messages=[ai]), runtime, {"configurable": {"thread_id": "7"}}
    )

    assert [call.args[3] for call in run.await_args_list] == ["first()", "second()"]
    assert ai.model_dump() == original
    assert command is not None
    messages = command["messages"][1:]
    assert all(isinstance(message, ToolMessage) for message in messages)
    assert [message.tool_call_id for message in messages] == ["call-a", "call-b"]
    assert "first result" in messages[0].content
    assert "second result" in messages[1].content


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
            agent=AgentSlices.resolve(),
        )
    )


async def test_real_children_do_not_share_globals(fake_cancel_event: InterruptEvent) -> None:
    result = await _run_calls(
        _state('shared_name = 42; print("first")', 'print("shared_name" in globals())'),
        _runtime(),
        {"configurable": {"thread_id": "7"}},
    )
    assert result is not None
    _, first, second = result["messages"]
    assert first.tool_call_id == "call-0"
    assert second.tool_call_id == "call-1"
    assert "first" in first.content
    assert "False" in second.content


async def test_exception_does_not_skip_later_call(fake_cancel_event: InterruptEvent) -> None:
    result = await _run_calls(
        _state('raise ValueError("first failed")', 'print("second ran")'),
        _runtime(),
        {"configurable": {"thread_id": "7"}},
    )
    assert result is not None
    _, first, second = result["messages"]
    assert "ValueError: first failed" in first.content
    assert "second ran" in second.content


async def test_plugin_reducers_commit_between_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from operator import add
    from typing import Annotated

    class CounterState(AgentState):
        total: Annotated[int, add] = 10

    state = CounterState(messages=_state("first()", "second()").messages)
    snapshots: list[int] = []

    async def run(state: CounterState, *args: Any) -> tuple[Any, ...]:
        snapshots.append(state.total)
        return _ExecDone(output="ok"), {"total": 1}, 0, [], None, None

    monkeypatch.setattr("agent.graph.exec.node._run_agent_code", run)
    result = await _run_calls(state, _runtime(), {"configurable": {"thread_id": "7"}})
    assert snapshots == [10, 11]
    assert state.total == 10
    assert result is not None
    assert result["total"] == 12


async def test_notes_and_media_follow_all_results_and_stream_ids_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from langchain_core.messages import HumanMessage

    note = HumanMessage(content="context note")
    media = HumanMessage(content="attachment")
    run = AsyncMock(
        side_effect=[
            (_ExecDone(output="first"), {"messages": [note]}, 1, [], None, []),
            (_ExecDone(output="second"), {}, 2, [], None, []),
        ]
    )
    monkeypatch.setattr("agent.graph.exec.node._run_agent_code", run)
    monkeypatch.setattr(
        "agent.graph.exec.node.build_attach_message", MagicMock(side_effect=[media, None])
    )
    runtime = _runtime()
    result = await _run_calls(
        _state("first()", "second()"), runtime, {"configurable": {"thread_id": "7"}}
    )
    assert result is not None
    messages = result["messages"][1:]
    assert [message.tool_call_id for message in messages[:2]] == ["call-0", "call-1"]
    assert messages[2:] == [note, media]
    publisher = runtime.context.event_publisher
    assert isinstance(publisher, MagicMock)
    events = [json.loads(call.args[0]) for call in publisher.emit.call_args_list]
    assert [event["item_id"] for event in events] == ["1.0", "1.0", "2.0", "2.0"]


@pytest.mark.parametrize("outcome", ["cancel", "restart", "terminate", "compact", "timeout"])
async def test_lifecycle_pairs_skipped_calls_and_timeout_continues(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    from agent.graph.exec._result import _ExecCancelled, _ExecLifecycle, _ExecTimedOut
    from base.agents.lifecycle import AgentRestart, AgentTermination, SystemHalt

    outcomes = {
        "cancel": _ExecCancelled(output="cancelled"),
        "restart": _ExecLifecycle(output="restart", exc=AgentRestart()),
        "terminate": _ExecLifecycle(output="terminate", exc=AgentTermination()),
        "compact": _ExecLifecycle(output="compact", exc=SystemHalt()),
        "timeout": _ExecTimedOut(output="timeout"),
    }
    run = AsyncMock(
        side_effect=[
            (outcomes[outcome], {}, 1, [], None, None),
            (_ExecDone(output="second"), {}, 2, [], None, []),
        ]
    )
    monkeypatch.setattr("agent.graph.exec.node._run_agent_code", run)
    result = await _run_calls(
        _state("first()", "second()"), _runtime(), {"configurable": {"thread_id": "7"}}
    )
    assert result is not None
    if outcome == "compact":
        assert len(result["messages"]) == 1
    else:
        _, first, second = result["messages"]
        assert [first.tool_call_id, second.tool_call_id] == ["call-0", "call-1"]
        assert ("second" if outcome == "timeout" else "Not executed") in second.content
    assert run.await_count == (2 if outcome == "timeout" else 1)
    assert result["halted"] is (outcome != "timeout")


async def test_unknown_tool_does_not_consume_sibling_code(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state("print('must not run')", "print('runs')")
    ai = state.messages[-1]
    assert isinstance(ai, AIMessage)
    ai.tool_calls[0]["name"] = "ava.files.edit"
    run = AsyncMock(return_value=(_ExecDone(output="runs"), {}, 0, [], None, []))
    monkeypatch.setattr("agent.graph.exec.node._run_agent_code", run)
    result = await _run_calls(state, _runtime(), {"configurable": {"thread_id": "7"}})
    assert result is not None
    _, first, second = result["messages"]
    assert "unknown tool" in first.content
    assert "runs" in second.content
    run.assert_awaited_once()
    assert run.await_args is not None
    assert run.await_args.args[3] == "print('runs')"


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


def test_normalize_recovers_each_content_call_without_merging() -> None:
    original = _ai_with_two_content_tool_uses()
    normalized = normalize_tool_calls(original)
    assert normalized is not None
    assert normalized.id == original.id
    assert normalized.content == original.content
    assert normalized.response_metadata == original.response_metadata
    assert normalized.usage_metadata == original.usage_metadata
    assert [call["id"] for call in normalized.tool_calls] == ["call_00", "call_01"]
    assert [call["args"]["code"] for call in normalized.tool_calls] == [
        'print("skills")',
        'print("agents")',
    ]
    assert normalize_tool_calls(normalized) is None


async def test_exec_recovers_missing_content_call_without_syntax_plugin(
    fake_cancel_event: InterruptEvent,
) -> None:
    state = AgentState(messages=[_ai_with_two_content_tool_uses()])
    result = await _run_calls(state, _runtime(), _config())
    assert result is not None
    fixed_ai, first, second = result["messages"]
    assert len(fixed_ai.tool_calls) == 2
    assert first.tool_call_id == "call_00"
    assert second.tool_call_id == "call_01"
    assert "skills" in first.content
    assert "agents" in second.content


def _ai_with_hallucinated_tool_name() -> AIMessage:
    # Reproduces django__django-13417 from SWE-bench Verified: DeepSeek (via
    # the Anthropic-compatible endpoint) returned a tool_use block calling
    # `ava.files.edit` as if it were a registered tool. Anthropic Claude's
    # grammar-constrained decoding rejects this, but DeepSeek's compat layer
    # does not, so the wire reaches exec_node verbatim.
    return AIMessage(
        id="ai_hallucinated",
        content=[],
        tool_calls=[
            {
                "name": "ava.files.edit",
                "args": {"path": "/testbed/x.py", "old": "a", "new": "b"},
                "id": "call_99",
            },
        ],
        response_metadata={"stop_reason": "tool_use"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )


async def test_exec_node_feeds_back_on_unknown_tool_name() -> None:
    state = AgentState(messages=[_ai_with_hallucinated_tool_name()])

    result = await _run_calls(state, _runtime(), _config())

    assert result is not None
    assert result["halted"] is False
    _, tool_msg = result["messages"]
    assert tool_msg.tool_call_id == "call_99"
    assert "ava.files.edit" in tool_msg.content
    assert "execute_code" in tool_msg.content


async def test_exec_node_feeds_back_when_code_key_missing() -> None:
    # execute_code name but malformed args ({} or missing "code" key) — same
    # KeyError path as the unknown-name case, must also feed back not crash.
    ai = AIMessage(
        id="ai_no_code",
        content=[],
        tool_calls=[{"name": "execute_code", "args": {"foo": "bar"}, "id": "call_88"}],
        response_metadata={"stop_reason": "tool_use"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    state = AgentState(messages=[ai])

    result = await _run_calls(state, _runtime(), _config())

    assert result is not None
    _, tool_msg = result["messages"]
    assert tool_msg.tool_call_id == "call_88"
    assert "execute_code" in tool_msg.content


async def test_langgraph_owns_each_call_state_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    from typing import Annotated

    def append_digit(current: int, digit: int) -> int:
        return current * 10 + digit

    class DecimalState(AgentState):
        total: Annotated[int, append_digit] = 0

    snapshots: list[int] = []

    async def run(state: DecimalState, *args: Any) -> tuple[Any, ...]:
        snapshots.append(state.total)
        return _ExecDone(output="ok"), {"total": len(snapshots)}, 0, [], None, None

    monkeypatch.setattr("agent.graph.exec.node._run_agent_code", run)
    state = DecimalState(total=7, messages=_state("first()", "second()").messages)
    result = await _run_calls(state, _runtime(), _config())
    assert snapshots == [7, 71]
    assert result["total"] == 712


async def test_checkpoint_resume_keeps_results_and_deferred_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver

    note = HumanMessage(content="context from first call", id="note-first")
    run = AsyncMock(
        side_effect=[
            (_ExecDone(output="first"), {"messages": [note]}, 0, [], None, []),
            (_ExecDone(output="second"), {}, 0, [], None, []),
        ]
    )
    monkeypatch.setattr("agent.graph.exec.node._run_agent_code", run)
    graph = _graph(AgentState, checkpointer=InMemorySaver(), interrupt_after=["exec"])
    config = _config()
    runtime = _runtime()
    await graph.ainvoke(dict(_state("first()", "second()")), config=config, context=runtime.context)
    checkpoint = await graph.aget_state(config)
    assert checkpoint.next == ("exec",)
    assert len(checkpoint.values["messages"]) == 2
    assert checkpoint.values["messages"][-1].tool_call_id == "call-0"
    assert checkpoint.values["pending_exec_notes"] == [note]
    assert run.await_count == 1

    result = await graph.ainvoke(None, config=config, context=runtime.context)
    assert [call.args[3] for call in run.await_args_list] == ["first()", "second()"]
    assert [message.tool_call_id for message in result["messages"][1:3]] == [
        "call-0",
        "call-1",
    ]
    assert result["messages"][3:] == [note]
    assert result["pending_exec_notes"] == []
