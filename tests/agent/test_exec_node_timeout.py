"""Test exec node graph-level timeout shield (asyncio.wait_for).

If the inner per-code-block timeout (exec_timeout_seconds) misses a cancellable
framework hang, the outer node-level timeout (exec_node_timeout_seconds)
requests cancellation and waits for resource teardown before surfacing it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph._context import AvaContext
from agent.graph._exec import _exec_node_impl
from agent.state import AgentState
from tests.agent._fakes import make_fake_ops_pool

_CONFIG: RunnableConfig = {"configurable": {"thread_id": "7"}}

# A minimal AIMessage with one execute_code tool call — what exec_node expects.
_TOOL_CALL_AIMESSAGE = AIMessage(
    content="",
    tool_calls=[
        {"name": "execute_code", "args": {"code": "x = 1"}, "id": "tc-1", "type": "tool_call"}
    ],
    response_metadata={"model_provider": "anthropic", "stop_reason": "tool_use"},
    usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
)


def _make_runtime() -> Runtime[AvaContext]:
    """Minimal runtime with fake ops_pool + event_publisher."""
    ctx = AvaContext(
        ops_pool=make_fake_ops_pool(),
        llm=None,
        event_publisher=MagicMock(),
    )
    return Runtime(context=ctx)


async def test_exec_node_timeout_fires_asyncio_wait_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When _run_in_subprocess hangs longer than exec_node_timeout_seconds,
    the outer asyncio.wait_for inside _exec_node_impl fires.

    We monkeypatch _run_in_subprocess to wait forever on a cancellable Future,
    simulating a framework wait that the inner code-exec timeout missed.
    """
    # Graph-level timeout very short, inner timeout irrelevant (patched out)
    monkeypatch.setattr("shared.config.settings.sandbox.exec_node_timeout_seconds", 0.05)

    async def _hang_forever(*args, **kwargs):
        await asyncio.Future()  # never completes

    monkeypatch.setattr(
        "agent.graph._exec._run_in_subprocess",
        _hang_forever,  # pyright: ignore[reportUnknownArgumentType]
    )

    state = AgentState(messages=[HumanMessage(content="hi"), _TOOL_CALL_AIMESSAGE], halted=False)

    # _exec_node_impl will call our _hang_forever → asyncio.wait_for(0.05s) fires
    # → TimeoutError caught → returns _ExecTimedOut via Command
    from langgraph.types import Command

    result = await _exec_node_impl(state, _make_runtime(), _CONFIG)

    # The result is a Command (returned by _exec_node_impl's match dispatch).
    # _ExecTimedOut branch sets halted=False and returns a Command with
    # messages + goto=AFTER_EXEC.
    assert isinstance(result, Command), f"Expected Command, got {type(result).__name__}"
    update = result.update
    assert update is not None
    msgs = update.get("messages", [])
    assert len(msgs) >= 1, f"Expected at least 1 message in Command.update, got {msgs}"
    tool_msg = msgs[-1]
    assert isinstance(tool_msg, ToolMessage), f"Expected ToolMessage, got {type(tool_msg).__name__}"
    content = tool_msg.content  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(content, str), f"Expected str content, got {type(content).__name__}"  # pyright: ignore[reportUnknownArgumentType]
    assert "timeout" in content.lower(), (
        f"ToolMessage content should mention timeout, got: {content!r}"
    )


async def test_exec_node_timeout_does_not_fire_when_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When _run_in_subprocess completes quickly, exec_node_timeout_seconds
    does NOT fire — the normal path returns _ExecDone."""
    monkeypatch.setattr("shared.config.settings.sandbox.exec_node_timeout_seconds", 10.0)
    monkeypatch.setattr("shared.config.settings.sandbox.exec_timeout_seconds", 30.0)

    from agent.graph._exec import _ExecDone, _ExecResult
    from agent.graph._exec_protocol import ResultPayload

    async def _fast_return(*args, **kwargs) -> tuple[_ExecResult, ResultPayload | None]:
        return (_ExecDone(output="hello"), None)

    monkeypatch.setattr(
        "agent.graph._exec._run_in_subprocess",
        _fast_return,  # pyright: ignore[reportUnknownArgumentType]
    )

    state = AgentState(messages=[HumanMessage(content="hi"), _TOOL_CALL_AIMESSAGE], halted=False)

    from langgraph.types import Command

    result = await _exec_node_impl(state, _make_runtime(), _CONFIG)

    assert isinstance(result, Command)
    assert result.update is not None
    msgs = result.update.get("messages", [])
    assert len(msgs) >= 1
    tool_msg = msgs[-1]
    assert isinstance(tool_msg, ToolMessage)
    assert "hello" in tool_msg.content  # pyright: ignore[reportUnknownMemberType]
